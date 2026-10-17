"""Page: GSTR-3B comparison - ITC claimed in 3B vs available in 2B vs books, month by month."""
from __future__ import annotations

import pandas as pd
import streamlit as st

from modules import ReconError
from modules.formatting import format_inr
from modules.gstr3b import GSTR3B_COLUMNS, HEADS, combine_gstr3b, compare_gstr3b, parse_gstr3b_json, \
    parse_gstr3b_table
from modules.loader import load_file
from modules.periods import month_label
from modules.reports import display_frame, gstr3b_sheet
from ui import charts
from ui.state import csv_download, get_db, money_config, require_result, run_months, show_table


def _load_uploads(files) -> pd.DataFrame:
    frames = []
    for f in files:
        if f.name.lower().endswith(".json"):
            frames.append(parse_gstr3b_json(f.getvalue()))
        else:
            raw = load_file(f.getvalue(), name=f.name, dataset="gstr3b").frame.drop(columns="__source_row__",
                                                                                   errors="ignore")
            frames.append(parse_gstr3b_table(raw, source=f.name))
    return combine_gstr3b(frames)


def render() -> None:
    res = require_result()
    if res is None:
        return
    st.header("GSTR-3B comparison")
    if res.mode != "purchase":
        st.info("The GSTR-3B ITC comparison applies to purchase reconciliations (Purchase Register vs GSTR-2B/2A).")
        return
    st.caption("Compares ITC claimed in GSTR-3B Table **4A(5) – All other ITC** (the part fed by GSTR-2B's B2B and "
               "credit/debit notes) with ITC available in GSTR-2B (by return period) and ITC in books (by invoice "
               "month). A month where 3B is higher than 2B is flagged **Excess claimed** (Sec. 16(2)(aa) / Rule 36(4)).")
    months = run_months(res)
    current = st.session_state.get("gstr3b")
    run_id = st.session_state["run_id"]
    if current is None and run_id:
        current = get_db().load_gstr3b(run_id)
        st.session_state["gstr3b"] = current

    with st.expander("Enter GSTR-3B figures", expanded=current is None or current.empty):
        up = st.file_uploader("GSTR-3B JSON (one per month) or an Excel/CSV with Month, IGST, CGST, SGST, Cess",
                              type=["json", "xlsx", "xls", "csv"], accept_multiple_files=True, key="g3b_upload")
        if up and st.button("Read files", icon=":material/upload_file:"):
            try:
                st.session_state["gstr3b"] = combine_gstr3b([current, _load_uploads(up)])
                if run_id:
                    get_db().save_gstr3b(run_id, st.session_state["gstr3b"])
                st.session_state["report_bytes"] = None
                st.rerun()
            except ReconError as exc:
                st.error(str(exc))
        st.markdown("**Or type them in** (4A(5) ITC claimed, ₹):")
        base = current if current is not None and len(current) else pd.DataFrame(
            {"period": months, **{h: 0.0 for h in HEADS}})
        manual = pd.DataFrame({"Month": base["period"].map(month_label),
                               **{h.upper(): base[h].astype(float) for h in HEADS}})
        edited = st.data_editor(manual, hide_index=True, num_rows="dynamic", key="g3b_editor",
                                column_config=money_config(manual))
        c1, c2 = st.columns(2)
        if c1.button("Use and save these figures", type="primary", icon=":material/save:"):
            try:
                table = parse_gstr3b_table(edited, source="manual")
                st.session_state["gstr3b"] = combine_gstr3b([table])
                if run_id:
                    get_db().save_gstr3b(run_id, st.session_state["gstr3b"])
                st.session_state["report_bytes"] = None
                st.success("GSTR-3B figures saved with this run.")
                st.rerun()
            except ReconError as exc:
                st.error(str(exc))
        if c2.button("Clear 3B figures"):
            st.session_state["gstr3b"] = pd.DataFrame(columns=GSTR3B_COLUMNS)
            if run_id:
                get_db().save_gstr3b(run_id, st.session_state["gstr3b"])
            st.rerun()

    comparison = compare_gstr3b(res, st.session_state.get("gstr3b"), months or None)
    total = comparison.iloc[-1]
    c = st.columns(4)
    c[0].metric("ITC in books", format_inr(total["books_total"], decimals=0))
    c[1].metric("ITC available in 2B", format_inr(total["gstr2b_total"], decimals=0))
    c[2].metric("ITC claimed in 3B 4A(5)", format_inr(total["gstr3b_total"], decimals=0))
    c[3].metric("3B − 2B", format_inr(total["diff_3b_2b_total"], decimals=0), total["status"], delta_color="off")
    excess = comparison[(comparison["month"] != "TOTAL") & (comparison["status"] == "Excess claimed")]
    if len(excess):
        st.warning("Excess ITC claimed over GSTR-2B in: " + ", ".join(excess["month"].map(month_label))
                   + ". Review for reversal / interest exposure.", icon=":material/warning:")
    st.plotly_chart(charts.gstr3b_months(comparison), key="g3b_chart")
    frame, cols, _ = gstr3b_sheet(comparison)["GSTR-3B vs 2B vs Books"]
    table = display_frame(frame, cols)
    show_table(table, key="g3b_table")
    csv_download(table, "Download comparison (CSV)", "gstr3b_comparison.csv", "dl_g3b")
    st.caption("The comparison is also a sheet in the Excel report.")
