"""
GST Reconciliation - Streamlit entry point.

Run with:  streamlit run app.py   (or start_app.bat)

Flow: first start -> create the admin account; afterwards -> sign in.
Pages live in the ``ui`` package (one module per page).
"""
from __future__ import annotations

import streamlit as st

from config import APP_TITLE
from ui import (
    pg_admin, pg_carry, pg_dashboard, pg_gstr3b, pg_history, pg_quality, pg_reconcile, pg_reports, pg_review,
    pg_suppliers, pg_vendors,
)
from ui.login import account_box, render_login, render_setup
from ui.state import base_result, current_user, get_auth, guarded, init_state, is_admin

st.set_page_config(page_title=APP_TITLE, page_icon="🧾", layout="wide")

PAGES = {
    "Reconcile": pg_reconcile.render,
    "Dashboard": pg_dashboard.render,
    "Review Probable Matches": pg_review.render,
    "Supplier Follow-up": pg_suppliers.render,
    "Month-wise & Carry Forward": pg_carry.render,
    "GSTR-3B Comparison": pg_gstr3b.render,
    "Vendor Master": pg_vendors.render,
    "Data Quality": pg_quality.render,
    "Reports": pg_reports.render,
    "History": pg_history.render,
}


def sidebar() -> str:
    with st.sidebar:
        st.title("🧾 GST Reconciliation")
        pages = list(PAGES) + (["Admin"] if is_admin() else [])
        page = st.radio("Go to", pages, label_visibility="collapsed", key="nav")
        st.divider()
        st.text_input("Your company name", key="company_name")
        st.text_input("Your GSTIN", key="company_gstin", max_chars=15,
                      help="Also used to find the previous run for carry forward.")
        res = base_result()
        if res is not None:
            meta = st.session_state["meta"]
            st.divider()
            st.caption("Loaded reconciliation")
            st.markdown(f"**{res.mode.title()} vs {res.portal_name}**  \n"
                        f"{meta.get('period_label') or 'No period label'}  \n"
                        f"Run #{st.session_state['run_id'] or '-'} · {res.created_at:%d-%m-%Y %H:%M}")
        st.divider()
        account_box()
    return page


def main() -> None:
    init_state()
    if not get_auth().has_users():
        render_setup()
        return
    if current_user() is None:
        render_login()
        return
    page = sidebar()
    guarded(pg_admin.render if page == "Admin" else PAGES[page])


main()
