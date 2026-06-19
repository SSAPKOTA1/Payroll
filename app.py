"""
Payroll Tracking Dashboard — Streamlit application.

Run with:
    streamlit run app.py
"""

import os
import calendar
import pandas as pd
import streamlit as st
from pathlib import Path

from scanner import scan_directory
from parser import parse_payroll, parse_bank
from matcher import match_transfers, compute_status, _transfer_is_candidate
from db import init_db, get_override, set_override, clear_override, get_all_overrides_for_month

# ---------------------------------------------------------------------------
# Page config
# ---------------------------------------------------------------------------
st.set_page_config(
    page_title="Payroll Dashboard",
    page_icon="💼",
    layout="wide",
)

init_db()

# ---------------------------------------------------------------------------
# Caching
# ---------------------------------------------------------------------------
@st.cache_data(show_spinner="Scanning folders…")
def load_all_files(root: str):
    return scan_directory(root)


@st.cache_data(show_spinner="Parsing payroll…")
def load_payroll(path: str, year: int, month: int):
    return parse_payroll(path, year, month)


@st.cache_data(show_spinner="Parsing bank statement…")
def load_bank(path: str):
    return parse_bank(path)


# ---------------------------------------------------------------------------
# Sidebar — configuration
# ---------------------------------------------------------------------------
st.sidebar.title("⚙️ Configuration")

default_root = r"D:\\"
root_dir = st.sidebar.text_input("Root scan directory", value=default_root)

scan_btn = st.sidebar.button("🔍 Scan / Refresh", use_container_width=True)
if scan_btn:
    st.cache_data.clear()

if not os.path.isdir(root_dir):
    st.warning(f"Directory not found: `{root_dir}`  \nPlease set a valid root directory in the sidebar.")
    st.stop()

files = load_all_files(root_dir)
payroll_entries = sorted(
    [e for e in files["payroll"] if e["year"] and e["month"]],
    key=lambda e: (e["year"], e["month"]),
)
bank_entries = files["bank"]

if not payroll_entries:
    st.error("No payroll files detected. Ensure your files match the lojo_YYYYMM_*.csv pattern or contain German payroll headers.")
    st.stop()

# ---------------------------------------------------------------------------
# Group payroll files by (year, month) — a single month may have multiple
# files (e.g. one per company / department).
# ---------------------------------------------------------------------------
from collections import defaultdict
month_to_entries: dict = defaultdict(list)
for e in payroll_entries:
    month_to_entries[(e["year"], e["month"])].append(e)

# Unique sorted month keys
unique_months = sorted(month_to_entries.keys())

# ---------------------------------------------------------------------------
# Month selector
# ---------------------------------------------------------------------------
month_labels = [f"{calendar.month_name[m]} {y}" for y, m in unique_months]
default_idx = len(month_labels) - 1  # most recent

selected_month_label = st.sidebar.selectbox(
    "Select payroll month", month_labels, index=default_idx
)
sel_year, sel_month = unique_months[month_labels.index(selected_month_label)]

# ---------------------------------------------------------------------------
# Load ALL payroll files for the selected month and merge
# ---------------------------------------------------------------------------
entries_for_month = month_to_entries[(sel_year, sel_month)]

@st.cache_data(show_spinner="Parsing payroll files…")
def load_payroll_for_month(paths_years_months: list) -> pd.DataFrame:
    frames = []
    for path, year, month in paths_years_months:
        df = parse_payroll(path, year, month)
        if not df.empty:
            df["source_file"] = os.path.basename(path)
            frames.append(df)
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)

# Pass as a list of tuples (hashable for cache key)
payroll_df_raw = load_payroll_for_month(
    [(e["path"], e["year"], e["month"]) for e in entries_for_month]
)

if payroll_df_raw.empty:
    st.error(f"Could not parse any payroll file for {selected_month_label}.")
    st.stop()

