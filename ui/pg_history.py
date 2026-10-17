"""Page: saved reconciliation runs."""
from __future__ import annotations

import streamlit as st

from ui.state import get_db, is_admin, set_result, show_table


def render() -> None:
    st.header("Reconciliation history")
    db = get_db()
    runs = db.list_runs()
    if runs.empty:
        st.info("No runs saved yet.")
        return
    fys = sorted({f for f in runs["fy"] if f}, reverse=True)
    if fys:
        pick = st.multiselect("Financial year", fys, placeholder="All")
        if pick:
            runs = runs[runs["fy"].isin(pick)]
    show_table(runs, key="hist_table")
    labels = {r.run_id: f"#{r.run_id} · {r.created_at:%d-%m-%Y %H:%M} · {r.reconciliation} · {r.period or '-'}"
              + (f" · by {r.created_by}" if r.created_by else "") for r in runs.itertuples()}
    if not labels:
        return
    run_id = st.selectbox("Select a run", list(labels), format_func=labels.get)
    c1, c2, c3 = st.columns([1, 1, 3], vertical_alignment="center")
    if c1.button("Load run", type="primary", icon=":material/folder_open:"):
        result, decisions, meta = db.load_run(int(run_id))
        set_result(result, int(run_id), meta, decisions, gstr3b=db.load_gstr3b(int(run_id)))
        st.success(f"Run #{run_id} loaded - open the Dashboard.")
    if is_admin():
        confirm = c3.checkbox("I understand deleting is permanent", key="confirm_delete")
        if c2.button("Delete run", disabled=not confirm, icon=":material/delete:"):
            db.delete_run(int(run_id))
            if st.session_state["run_id"] == int(run_id):
                set_result(None, None, {})
            st.rerun()
    else:
        c3.caption("Only an admin can delete runs.")
