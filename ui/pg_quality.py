"""Page: invalid GSTINs, duplicates and rows excluded while cleaning."""
from __future__ import annotations

import pandas as pd
import streamlit as st

from modules.reports import DATA_QUALITY_COLUMNS, data_quality_frame, display_frame
from ui.state import as_text, require_result, show_table


def render() -> None:
    res = require_result()
    if res is None:
        return
    st.header("Data quality")
    reports = st.session_state["clean_reports"]
    if reports:
        c1, c2 = st.columns(2)
        for col, rep, title in ((c1, reports[0], "Books"), (c2, reports[1], res.portal_name)):
            col.markdown(f"**{title}**")
            for msg in rep.messages():
                col.markdown(f"- {msg}")
    tabs = st.tabs(["Invalid GSTINs", "Duplicates", "Rows without invoice number"])
    with tabs[0]:
        inv = display_frame(data_quality_frame(res, "invalid"), [c for c in DATA_QUALITY_COLUMNS if c.src != "dup_count"])
        st.caption("GSTINs failing length, state code, PAN structure or checksum validation. They are still "
                   "reconciled, but will rarely match the portal.")
        if inv.empty:
            st.success("All GSTINs are valid.")
        else:
            show_table(inv, key="dq_invalid")
    with tabs[1]:
        dup = display_frame(data_quality_frame(res, "duplicates"),
                            [c for c in DATA_QUALITY_COLUMNS if c.src != "gstin_error"])
        st.caption("Same GSTIN + document type + normalised invoice number more than once in the same dataset. "
                   "Only one copy can match; extra copies show as missing.")
        if dup.empty:
            st.success("No duplicates found.")
        else:
            show_table(dup, key="dq_dup")
    with tabs[2]:
        dropped = [r.dropped_no_invoice.assign(Dataset=r.dataset) for r in reports if len(r.dropped_no_invoice)]
        if dropped:
            st.dataframe(as_text(pd.concat(dropped, ignore_index=True)), hide_index=True)
        else:
            st.success("No rows were excluded." if reports else "Only available for runs made in this session.")
