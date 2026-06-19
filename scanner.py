"""Recursive file scanner and file-type detector."""

import os
import re
import csv
from pathlib import Path
from typing import Optional

# Payroll files produced by LOHN-/GEHALT software (German payroll)
# Filename pattern: lojo_YYYYMM_<company>_<dept>_<seq>.csv
PAYROLL_PATTERN = re.compile(r"lojo_(\d{6})_", re.IGNORECASE)

# Bank export filenames often contain "umsatz" (German: turnover/transaction)
BANK_PATTERN = re.compile(r"umsatz", re.IGNORECASE)

# Month names for extraction from content/purpose strings
MONTH_NAMES_DE = {
    "januar": 1, "februar": 2, "märz": 3, "maerz": 3, "april": 4,
    "mai": 5, "juni": 6, "juli": 7, "august": 8, "september": 9,
    "oktober": 10, "november": 11, "dezember": 12,
}
MONTH_NAMES_EN = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5,
    "june": 6, "july": 7, "august": 8, "september": 9,
    "october": 10, "november": 11, "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4,
    "jun": 6, "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


def extract_month_from_filename(filename: str) -> Optional[tuple[int, int]]:
    """Return (year, month) from a payroll filename, or None."""
    m = PAYROLL_PATTERN.search(filename)
    if m:
        yyyymm = m.group(1)
        year, month = int(yyyymm[:4]), int(yyyymm[4:])
        if 1 <= month <= 12:
            return year, month
    return None


def extract_month_from_text(text: str) -> Optional[tuple[int, int]]:
    """
    Try to extract (year, month) from a free-text string such as a
    transfer purpose or CSV header cell.
    Handles patterns like:
      01/2025   2025-01   Jan 2025   Januar 2025   Gehalt 01.2025
    """
    text_lower = text.lower()

    # Named month (German or English)
    for name, num in {**MONTH_NAMES_DE, **MONTH_NAMES_EN}.items():
        if name in text_lower:
            year_m = re.search(r"\b(20\d{2})\b", text)
            if year_m:
                return int(year_m.group(1)), num

    # Numeric patterns
    patterns = [
        r"\b(20\d{2})[-./](\d{1,2})\b",   # 2025-01
        r"\b(\d{1,2})[-./](20\d{2})\b",    # 01/2025
        r"\b(\d{1,2})[-./](\d{2})\b",      # 01/25
    ]
    for pat in patterns:
        m = re.search(pat, text)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            if a > 12:  # year first
                if 1 <= b <= 12:
                    return a, b
            elif b > 31:  # year second (4-digit)
                if 1 <= a <= 12:
                    return b, a
            else:  # both small → month/year where year is 2-digit
                if 1 <= a <= 12:
                    return 2000 + b, a
    return None


def _sniff_delimiter(filepath: str) -> str:
    with open(filepath, "r", encoding="utf-8-sig", errors="replace") as f:
        sample = f.read(4096)
    for delim in [";", ",", "\t"]:
        if sample.count(delim) > 2:
            return delim
    return ","


def is_payroll_file(filepath: str) -> bool:
    name = Path(filepath).name
    if PAYROLL_PATTERN.search(name):
        return True
    try:
        delim = _sniff_delimiter(filepath)
        with open(filepath, "r", encoding="utf-8-sig", errors="replace") as f:
            reader = csv.reader(f, delimiter=delim)
            for i, row in enumerate(reader):
                if i > 5:
                    break
                row_text = " ".join(row).lower()
                if "pers.-nr" in row_text or "gesamt-brutto" in row_text or "auszahlungsbetrag" in row_text:
                    return True
    except Exception:
        pass
    return False


def is_bank_file(filepath: str) -> bool:
    name = Path(filepath).name
    if BANK_PATTERN.search(name):
        return True
    try:
        delim = _sniff_delimiter(filepath)
        with open(filepath, "r", encoding="utf-8-sig", errors="replace") as f:
            reader = csv.reader(f, delimiter=delim)
            for i, row in enumerate(reader):
                if i > 3:
                    break
                row_text = " ".join(row).lower()
                if "buchungstag" in row_text or "auftragskonto" in row_text or "verwendungszweck" in row_text:
                    return True
    except Exception:
        pass
    return False


def scan_directory(root: str) -> dict:
    """
    Recursively scan root for payroll and bank CSV files.
    Returns:
      {
        "payroll": [{"path": ..., "year": ..., "month": ...}, ...],
        "bank":    [{"path": ..., "year": ..., "month": ...}, ...],
      }
    """
    payroll_files = []
    bank_files = []

    for dirpath, _, filenames in os.walk(root):
        for fname in filenames:
            if not fname.lower().endswith(".csv"):
                continue
            full = os.path.join(dirpath, fname)
            if is_payroll_file(full):
                ym = extract_month_from_filename(fname)
                payroll_files.append({"path": full, "year": ym[0] if ym else None,
                                       "month": ym[1] if ym else None})
            elif is_bank_file(full):
                bank_files.append({"path": full, "year": None, "month": None})

    return {"payroll": payroll_files, "bank": bank_files}