companies = sorted(payroll_df_raw["company"].unique().tolist())
selected_company = st.sidebar.selectbox("Select company", companies)
payroll_df = payroll_df_raw[payroll_df_raw["company"] == selected_company].copy()

# ---------------------------------------------------------------------------
# Load ALL bank statements (always merged across all files)
# ---------------------------------------------------------------------------
@st.cache_data(show_spinner="Parsing bank statements…")
def load_all_bank(paths: tuple) -> pd.DataFrame:
    frames = []
    for path in paths:
        bdf = parse_bank(path)
        if not bdf.empty:
            bdf["source_file"] = os.path.basename(path)
            frames.append(bdf)
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)

bank_df = load_all_bank(tuple(e["path"] for e in bank_entries))

# ---------------------------------------------------------------------------
# Apply overrides to payroll before matching
# ---------------------------------------------------------------------------
overrides = get_all_overrides_for_month(selected_company, sel_year, sel_month)

for i, row in payroll_df.iterrows():
    ov = overrides.get(row["employee_id"], {})
    if ov.get("gross_override") is not None:
        payroll_df.at[i, "gross_salary"] = ov["gross_override"]
    if ov.get("net_override") is not None:
        payroll_df.at[i, "net_salary"] = ov["net_override"]

# ---------------------------------------------------------------------------
# Match transfers
# ---------------------------------------------------------------------------
result_df = match_transfers(payroll_df, bank_df, sel_year, sel_month)

# Apply paid_amount_override and paid_override after matching
for i, row in result_df.iterrows():
    emp_id = row["employee_id"]
    ov = overrides.get(emp_id, {})
    if ov.get("paid_amount_override") is not None:
        result_df.at[i, "paid_amount"] = ov["paid_amount_override"]
    if ov.get("paid_override") == 1:
        result_df.at[i, "paid_amount"] = row["net_salary"]
    elif ov.get("paid_override") == 0:
        result_df.at[i, "paid_amount"] = 0.0
    paid = result_df.at[i, "paid_amount"]
    expected = result_df.at[i, "net_salary"]
    result_df.at[i, "status"] = compute_status(paid, expected)
    result_df.at[i, "difference"] = paid - expected
    result_df.at[i, "difference_pct"] = ((paid - expected) / expected * 100) if expected else 0.0
    result_df.at[i, "pending_amount"] = max(0.0, expected - paid)


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------
STATUS_COLOR = {
    "Paid": "green",
    "Not Paid": "red",
    "Underpaid": "orange",
    "Overpaid": "orange",
    "Double Paid": "red",
    "Multiple Overpayments": "red",
    "Partially Paid": "orange",
}

def status_badge(status: str) -> str:
    color = STATUS_COLOR.get(status, "grey")
    return f":{color}[**{status}**]"


# ---------------------------------------------------------------------------
# TABS
# ---------------------------------------------------------------------------
st.title(f"💼 Payroll Dashboard — {selected_month_label}")
st.caption(f"Company: **{selected_company}**")

tab_main, tab_bank, tab_files = st.tabs(["📊 Dashboard", "🏦 Bank Statement", "📁 Detected Files"])

