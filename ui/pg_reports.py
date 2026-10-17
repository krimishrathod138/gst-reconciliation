"""Page: Excel workbook, PDF exports and quick CSVs."""
from __future__ import annotations

from datetime import datetime

import streamlit as st

from config import STATUS_MISMATCH, STATUS_MISSING_BOOKS
from modules.pdf_reports import summary_pdf, supplier_pdf
from modules.matcher import status_table
from modules.reports import build_excel_report, display_frame, pair_columns, side_columns
from ui.state import csv_download, filter_bar, report_extras, require_result, short_name, show_table, with_recon

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def render() -> None:
    res = require_result()
    if res is None:
        return
    short = short_name(res)
    meta = st.session_state["meta"]
    company, gstin, period = st.session_state["company_name"], st.session_state["company_gstin"], \
        meta.get("period_label", "")
    stem = f"{short}_{(period or 'report').replace(' ', '_').replace('/', '-')}_{datetime.now():%Y%m%d_%H%M}"
    st.header("Reports")

    with st.expander("Limit the reports to some suppliers / months (optional)"):
        recon = filter_bar(res.recon, "rep", res)
    filtered = len(recon) < len(res.recon)
    view = with_recon(res, recon) if filtered else res
    if filtered:
        st.caption(f"Reports will include {len(recon):,} of {len(res.recon):,} rows.")

    st.subheader("Excel workbook")
    extra = "GSTR-3B vs 2B vs Books, " if res.mode == "purchase" else ""
    st.markdown(
        f"Sheets: **Summary, Matched, Matched with Tolerance, Mismatched** (differences in red), **Probable Matches, "
        f"Missing in {short}, Missing in Books, Supplier-wise Summary, Duplicates, Invalid GSTINs, {extra}"
        "Month-wise Carry Forward, Review Audit Trail**. Indian ₹ lakh/crore formatting; review decisions, "
        "reviewers and remarks included.")
    if st.button("Generate Excel report", type="primary", icon=":material/table_view:"):
        with st.spinner("Building workbook…"):
            st.session_state["report_bytes"] = build_excel_report(view, company, gstin, period,
                                                                  extra_sheets=report_extras(view))
    if st.session_state["report_bytes"]:
        st.download_button("Download Excel report", st.session_state["report_bytes"],
                           file_name=f"GST_Reconciliation_{stem}.xlsx", mime=XLSX, icon=":material/download:",
                           type="primary")

    st.subheader("PDF")
    c1, c2 = st.columns([1, 2], vertical_alignment="center")
    pending_only = c2.toggle("Supplier-wise PDF: only suppliers with pending items", value=False)
    if c1.button("Generate PDFs", icon=":material/picture_as_pdf:"):
        with st.spinner("Building PDFs…"):
            st.session_state["pdf_bytes"] = {
                "summary": summary_pdf(view, company, gstin, period),
                "supplier": supplier_pdf(view, company, gstin, period, only_pending=pending_only)}
    pdfs = st.session_state.get("pdf_bytes")
    if pdfs:
        d1, d2 = st.columns(2)
        d1.download_button("Download Summary (PDF)", pdfs["summary"], file_name=f"Summary_{stem}.pdf",
                           mime="application/pdf", icon=":material/download:")
        d2.download_button("Download Supplier-wise (PDF)", pdfs["supplier"], file_name=f"Supplier_wise_{stem}.pdf",
                           mime="application/pdf", icon=":material/download:")

    st.subheader("Status summary")
    t = status_table(view.recon).rename(columns={
        "status": "Status", "documents": "Documents", "taxable_books": "Taxable (Books)", "itc_books": "ITC (Books)",
        "taxable_portal": f"Taxable ({short})", "itc_portal": f"ITC ({short})", "itc_difference": "ITC Difference"})
    show_table(t, key="rep_status")
    st.subheader("Quick exports (CSV)")
    cols = st.columns(3)
    r = view.recon
    groups = [("Missing in " + short, r[r["status"] == res.missing_portal_status], side_columns(res, "books")),
              ("Missing in Books", r[r["status"] == STATUS_MISSING_BOOKS], side_columns(res, "portal")),
              ("Mismatched", r[r["status"] == STATUS_MISMATCH], pair_columns(res))]
    for col, (name, frame, spec) in zip(cols, groups):
        with col:
            csv_download(display_frame(frame, spec), f"{name} ({len(frame):,})", f"{name.replace(' ', '_')}.csv",
                         f"dl_{name}")
