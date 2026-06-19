"""Match bank transfers to payroll employees using fuzzy name matching."""

import unicodedata
import re
import pandas as pd
from thefuzz import fuzz

FUZZY_THRESHOLD = 72  # minimum score to consider a name match

# Keywords in Verwendungszweck that indicate a salary payment
SALARY_KEYWORDS = [
    "gehalt", "lohn", "salary", "entgelt", "vergütung", "verguetung",
    "monatslohn", "monatsgehalt", "nettolohn", "nettogehalt",
]


def normalize_name(name: str) -> str:
    """Lowercase, strip accents, collapse spaces."""
    if not name:
        return ""
    nfkd = unicodedata.normalize("NFKD", name)
    ascii_name = "".join(c for c in nfkd if not unicodedata.combining(c))
    ascii_name = ascii_name.lower()
    ascii_name = re.sub(r"[^a-z ]", " ", ascii_name)
    return " ".join(ascii_name.split())


def _score(query: str, candidate: str) -> int:
    return fuzz.token_set_ratio(normalize_name(query), normalize_name(candidate))


def is_salary_transfer(purpose: str) -> bool:
    """Return True if the purpose text looks like a salary payment."""
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
    A bank transfer is a candidate for a given payroll month if:
      - It is a salary transfer (purpose contains Gehalt/Lohn/etc.), AND
      - The purpose month matches, OR booking date is in payroll month/month+1,
        OR no month could be determined at all.

    If the purpose has NO salary keyword, it is still kept as a candidate
    (the user may have transfers without a standard keyword).
    """
    purpose_year = row.get("year")
    purpose_month = row.get("month")
    booking_year = row.get("booking_year")
    booking_month = row.get("booking_month")

    # Rule 1 – purpose explicitly states the payroll month
    if purpose_year == payroll_year and purpose_month == payroll_month:
        return True

    # Rule 2 – booking date is in payroll month or payroll month+1
    if booking_year and booking_month:
        if booking_year == payroll_year and booking_month == payroll_month:
            return True
        next_month = payroll_month % 12 + 1
        next_year = payroll_year + (1 if payroll_month == 12 else 0)
        if booking_year == next_year and booking_month == next_month:
            return True

    # Rule 3 – no month info at all → keep as fallback candidate
    if purpose_year is None and booking_year is None:
        return True

    return False


def match_transfers(payroll_df: pd.DataFrame, bank_df: pd.DataFrame,
                    payroll_year: int, payroll_month: int) -> pd.DataFrame:
    """
    For each employee in payroll_df, find matching salary bank transfers.

    Matching logic:
      1. Prefer transfers whose Verwendungszweck contains a salary keyword
         (Gehalt, Lohn, …) — these are the primary salary candidates.
      2. Match the account holder name (Beguenstigter/recipient field) against
         the payroll employee name using fuzzy matching.
      3. Also try matching against the purpose text for banks that embed the
         employee name there.
      4. Outgoing payments are stored as negative amounts in German bank exports;
         the absolute value is used for comparison against NetSalary.

    Returns enriched payroll_df with extra columns:
      paid_amount, bank_reference, match_score, match_reason,
      is_salary_transfer, status, difference, difference_pct, pending_amount
    """
    if payroll_df.empty:
        return payroll_df.copy()

    if not bank_df.empty:
        # Tag each transfer as salary-related
        bank_df = bank_df.copy()
        bank_df["is_salary"] = bank_df["purpose"].apply(
            lambda p: is_salary_transfer(str(p))
        )

        # Filter to month candidates
        mask = bank_df.apply(
            lambda r: _transfer_is_candidate(r, payroll_year, payroll_month), axis=1
        )
        month_bank = bank_df[mask].copy().reset_index(drop=True)

        # Prefer salary-tagged rows; if none exist keep all candidates
        salary_only = month_bank[month_bank["is_salary"]]
        working_bank = salary_only if not salary_only.empty else month_bank
    else:
        month_bank = pd.DataFrame()
        working_bank = pd.DataFrame()

    results = []
    used_indices: set[int] = set()

    for _, emp in payroll_df.iterrows():
        expected_net = emp["net_salary"]
        matched_amount = 0.0
        matched_refs = []
        matched_score = 0
        match_reasons = []

        if not working_bank.empty:
            for idx, transfer in working_bank.iterrows():
                if idx in used_indices:
                    continue

                recipient = str(transfer.get("recipient", ""))
                purpose = str(transfer.get("purpose", ""))

                # Primary: match account holder name (recipient field)
                score_recipient = _score(emp["employee_name"], recipient)
                # Fallback: match against purpose text
                score_purpose = _score(emp["employee_name"], purpose)
                best_score = max(score_recipient, score_purpose)

                if best_score >= FUZZY_THRESHOLD:
                    # Salary payments are outgoing → negative in German bank CSV.
                    # Use absolute value so €-1.808,40 correctly matches NetSalary 1808.40.
                    raw_amount = transfer["amount"]
                    matched_amount += abs(raw_amount)
                    ref = purpose[:80] if purpose else recipient[:80]
                    matched_refs.append(ref)
                    if best_score > matched_score:
                        matched_score = best_score
                    field = "recipient" if score_recipient >= score_purpose else "purpose"
                    sign_note = "outgoing" if raw_amount < 0 else "incoming"
                    match_reasons.append(
                        f"[{field} score={best_score}, {sign_note}, "
                        f"salary_kw={'yes' if transfer.get('is_salary') else 'no'}]"
                    )
                    used_indices.add(idx)

        status = compute_status(matched_amount, expected_net)
        difference = matched_amount - expected_net
        diff_pct = (difference / expected_net * 100) if expected_net else 0.0
        pending = max(0.0, expected_net - matched_amount)

        results.append({
            **emp.to_dict(),
            "paid_amount": matched_amount,
            "bank_reference": "; ".join(matched_refs),
            "match_score": matched_score,
            "match_reason": "; ".join(match_reasons),
            "status": status,
            "difference": difference,
            "difference_pct": diff_pct,
            "pending_amount": pending,
        })

    return pd.DataFrame(results)