# ===========================================================================
# TAB 1: MAIN DASHBOARD
# ===========================================================================
with tab_main:
    # Summary metrics
    total_employees = len(result_df)
    total_gross = result_df["gross_salary"].sum()
    total_net = result_df["net_salary"].sum()
    total_paid = result_df["paid_amount"].sum()
    total_pending = result_df["pending_amount"].sum()
    status_counts = result_df["status"].value_counts()

    col1, col2, col3, col4, col5 = st.columns(5)
    col1.metric("Employees", total_employees)
    col2.metric("Total Gross", f"€{total_gross:,.2f}")
    col3.metric("Total Net", f"€{total_net:,.2f}")
    col4.metric("Total Paid", f"€{total_paid:,.2f}")
    col5.metric("Pending", f"€{total_pending:,.2f}")

    col6, col7, col8, col9 = st.columns(4)
    col6.metric("Paid ✅", status_counts.get("Paid", 0))
    col7.metric("Not Paid ❌", status_counts.get("Not Paid", 0))
    col8.metric("Underpaid ⚠️", int(status_counts.get("Underpaid", 0)) + int(status_counts.get("Partially Paid", 0)))
    col9.metric("Overpaid / Double 🔴",
                int(status_counts.get("Overpaid", 0)) + int(status_counts.get("Double Paid", 0)) +
                int(status_counts.get("Multiple Overpayments", 0)))

    st.divider()

    # Employee overview table
    st.subheader("Employee Payment Overview")

    display_cols = ["employee_id", "employee_name", "gross_salary", "net_salary",
                    "paid_amount", "difference", "pending_amount", "status"]

    def fmt(df):
        out = df[display_cols].copy()
        for c in ["gross_salary", "net_salary", "paid_amount", "difference", "pending_amount"]:
            out[c] = out[c].map(lambda x: f"€{x:,.2f}")
        out.columns = ["ID", "Name", "Gross", "Net", "Paid", "Difference", "Pending", "Status"]
        return out

    st.dataframe(fmt(result_df), use_container_width=True, hide_index=True)

    st.divider()

    # Employee detail & override
    st.subheader("Employee Detail & Manual Override")

    emp_options = result_df["employee_name"].tolist()
    if not emp_options:
        st.info("No employees found.")
    else:
        selected_emp_name = st.selectbox("Select employee", emp_options)
        emp_row = result_df[result_df["employee_name"] == selected_emp_name].iloc[0]
        emp_id = emp_row["employee_id"]
        ov = overrides.get(emp_id, {})

        col_a, col_b = st.columns(2)

        with col_a:
            st.markdown("#### Current Values")
            st.write(f"**Employee ID:** {emp_id}")
            st.write(f"**Status:** {status_badge(emp_row['status'])}")
            st.write(f"**Gross Salary:** €{emp_row['gross_salary']:,.2f}")
            st.write(f"**Net Salary:** €{emp_row['net_salary']:,.2f}")
            st.write(f"**Amount Paid:** €{emp_row['paid_amount']:,.2f}")
            st.write(f"**Difference:** €{emp_row['difference']:,.2f} ({emp_row['difference_pct']:.1f}%)")
            st.write(f"**Pending:** €{emp_row['pending_amount']:,.2f}")
            if emp_row.get("bank_reference"):
                st.write(f"**Bank Reference:** {emp_row['bank_reference']}")
            if emp_row.get("match_reason"):
                st.caption(f"Match reason: {emp_row['match_reason']}")

        with col_b:
            st.markdown("#### Manual Override")

            with st.form(f"override_form_{emp_id}"):
                new_gross = st.number_input(
                    "Gross Salary Override",
                    value=float(ov.get("gross_override") or emp_row["gross_salary"]),
                    min_value=0.0, step=0.01, format="%.2f"
                )
                new_net = st.number_input(
                    "Net Salary Override",
                    value=float(ov.get("net_override") or emp_row["net_salary"]),
                    min_value=0.0, step=0.01, format="%.2f"
                )

                use_net_as_paid = st.checkbox(
                    "✔ Use Net Salary as Amount Paid",
                    value=(ov.get("paid_amount_override") is not None and
                           abs(ov.get("paid_amount_override", 0) - emp_row["net_salary"]) < 0.01)
                )

                new_paid = st.number_input(
                    "Amount Paid Override",
                    value=float(ov.get("paid_amount_override") or emp_row["paid_amount"]),
                    min_value=0.0, step=0.01, format="%.2f",
                    disabled=use_net_as_paid,
                )

                paid_override_opt = st.radio(
                    "Payment Status Override",
                    options=["Auto (from bank data)", "Force Paid", "Force Not Paid"],
                    index=(1 if ov.get("paid_override") == 1 else
                           2 if ov.get("paid_override") == 0 else 0),
                )

                submitted = st.form_submit_button("💾 Save Override", use_container_width=True)
                clear_btn = st.form_submit_button("🗑 Clear Override", use_container_width=True)

                if submitted:
                    paid_ov_val = None
                    if paid_override_opt == "Force Paid":
                        paid_ov_val = 1
                    elif paid_override_opt == "Force Not Paid":
                        paid_ov_val = 0
                    paid_amount_ov = new_net if use_net_as_paid else new_paid
                    set_override(
                        selected_company, emp_id, sel_year, sel_month,
                        gross_override=new_gross,
                        net_override=new_net,
                        paid_amount_override=paid_amount_ov,
                        paid_override=paid_ov_val,
                    )
                    st.success("Override saved. Refresh to see updated values.")
                    st.cache_data.clear()

                if clear_btn:
                    clear_override(selected_company, emp_id, sel_year, sel_month)
                    st.info("Override cleared.")
                    st.cache_data.clear()

        # Payroll history
        st.divider()
        st.subheader(f"Payroll History — {selected_emp_name}")
        history_rows = []
        for (hy, hm), h_entries in sorted(month_to_entries.items()):
            df_raw = load_payroll_for_month(
                [(e["path"], e["year"], e["month"]) for e in h_entries]
            )
            if df_raw.empty:
                continue
            emp_hist = df_raw[(df_raw["employee_id"] == emp_id) &
                              (df_raw["company"] == selected_company)]
            if not emp_hist.empty:
                r = emp_hist.iloc[0]
                ov_h = get_override(selected_company, emp_id, hy, hm)
                gross = ov_h.get("gross_override") or r["gross_salary"]
                net = ov_h.get("net_override") or r["net_salary"]
                history_rows.append({
                    "Month": f"{calendar.month_name[hm]} {hy}",
                    "Gross": f"€{gross:,.2f}",
                    "Net": f"€{net:,.2f}",
                    "Files": len(h_entries),
                })
        if history_rows:
            st.dataframe(pd.DataFrame(history_rows), use_container_width=True, hide_index=True)
        else:
            st.info("No history found for this employee.")


