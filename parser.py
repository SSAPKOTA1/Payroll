"""Parse payroll and bank CSV files into DataFrames."""

import re
import csv
import pandas as pd
from pathlib import Path
from scanner import extract_month_from_text, _sniff_delimiter


# ---------------------------------------------------------------------------
# Payroll parser
# ---------------------------------------------------------------------------

def _read_raw(filepath: str) -> list[list[str]]:
    delim = _sniff_delimiter(filepath)
    rows = []
    with open(filepath, "r", encoding="utf-8-sig", errors="replace") as f:
        reader = csv.reader(f, delimiter=delim)
        for row in reader:
            rows.append(row)
    return rows


def _parse_german_number(val: str) -> float:
    """Convert '1.808,40' or '1808,40' or '1808.40' to float."""
    if not val or str(val).strip() == "":
        return 0.0
    val = str(val).strip()
    # Remove non-numeric except comma, dot, minus
    val = re.sub(r"[^\d,.\-]", "", val)
    if not val or val == "-":
        return 0.0
    # If both dot and comma present, the last one is the decimal separator
    if "," in val and "." in val:
        if val.rfind(",") > val.rfind("."):
            val = val.replace(".", "").replace(",", ".")
        else:
            val = val.replace(",", "")
    elif "," in val:
        val = val.replace(",", ".")
    try:
        return float(val)
    except ValueError:
        return 0.0


def parse_payroll(filepath: str, year: int = None, month: int = None) -> pd.DataFrame:
    """
    Parse a German payroll CSV (lojo format).
    Returns a DataFrame with columns:
      company, employee_id, employee_name, gross_salary, net_salary, year, month
    """
    rows = _read_raw(filepath)

    company = ""
    header_idx = None
    header_row = None

    for i, row in enumerate(rows):
        joined = " ".join(r.strip() for r in row if r.strip())
        # Company name is usually on line 0 or 1 (before the header)
        if i < 5 and joined and not company:
            # The company cell often contains "GmbH" or similar
            for cell in row:
                cell = cell.strip()
                if cell and len(cell) > 5 and ";" not in cell:
                    # skip obvious column header lines
                    if "Pers.-Nr" not in cell and "Summen" not in cell:
                        company = cell
                        break

        # Detect the column header row
        joined_lower = joined.lower()
        if "pers.-nr" in joined_lower or "gesamt-brutto" in joined_lower:
            header_idx = i
            header_row = row
            break

    if header_idx is None:
        return pd.DataFrame()

    # Map column names to indices
    col_map = {}
    for j, h in enumerate(header_row):
        h_lower = h.strip().lower()
        if h_lower in ("pers.-nr", "pers.nr", "pers.-nr."):
            col_map["employee_id"] = j
        elif h_lower == "name":
            col_map["employee_name"] = j
        elif h_lower == "gesamt-brutto":
            col_map["gross_salary"] = j
        elif h_lower == "auszahlungsbetrag":
            col_map["net_salary"] = j

    records = []
    for row in rows[header_idx + 1:]:
        if not row or not any(r.strip() for r in row):
            continue
        first = row[0].strip() if row else ""
        # Skip summary rows
        if not first or not re.match(r"^\d{5}$", first):
            continue

        emp_id = first
        emp_name = row[col_map["employee_name"]].strip() if "employee_name" in col_map else ""
        gross = _parse_german_number(row[col_map["gross_salary"]]) if "gross_salary" in col_map else 0.0
        net = _parse_german_number(row[col_map["net_salary"]]) if "net_salary" in col_map else 0.0

        records.append({
            "company": company,
            "employee_id": emp_id,
            "employee_name": emp_name,
            "gross_salary": gross,
            "net_salary": net,
            "year": year,
            "month": month,
        })

    return pd.DataFrame(records)


# ---------------------------------------------------------------------------
# Bank transfer parser
# ---------------------------------------------------------------------------

def parse_bank(filepath: str) -> pd.DataFrame:
    """
    Parse a German bank export CSV (Sparkasse/Nassauische Sparkasse format).
    Returns DataFrame with columns:
      booking_date, recipient, purpose, amount, iban, year, month
    """
    rows = _read_raw(filepath)

    header_idx = None
    header_row = None
    for i, row in enumerate(rows):
        joined = " ".join(row).lower()
        if "buchungstag" in joined or "verwendungszweck" in joined:
            header_idx = i
            header_row = row
            break

    if header_idx is None:
        return pd.DataFrame()

    col_map = {}
    for j, h in enumerate(header_row):
        h_lower = h.strip().lower().strip('"')
        if h_lower == "buchungstag":
            col_map["booking_date"] = j
        elif h_lower in ("beguenstigter/zahlungspflichtiger",):
            col_map["recipient"] = j
        elif h_lower == "verwendungszweck":
            col_map["purpose"] = j
        elif h_lower == "betrag":
            col_map["amount"] = j
        elif h_lower in ("kontonummer/iban",):
            col_map["iban"] = j

    records = []
    for row in rows[header_idx + 1:]:
        if not row or not any(r.strip() for r in row):
            continue
        amount_raw = row[col_map["amount"]].strip().strip('"') if "amount" in col_map else "0"
        amount = _parse_german_number(amount_raw)

        purpose = row[col_map["purpose"]].strip().strip('"') if "purpose" in col_map else ""
        recipient = row[col_map["recipient"]].strip().strip('"') if "recipient" in col_map else ""
        booking_date = row[col_map["booking_date"]].strip().strip('"') if "booking_date" in col_map else ""
        iban = row[col_map["iban"]].strip().strip('"') if "iban" in col_map else ""

        ym = extract_month_from_text(purpose)
        year = ym[0] if ym else None
        month = ym[1] if ym else None

        records.append({
            "booking_date": booking_date,
            "recipient": recipient,
            "purpose": purpose,
            "amount": amount,
            "iban": iban,
            "year": year,
            "month": month,
        })

    return pd.DataFrame(records)
