"""Page: month-wise movement of pending documents (carry forward until they appear on the portal)."""
from __future__ import annotations

import streamlit as st

from modules.formatting import format_inr
from modules.periods import carry_forward_schedule, month_label
from modules.reports import carry_forward_sheet, display_frame, side_columns
from ui import charts
from ui.state import csv_download, filter_bar, require_result, run_months, short_name, show_table, with_recon


def render() -> None:
    res = require_result()
    if res is None:
        return
    short = short_name(res)
    months = run_months(res)
    st.header("Month-wise & carry forward")
    st.caption(f"Books documents are followed month by month until they appear in {short}. Documents still "
               f"missing at the end of this run are carried into the next run automatically (Reconcile → Period).")
    if not months:
        st.info("This run has no month information.")
        return
    recon = filter_bar(res.recon, "cf", res, statuses=False)
    schedule = carry_forward_schedule(with_recon(res, recon), months)
    last = schedule.iloc[-1]
    c = st.columns(4)
    c[0].metric("Period", f"{month_label(months[0])} – {month_label(months[-1])}")
    c[1].metric(f"Reflected in {short} (docs)", f"{int(schedule['reflected_count'].sum()):,}")
    c[2].metric("Still pending at end (docs)", f"{int(last['closing_count']):,}")
    c[3].metric("Pending ITC carried forward", format_inr(last["closing_itc"], decimals=0))
    c1, c2 = st.columns(2)
    c1.plotly_chart(charts.carry_forward(schedule, short), key="cf_chart")
    c2.plotly_chart(charts.closing_pending(schedule), key="cf_closing")

    frame, cols, _ = carry_forward_sheet(schedule, short)["Month-wise Carry Forward"]
    table = display_frame(frame, cols)
    show_table(table, key="cf_table")
    csv_download(table, "Download month-wise table (CSV)", "carry_forward.csv", "dl_cf")

    st.subheader(f"Documents still missing in {short}")
    pending = recon[recon["status"] == res.missing_portal_status].sort_values("months_pending", ascending=False)
    if pending.empty:
        st.success(f"Every books document is reflected in {short}.")
    else:
        show_table(display_frame(pending, side_columns(res, "books")), height=360, key="cf_pending")