# ===========================================================================
# TAB 2: BANK STATEMENT DIAGNOSTICS
# ===========================================================================
with tab_bank:
    st.subheader("Bank Statement — All Loaded Transfers")

    if bank_df.empty:
        st.warning("No bank statement files detected. Check that your bank CSV files are under the scan root and contain 'Buchungstag'/'Verwendungszweck' headers (or 'umsatz' in the filename).")
    else:
        from matcher import is_salary_transfer as _is_salary
        diag = bank_df.copy()
        diag["is_salary"] = diag["purpose"].apply(lambda p: _is_salary(str(p)))
        diag["candidate_for_month"] = diag.apply(
            lambda r: _transfer_is_candidate(r, sel_year, sel_month), axis=1
        )
        diag["amount_fmt"] = diag["amount"].map(lambda x: f"€{x:,.2f}")
        diag["abs_amount_fmt"] = diag["amount"].map(lambda x: f"€{abs(x):,.2f}")

        n_salary = diag["is_salary"].sum()
        n_candidates = diag["candidate_for_month"].sum()
        n_salary_candidates = (diag["is_salary"] & diag["candidate_for_month"]).sum()

        st.success(
            f"**{len(bank_df)} transfers loaded** from {len(bank_entries)} bank file(s) · "
            f"**{n_salary} salary transfers** (Gehalt/Lohn in purpose) · "
            f"**{n_salary_candidates} salary candidates** for {selected_month_label}"
        )

        if n_salary == 0:
            st.warning(
                "No transfers with **Gehalt / Lohn / Salary** in the purpose were found. "
                "If your bank export uses a different keyword, the app falls back to matching "
                "all candidate transfers by name. Check the 'Purpose' column below."
            )

        show_cols = ["source_file", "booking_date", "recipient", "purpose",
                     "amount_fmt", "abs_amount_fmt", "is_salary",
                     "booking_month", "booking_year", "month", "year",
                     "candidate_for_month"]
        available = [c for c in show_cols if c in diag.columns]
        st.dataframe(
            diag[available].rename(columns={
                "source_file": "File",
                "booking_date": "Date",
                "recipient": "Account Holder (Recipient)",
                "purpose": "Purpose (Verwendungszweck)",
                "amount_fmt": "Amount (raw)",
                "abs_amount_fmt": "Amount (abs)",
                "is_salary": "Salary Transfer?",
                "booking_month": "Bk.Month",
                "booking_year": "Bk.Year",
                "month": "Purpose Month",
                "year": "Purpose Year",
                "candidate_for_month": "Candidate?",
            }),
            use_container_width=True, hide_index=True
        )

        st.divider()
        st.subheader("Fuzzy match scores — employees vs salary transfers")
        st.markdown(
            "Shows how each payroll employee name scores against the **Account Holder** "
            "(Beguenstigter) and **Purpose** fields of every salary-candidate transfer. "
            "Threshold for a match is **72**."
        )

        if not payroll_df.empty:
            salary_cands = diag[diag["candidate_for_month"]].reset_index(drop=True)
            # If salary-tagged rows exist, show only those; else show all candidates
            if diag["is_salary"].any():
                salary_cands = diag[diag["is_salary"] & diag["candidate_for_month"]].reset_index(drop=True)

            if salary_cands.empty:
                st.info("No salary-candidate transfers to score against.")
            else:
                from matcher import _score as _fscore
                score_rows = []
                for _, emp in payroll_df.iterrows():
                    for _, tr in salary_cands.iterrows():
                        sr = _fscore(emp["employee_name"], str(tr.get("recipient", "")))
                        sp = _fscore(emp["employee_name"], str(tr.get("purpose", "")))
                        best = max(sr, sp)
                        score_rows.append({
                            "Employee": emp["employee_name"],
                            "Account Holder": str(tr.get("recipient", ""))[:50],
                            "Purpose": str(tr.get("purpose", ""))[:50],
                            "Score (holder)": sr,
                            "Score (purpose)": sp,
                            "Best Score": best,
                            "Matched?": "✅" if best >= 72 else "❌",
                        })
                score_df = pd.DataFrame(score_rows).sort_values("Best Score", ascending=False)
                st.dataframe(score_df, use_container_width=True, hide_index=True)


