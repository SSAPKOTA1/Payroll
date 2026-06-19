"""Match bank transfers to payroll employees."""

import unicodedata
import re
import pandas as pd

# ── Salary keywords scanned across the ENTIRE row ─────────────────────────
SALARY_KEYWORDS = [
    "gehalt", "lohn", "salary", "entgelt",
    "vergütung", "verguetung", "vergutung",
    "monatslohn", "monatsgehalt", "nettolohn", "nettogehalt",
]


# ── Name normalisation ─────────────────────────────────────────────────────

def _fold_umlauts(s: str) -> str:
    return (s
            .replace("ä", "ae").replace("Ä", "ae")
            .replace("ö", "oe").replace("Ö", "oe")
            .replace("ü", "ue").replace("Ü", "ue")
            .replace("ß", "ss"))


def name_tokens(name: str) -> set[str]:
    """
    Return the set of normalised word tokens for a name string.

    'Müller, Hans-Peter' → {'muller', 'hans', 'peter'}
    'RAMESH ARYAL'       → {'ramesh', 'aryal'}
    """
    if not name:
        return set()
    name = _fold_umlauts(name)
    nfkd = unicodedata.normalize("NFKD", name)
    ascii_str = "".join(c for c in nfkd if not unicodedata.combining(c))
    ascii_str = ascii_str.lower()
    # Replace every non-letter character with a space (handles , . - / etc.)
    ascii_str = re.sub(r"[^a-z]", " ", ascii_str)
    tokens = set(t for t in ascii_str.split() if len(t) > 1)  # drop single chars
    return tokens


def names_match(payroll_name: str, bank_name: str) -> tuple[bool, str]:
    """
    Return (matched, reason).

    Rule: ALL tokens from the payroll name must be present in the bank name
    token set (exact token match after normalisation).  This handles any
    first/last name order and case differences.

    Example:
      payroll = 'Aryal, Ramesh'  → tokens {'aryal', 'ramesh'}
      bank    = 'Ramesh Aryal'   → tokens {'ramesh', 'aryal'}
      {'aryal','ramesh'} ⊆ {'ramesh','aryal'}  → match ✅

      payroll = 'Müller, Hans'   → tokens {'muller', 'hans'}
      bank    = 'Hans Mueller'   → tokens {'hans', 'mueller'}
      ⊆ ✅

      payroll = 'Aryal, Ramesh'  → tokens {'aryal', 'ramesh'}
      bank    = 'Trip Inn GmbH'  → tokens {'trip', 'inn', 'gmbh'}
      'aryal' not in bank tokens  → no match ❌
    """
    pt = name_tokens(payroll_name)
    bt = name_tokens(bank_name)
    if not pt or not bt:
        return False, "empty tokens"
    missing = pt - bt
    if not missing:
        return True, f"all tokens matched ({pt})"
    return False, f"missing tokens {missing} (bank has {bt})"


# ── Salary detection ───────────────────────────────────────────────────────

def is_salary_row(row_text: str) -> bool:
    """
    Return True if ANY column in the row contains a salary keyword.
    `row_text` is the full concatenated text of every cell in the row.
    """
    t = row_text.lower()
    return any(kw in t for kw in SALARY_KEYWORDS)


# ── Month candidate filter ─────────────────────────────────────────────────

def _transfer_is_candidate(row: pd.Series, payroll_year: int, payroll_month: int) -> bool:
    """
    Accept a bank transfer as a candidate for this payroll month if:
      1. Purpose/row text mentions payroll month+year, OR
      2. Booking date is in payroll month or month+1, OR
      3. No month info found at all (fallback).
    """
    purpose_year  = row.get("year")
    purpose_month = row.get("month")
    booking_year  = row.get("booking_year")
    booking_month = row.get("booking_month")

    try:
        if int(purpose_year) == payroll_year and int(purpose_month) == payroll_month:
            return True
    except (TypeError, ValueError):
        pass

    if booking_year and booking_month:
        if int(booking_year) == payroll_year and int(booking_month) == payroll_month:
            return True
        next_month = payroll_month % 12 + 1
        next_year  = payroll_year + (1 if payroll_month == 12 else 0)
        if int(booking_year) == next_year and int(booking_month) == next_month:
            return True

    if purpose_year is None and booking_year is None:
        return True

    return False


# ── Payment status ─────────────────────────────────────────────────────────

def compute_status(paid: float, expected: float, tolerance: float = 0.02) -> str:
    if paid == 0:
        return "Not Paid"
    if expected == 0:
        return "Paid" if paid > 0 else "Not Paid"
    ratio = paid / expected
    diff  = paid - expected
    if abs(diff) <= tolerance * expected:
        return "Paid"
    if diff < 0:
        return "Partially Paid" if abs(diff) / expected < 0.05 else "Underpaid"
    if ratio < 2 - tolerance:
        return "Overpaid"
    if ratio <= 2 + tolerance:
        return "Double Paid"
    return "Multiple Overpayments"


# ── Main matching function ─────────────────────────────────────────────────

def match_transfers(payroll_df: pd.DataFrame, bank_df: pd.DataFrame,
                    payroll_year: int, payroll_month: int) -> pd.DataFrame:
    """
    For each employee in payroll_df, find their salary bank transfer and
    compute payment status.

    Name matching:
      • All name tokens from the payroll (Last, First) must appear in the
        bank account-holder field — handles any token order, case, umlauts.
    Salary filter:
      • The entire row text (all columns) is scanned for Gehalt/Lohn/etc.
      • If no salary rows exist in the candidate set, all candidates are used.
    Amount:
      • abs(amount) is used — outgoing payments are negative in German exports.
    """
    if payroll_df.empty:
        return payroll_df.copy()

    if not bank_df.empty:
        bank_df = bank_df.copy()

        # Tag salary rows by scanning ALL columns (row_text)
        row_text_col = bank_df["row_text"] if "row_text" in bank_df.columns else bank_df.get("purpose", pd.Series([""] * len(bank_df)))
        bank_df["is_salary"] = row_text_col.apply(lambda t: is_salary_row(str(t)))

        # Month candidate filter
        bank_df["is_candidate"] = bank_df.apply(
            lambda r: _transfer_is_candidate(r, payroll_year, payroll_month), axis=1
        )

        candidates = bank_df[bank_df["is_candidate"]].copy().reset_index(drop=True)
        salary_candidates = candidates[candidates["is_salary"]]

        # Prefer salary-tagged rows; fall back to all candidates
        working = salary_candidates if not salary_candidates.empty else candidates
    else:
        candidates = pd.DataFrame()
        working    = pd.DataFrame()

    results = []
    used_indices: set[int] = set()

    for _, emp in payroll_df.iterrows():
        expected_net   = emp["net_salary"]
        matched_amount = 0.0
        matched_refs   = []
        match_details  = []

        if not working.empty:
            for idx, transfer in working.iterrows():
                if idx in used_indices:
                    continue

                recipient = str(transfer.get("recipient", "") or "")
                matched, reason = names_match(emp["employee_name"], recipient)

                if matched:
                    raw_amount = transfer["amount"]
                    matched_amount += abs(raw_amount)
                    purpose = str(transfer.get("purpose", "") or "")
                    matched_refs.append(purpose[:80] if purpose else recipient[:80])
                    sign = "outgoing" if raw_amount < 0 else "incoming"
                    match_details.append(
                        f"[{reason}, {sign}, "
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
            "match_score":    100 if matched_refs else 0,
            "match_reason":   "; ".join(match_details),
            "status":         status,
            "difference":     difference,
            "difference_pct": diff_pct,
            "pending_amount": pending,
        })

    return pd.DataFrame(results)
