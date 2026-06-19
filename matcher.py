"""Match bank transfers to payroll employees using fuzzy name matching."""

import unicodedata
import re
import pandas as pd
from thefuzz import fuzz

FUZZY_THRESHOLD = 65   # lowered – German names with umlauts lose points after normalization

# Keywords in Verwendungszweck that indicate a salary payment
SALARY_KEYWORDS = [
    "gehalt", "lohn", "salary", "entgelt",
    "vergütung", "verguetung", "monatslohn", "monatsgehalt",
    "nettolohn", "nettogehalt",
]


def normalize_name(name: str) -> str:
    """
    Lowercase, remove accents/umlauts, strip punctuation, collapse spaces.
    Handles 'Müller, Hans' → 'muller hans'  (comma removed, umlaut folded)
    """
    if not name:
        return ""
    # Expand common German umlauts before stripping diacritics
    name = (name
            .replace("ä", "ae").replace("Ä", "ae")
            .replace("ö", "oe").replace("Ö", "oe")
            .replace("ü", "ue").replace("Ü", "ue")
            .replace("ß", "ss"))
    nfkd = unicodedata.normalize("NFKD", name)
    ascii_name = "".join(c for c in nfkd if not unicodedata.combining(c))
    ascii_name = ascii_name.lower()
    # Keep only letters and spaces
    ascii_name = re.sub(r"[^a-z ]", " ", ascii_name)
    return " ".join(ascii_name.split())


def name_tokens(name: str) -> set[str]:
    """Return the set of word tokens in a normalized name."""
    return set(normalize_name(name).split())


def score_names(payroll_name: str, bank_name: str) -> int:
    """
    Return a 0-100 similarity score between two names.

    Strategy (take the max):
    1. token_set_ratio  – handles any token order and extra tokens
    2. token_sort_ratio – handles reordered tokens
    3. partial_ratio    – handles one name being a substring of the other
    4. Token overlap    – fraction of payroll tokens found in bank name tokens × 100
    """
    a = normalize_name(payroll_name)
    b = normalize_name(bank_name)
    if not a or not b:
        return 0

    s1 = fuzz.token_set_ratio(a, b)
    s2 = fuzz.token_sort_ratio(a, b)
    s3 = fuzz.partial_ratio(a, b)

    # Token overlap: how many tokens from the payroll name appear in the bank name?
    ta = name_tokens(payroll_name)
    tb = name_tokens(bank_name)
    if ta:
        overlap = len(ta & tb) / len(ta)
        s4 = int(overlap * 100)
    else:
        s4 = 0

    return max(s1, s2, s3, s4)


def is_salary_transfer(purpose: str) -> bool:
    if not purpose:
        return False
    p = purpose.lower()
    return any(kw in p for kw in SALARY_KEYWORDS)


def compute_status(paid: float, expected: float, tolerance: float = 0.02) -> str:
    if paid == 0:
        return "Not Paid"
    if expected == 0:
        return "Paid" if paid > 0 else "Not Paid"
    ratio = paid / expected
    diff = paid - expected
    if abs(diff) <= tolerance * expected:
        return "Paid"
    if diff < 0:
        if abs(diff) / expected < 0.05:
            return "Partially Paid"
        return "Underpaid"
    if ratio < 2 - tolerance:
        return "Overpaid"
    if ratio <= 2 + tolerance:
        return "Double Paid"
    return "Multiple Overpayments"


def _transfer_is_candidate(row: pd.Series, payroll_year: int, payroll_month: int) -> bool:
    """
    Accept a transfer if:
      1. Purpose text says month == payroll month/year, OR
      2. Booking date is in payroll month OR month+1, OR
      3. No month info found at all (keep as fallback).
    """
    purpose_year  = row.get("year")
    purpose_month = row.get("month")
    booking_year  = row.get("booking_year")
    booking_month = row.get("booking_month")

    # Rule 1 – purpose says this is for the payroll month
    try:
        if int(purpose_year) == payroll_year and int(purpose_month) == payroll_month:
            return True
    except (TypeError, ValueError):
        pass

    # Rule 2 – booking date is in payroll month or payroll month+1
    if booking_year and booking_month:
        if int(booking_year) == payroll_year and int(booking_month) == payroll_month:
            return True
        next_month = payroll_month % 12 + 1
        next_year  = payroll_year + (1 if payroll_month == 12 else 0)
        if int(booking_year) == next_year and int(booking_month) == next_month:
            return True

    # Rule 3 – no month info at all
    if purpose_year is None and booking_year is None:
        return True

    return False