# ===========================================================================
# TAB 3: DETECTED FILES
# ===========================================================================
with tab_files:
    st.subheader("Detected Files")
    c1, c2 = st.columns(2)
    with c1:
        st.markdown(f"**Payroll files ({len(files['payroll'])})**")
        for (hy, hm), h_entries in sorted(month_to_entries.items()):
            label = f"{calendar.month_name[hm]} {hy}"
            st.markdown(f"**{label}** — {len(h_entries)} file(s)")
            for e in h_entries:
                st.caption(f"  📄 {e['path']}")
        if [e for e in files["payroll"] if not e.get("month")]:
            st.markdown("**Unknown month**")
            for e in files["payroll"]:
                if not e.get("month"):
                    st.caption(f"  📄 {e['path']}")
    with c2:
        st.markdown(f"**Bank files ({len(files['bank'])})**")
        for e in files["bank"]:
            st.caption(f"🏦 {e['path']}")

# ---------------------------------------------------------------------------
# Sidebar — quick stats
# ---------------------------------------------------------------------------
st.sidebar.divider()
st.sidebar.markdown("**Detected files**")
st.sidebar.write(f"Payroll: {len(files['payroll'])}  |  Bank: {len(files['bank'])}")
st.sidebar.write(f"Bank transfers loaded: {len(bank_df) if not bank_df.empty else 0}")
