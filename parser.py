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
# Bank transfer parser — column-count flexible
# ---------------------------------------------------------------------------

# Headers we look for (lowercase, stripped). Multiple aliases per field.
_BANK_FIELD_ALIASES = {
    "booking_date": ["buchungstag", "buchungsdatum", "datum", "date", "valutadatum"],
    "recipient":    [
        "beguenstigter/zahlungspflichtiger",
        "beguenstigter",
        "zahlungspflichtiger",
        "kontoinhaber",
        "empfaenger",
        "empfänger",
        "name",
        "account holder",
        "recipient",
        "auftraggeber",
    ],
    "purpose":      ["verwendungszweck", "bemerkung", "betreff", "purpose",
                     "beschreibung", "buchungstext", "text", "referenz",
                     "zahlungsgrund", "mitteilung"],
    "amount":       ["betrag", "amount", "umsatz", "buchungsbetrag"],
    "iban":         ["kontonummer/iban", "iban", "kontonummer", "konto"],
}


def _detect_col(header_lower: str) -> str | None:
    """Return the logical field name for a column header string, or None."""
    h = header_lower.strip().strip('"').strip()
    for field, aliases in _BANK_FIELD_ALIASES.items():
        if h in aliases:
            return field
    # Partial match fallback
    for field, aliases in _BANK_FIELD_ALIASES.items():
        for alias in aliases:
            if alias in h or h in alias:
                return field
    return None


def parse_bank(filepath: str) -> pd.DataFrame:
    """
    Parse a German bank export CSV.
    Column count may vary between banks/exports — the parser detects
    columns by header name using a broad alias list.

    Returned columns:
      booking_date, booking_year, booking_month,
      recipient, purpose, row_text,
      amount, iban, year, month
    """
    rows = _read_raw(filepath)

    header_idx = None
    header_row = None
    for i, row in enumerate(rows):
        joined = " ".join(row).lower()
        if ("buchungstag" in joined or "verwendungszweck" in joined
                or "betrag" in joined or "buchungsdatum" in joined
                or "beguenstigter" in joined):
            header_idx = i
            header_row = row
            break

    if header_idx is None:
        return pd.DataFrame()

    # Map logical field → column index (first match wins)
    col_map: dict[str, int] = {}
    for j, h in enumerate(header_row):
        field = _detect_col(h.strip().lower().strip('"'))
        if field and field not in col_map:
            col_map[field] = j

    def _cell(row: list, field: str) -> str:
        idx = col_map.get(field)
        if idx is None or idx >= len(row):
            return ""
        return row[idx].strip().strip('"').strip()

    records = []
    for row in rows[header_idx + 1:]:
        if not row or not any(r.strip() for r in row):
            continue

        # Full row text for salary-keyword scanning (ALL columns)
        row_text = " ".join(c.strip().strip('"') for c in row if c.strip())

        booking_date = _cell(row, "booking_date")
        recipient    = _cell(row, "recipient")
        purpose      = _cell(row, "purpose")
        iban         = _cell(row, "iban")

        amount_raw = _cell(row, "amount") or "0"
        amount = _parse_german_number(amount_raw)

        # Month extraction: try purpose first, then full row text
        ym = extract_month_from_text(purpose) or extract_month_from_text(row_text)
        year  = ym[0] if ym else None
        month = ym[1] if ym else None

        # Parse booking date
        booking_year = booking_month = None
        if booking_date:
            bd_m = re.match(r"(\d{1,2})\.(\d{1,2})\.(\d{2,4})", booking_date)
            if bd_m:
                d_year = int(bd_m.group(3))
                if d_year < 100:
                    d_year += 2000
                booking_year  = d_year
                booking_month = int(bd_m.group(2))

        records.append({
            "booking_date":  booking_date,
            "booking_year":  booking_year,
            "booking_month": booking_month,
            "recipient":     recipient,
            "purpose":       purpose,
            "row_text":      row_text,   # full row — used for salary-keyword scan
            "amount":        amount,
            "iban":          iban,
            "year":          year,
            "month":         month,
        })

    return pd.DataFrame(records)