def match_transfers(payroll_df: pd.DataFrame, bank_df: pd.DataFrame,
                    payroll_year: int, payroll_month: int) -> pd.DataFrame:
    """
    Match salary bank transfers to payroll employees.

    Name matching:
      - 'Beguenstigter/Zahlungspflichtiger' (account holder) is the primary field.
      - Names can be in any order: 'Mustermann Max', 'Max Mustermann',
        'MUSTERMANN, MAX', 'Mustermann, Max' are all equivalent.
      - Umlaut folding: Müller → Mueller, Schäfer → Schaefer, etc.
      - Fuzzy threshold: 65 (permissive to handle spelling variants).

    Month matching:
      - Purpose text like 'Gehalt Mai 2026' or 'Lohn 05/2026' is parsed.
      - Booking date within payroll month or month+1 is also accepted.

    Amount:
      - Outgoing salary payments are negative in German bank exports.
        The absolute value is used for comparison against NetSalary.
    """
    if payroll_df.empty:
        return payroll_df.copy()

    if not bank_df.empty:
        bank_df = bank_df.copy()
        bank_df["is_salary"] = bank_df["purpose"].apply(
            lambda p: is_salary_transfer(str(p))
        )
        mask = bank_df.apply(
            lambda r: _transfer_is_candidate(r, payroll_year, payroll_month), axis=1
        )
        month_bank = bank_df[mask].copy().reset_index(drop=True)
        salary_rows = month_bank[month_bank["is_salary"]]
        # Use salary-tagged rows if any exist; else fall back to all candidates
        working_bank = salary_rows if not salary_rows.empty else month_bank
    else:
        month_bank  = pd.DataFrame()
        working_bank = pd.DataFrame()

    results = []
    used_indices: set[int] = set()

    for _, emp in payroll_df.iterrows():
        expected_net  = emp["net_salary"]
        matched_amount = 0.0
        matched_refs   = []
        matched_score  = 0
        match_reasons  = []

        if not working_bank.empty:
            for idx, transfer in working_bank.iterrows():
                if idx in used_indices:
                    continue

                recipient = str(transfer.get("recipient", "") or "")
                purpose   = str(transfer.get("purpose",   "") or "")

                # Match against account holder field (primary)
                score_holder  = score_names(emp["employee_name"], recipient)
                # Also try purpose text as fallback
                score_purpose = score_names(emp["employee_name"], purpose)
                best = max(score_holder, score_purpose)

                if best >= FUZZY_THRESHOLD:
                    raw_amount = transfer["amount"]
                    matched_amount += abs(raw_amount)
                    matched_refs.append(purpose[:80] if purpose else recipient[:80])
                    if best > matched_score:
                        matched_score = best
                    field = "holder" if score_holder >= score_purpose else "purpose"
                    sign  = "outgoing" if raw_amount < 0 else "incoming"
                    match_reasons.append(
                        f"[{field} score={best}, {sign}, "
                        f"salary={'yes' if transfer.get('is_salary') else 'no'}]"
                    )
                    used_indices.add(idx)

        status     = compute_status(matched_amount, expected_net)
        difference = matched_amount - expected_net
        diff_pct   = (difference / expected_net * 100) if expected_net else 0.0
        pending    = max(0.0, expected_net - matched_amount)

        results.append({
            **emp.to_dict(),
            "paid_amount":    matched_amount,
            "bank_reference": "; ".join(matched_refs),
            "match_score":    matched_score,
            "match_reason":   "; ".join(match_reasons),
            "status":         status,
            "difference":     difference,
            "difference_pct": diff_pct,
            "pending_amount": pending,
        })

    return pd.DataFrame(results)
