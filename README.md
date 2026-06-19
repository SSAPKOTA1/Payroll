# Payroll Tracking Dashboard

A Streamlit-based payroll tracking app that automatically scans folders for payroll and bank CSV files, matches transfers to employees, and tracks payment status with manual override support.

## Project Structure

```
Payroll/
├── app.py          # Main Streamlit dashboard
├── scanner.py      # Recursive folder scanning & file detection
├── parser.py       # CSV parsing (payroll + bank statement)
├── matcher.py      # Fuzzy name matching & payment status logic
├── db.py           # SQLite-based manual overrides
├── requirements.txt
└── overrides.db    # Created automatically on first run
```

## Setup

```bash
# 1. Create and activate a virtual environment (optional but recommended)
python -m venv .venv
source .venv/bin/activate        # Linux/macOS
.venv\Scripts\activate           # Windows

# 2. Install dependencies
pip install -r requirements.txt

# 3. Run the app
streamlit run app.py
```

The app opens at http://localhost:8501 in your browser.

## How to Use

1. **Set the root directory** in the sidebar (default: `D:\` on Windows).  
   The app scans all subfolders recursively.
2. Click **Scan / Refresh** after adding new files.
3. **Select a month** — the most recent payroll month is loaded automatically.
4. **Select a company** — extracted from the payroll CSV header.
5. The dashboard shows summary metrics and a per-employee payment table.
6. Click any employee in the **Employee Detail** section to:
   - View auto-matched bank transfer status
   - Override gross/net salary
   - Override the paid amount (or tick "Use Net Salary as Paid")
   - Force-override the payment status
   - View payroll history

## File Detection Rules

| File type | Detection rule |
|-----------|---------------|
| Payroll   | Filename matches `lojo_YYYYMM_*.csv` **or** CSV contains `Pers.-Nr` / `Gesamt-Brutto` headers |
| Bank      | Filename contains `umsatz` **or** CSV contains `Buchungstag` / `Verwendungszweck` headers |

## Payroll File Naming Convention

```
lojo_202605_0001595_00419_00000.csv
      ^^^^^^
      YYYYMM → May 2026
```

## Bank Transfer Month Extraction

The app parses the `Verwendungszweck` (purpose) field and detects patterns such as:

- `Gehalt Januar 2026`
- `Salary 01/2026`
- `Gehalt 2026-01`
- `Salary Jan 26`

## Payment Status Logic

| Condition | Status |
|-----------|--------|
| paid = 0 | Not Paid |
| 0 < paid < net (±5%) | Underpaid |
| paid ≈ net (±2%) | Paid |
| net < paid < net×2 | Overpaid |
| paid ≈ net×2 | Double Paid |
| paid > net×2 | Multiple Overpayments |
| Small rounding diff | Partially Paid |
