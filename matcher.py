"""Match bank transfers to payroll employees using fuzzy name matching."""

import unicodedata
import re
import pandas as pd
from thefuzz import fuzz

FUZZY_THRESHOLD = 72  # minimum score to consider a name match


def normalize_name(name: str) -> str:
    """Lowercase, strip accents, collapse spaces."""
    if not name:
        return ""
    # Normalize unicode (decompose accented chars)
    nfkd = unicodedata.normalize("NFKD", name)
    ascii_name = "".join(c for c in nfkd if not unicodedata.combining(c))
    ascii_name = ascii_name.lower()
    # Keep only letters and spaces
    ascii_name = re.sub(r"[^a-z ]", " ", ascii_name)
    return " ".join(ascii_name.split())


def _best_match(query: str, candidates: list[str]) -> tuple[str, int]:
    """Return (best_candidate, score) for fuzzy name matching."""
    q = normalize_name(query)
    best_score = 0
    best_cand = ""
    for c in candidates:
        score = fuzz.token_set_ratio(q, normalize_name(c))
        if score > best_score:
            best_score = score
            best_cand = c
    return best_cand, best_score


def compute_status(paid: float, expected: float, tolerance: float = 0.02) -> str:
    """Determine payment status string."""
    if paid == 0:
        return "Not Paid"
    ratio = paid / expected if expected else float("inf")
    diff = paid - expected
    if abs(diff) <= tolerance * max(expected, 1):
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


def match_transfers(payroll_df: pd.DataFrame, bank_df: pd.DataFrame,
                    payroll_year: int, payroll_month: int) -> pd.DataFrame:
    """
    For each employee in payroll_df, find matching bank transfers
    and compute payment status.

    Returns enriched payroll_df with extra columns:
      paid_amount, bank_reference, match_score, status,
      difference, difference_pct, pending_amount
    """
    if payroll_df.empty:
        return payroll_df.copy()

    # Filter bank transfers to the same month (where month is known)
    if not bank_df.empty:
        month_bank = bank_df[
            ((bank_df["year"] == payroll_year) & (bank_df["month"] == payroll_month))
            | (bank_df["year"].isna())  # transfers without detected month kept as candidates
        ].copy()
    else:
        month_bank = pd.DataFrame(columns=bank_df.columns if not bank_df.empty else
                                  ["booking_date", "recipient", "purpose", "amount", "iban", "year", "month"])

    emp_names = payroll_df["employee_name"].tolist()

    results = []
    used_indices: set[int] = set()

    for _, emp in payroll_df.iterrows():
        expected_net = emp["net_salary"]

        matched_amount = 0.0
        matched_refs = []
        matched_score = 0

        if not month_bank.empty:
            for idx, transfer in month_bank.iterrows():
                if idx in used_indices:
                    continue
                recipient = transfer.get("recipient", "")
                _, score = _best_match(emp["employee_name"], [recipient])
                if score >= FUZZY_THRESHOLD:
                    matched_amount += transfer["amount"]
                    matched_refs.append(transfer.get("purpose", "")[:60])
                    matched_score = max(matched_score, score)
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
            "status": status,
            "difference": difference,
            "difference_pct": diff_pct,
            "pending_amount": pending,
        })

    return pd.DataFrame(results)
