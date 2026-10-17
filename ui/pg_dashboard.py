"""Page: headline figures, charts, filters and the detailed table."""
from __future__ import annotations

import streamlit as st

from config import describe_tolerance
from modules.formatting import format_inr
from modules.reports import display_frame, pair_columns
from ui import charts
from ui.state import csv_download, filter_bar, require_result, short_name, show_table, tax_word, with_recon


def render() -> None:
    res = require_result()
    if res is None:
        return
    short = short_name(res)
    meta = st.session_state["meta"]
    st.header(f"Dashboard — {res.mode.title()} Register vs {res.portal_name}")
    st.info(f"**Period:** {meta.get('period_label') or '-'}  ·  **Tolerance rule:** {describe_tolerance(res.settings)}"
            + (f" · fuzzy matching at score ≥ {res.settings.fuzzy_threshold:g}" if res.settings.enable_fuzzy
               else " · fuzzy matching off"), icon=":material/rule:")
    recon = filter_bar(res.recon, "dash", res)
    s = with_recon(res, recon).summary()
    if len(recon) < len(res.recon):
        st.caption(f"Filtered: {len(recon):,} of {len(res.recon):,} rows")

    def inr0(v: float) -> str:
        return format_inr(v, decimals=0)

    tw = tax_word(res)
    r1 = st.columns(5)
    r1[0].metric("Documents in books", f"{s['docs_books']:,}")
    r1[1].metric(f"Documents in {short}", f"{s['docs_portal']:,}")
    r1[2].metric(f"Net {tw} as per books", inr0(s["itc_books"]))
    r1[3].metric(f"Net {tw} as per {short}", inr0(s["itc_portal"]))
    r1[4].metric(f"Difference (Books − {short})", inr0(s["itc_books"] - s["itc_portal"]))
    r2 = st.columns(6)
    r2[0].metric("Matched", f"{s['matched_count']:,}", inr0(s["matched_itc"]), delta_color="off",
                 help=f"Exact + within tolerance + manually accepted; value = {tw} per portal")
    r2[1].metric("Mismatched", f"{s['mismatch_count']:,}", inr0(s["mismatch_itc_diff"]), delta_color="off",
                 help="ITC difference books − portal")
    r2[2].metric("Probable (review)", f"{s['probable_count']:,}")
    r2[3].metric(f"Missing in {short}", f"{s['missing_portal_count']:,}", inr0(s["missing_portal_itc"]),
                 delta_color="off")
    r2[4].metric("Missing in books", f"{s['missing_books_count']:,}", inr0(s["missing_books_itc"]), delta_color="off")
    if res.mode == "purchase":
        r2[5].metric("⚠️ ITC at risk", inr0(s["itc_at_risk"]),
                     help=f"ITC of documents missing in {short} + shortfall on mismatches (credit notes excluded)")
    else:
        r2[5].metric("⚠️ Tax not reported", inr0(s["itc_at_risk"]),
                     help=f"Tax on sales missing in {short} + under-reported mismatches (credit notes excluded)")
    notes = []
    carried = recon[recon["carried_from_books"] != ""] if "carried_from_books" in recon else recon.head(0)
    if len(carried):
        found = int(carried["has_portal"].sum())
        notes.append(f"{len(carried):,} document(s) carried forward from earlier periods - {found:,} now found in {short}")
    if s["itc_portal_not_available"]:
        notes.append(f"ITC marked 'not available' in {short}: {format_inr(s['itc_portal_not_available'])}")
    if notes:
        st.caption(" · ".join(notes))

    if recon.empty:
        st.warning("No rows match the selected filters.")
        return
    c1, c2 = st.columns(2)
    c1.plotly_chart(charts.status_counts(recon), key="ch_status")
    c2.plotly_chart(charts.itc_by_status(recon, short, tw), key="ch_itc_status")
    c3, c4 = st.columns(2)
    c3.plotly_chart(charts.itc_by_month(recon, short, tw), key="ch_month")
    top = charts.top_pending(res, recon)
    if top is not None:
        c4.plotly_chart(top, key="ch_top")
    else:
        c4.success("No ITC at risk for the selected filters. 🎉")

    st.subheader("Details")
    detail = display_frame(recon, pair_columns(res, with_score=True))
    show_table(detail.head(20_000), height=420, key="dash_table")
    if len(detail) > 20_000:
        st.caption("Showing the first 20,000 rows - download the CSV or the Excel report for everything.")
    csv_download(detail, "Download filtered rows (CSV)", "gst_recon_filtered.csv", "dl_dash_csv")
