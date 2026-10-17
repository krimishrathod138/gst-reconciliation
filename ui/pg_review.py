"""Page: accept / reject probable matches, remarks, and the review audit trail."""
from __future__ import annotations

import pandas as pd
import streamlit as st

from config import DECISION_ACCEPT, DECISION_PENDING, DECISION_REJECT, DECISIONS, STATUS_MISMATCH, \
    STATUS_MISSING_BOOKS, STATUS_PROBABLE
from ui.state import base_result, filter_bar, get_db, merge_decisions, money_config, require_result, short_name, \
    show_table


def render() -> None:
    res = require_result()
    if res is None:
        return
    base = base_result()
    short = short_name(res)
    st.header("Review probable matches")
    st.caption("Invoice numbers are similar and amounts agree. Accept to treat as matched, reject to split them "
               f"back into 'Missing in {short}' and 'Missing in Books'. Every change is recorded with your user "
               "name and the time.")
    decisions = st.session_state["decisions"].drop_duplicates("match_id", keep="last").set_index("match_id")
    prob_all = base.recon[base.recon["status"] == STATUS_PROBABLE]
    if prob_all.empty:
        st.success("There are no probable matches to review.")
    else:
        prob = filter_bar(prob_all, "rev", res, statuses=False)
        edit = pd.DataFrame({
            "Match ID": prob["match_id"], "GSTIN": prob["gstin"], "Name": prob["party_name"],
            "Invoice (Books)": prob["invoice_no_books"], f"Invoice ({short})": prob["invoice_no_portal"],
            "Date (Books)": prob["invoice_date_books"], f"Date ({short})": prob["invoice_date_portal"],
            "Taxable (Books)": prob["taxable_value_books"], f"Taxable ({short})": prob["taxable_value_portal"],
            "Tax (Books)": prob["total_tax_books"], f"Tax ({short})": prob["total_tax_portal"],
            "Score": prob["fuzzy_score"],
            "Decision": prob["match_id"].map(decisions["decision"]).replace("", DECISION_PENDING).fillna(DECISION_PENDING),
            "Remarks": prob["match_id"].map(decisions["remarks"]).fillna(""),
            "Reviewed By": prob["match_id"].map(decisions["updated_by"]).fillna(""),
            "Reviewed At": pd.to_datetime(prob["match_id"].map(decisions["updated_at"]), errors="coerce"),
        })
        c1, c2, c3 = st.columns([2, 1, 3], vertical_alignment="bottom")
        min_score = c1.slider("Bulk-accept pending matches with score ≥", 85, 100, 95)
        if c2.button("Bulk accept"):
            sel = edit[(edit["Score"] >= min_score) & (edit["Decision"] == DECISION_PENDING)]
            n = merge_decisions(pd.DataFrame({"match_id": sel["Match ID"], "decision": DECISION_ACCEPT,
                                              "remarks": sel["Remarks"].replace("", f"Bulk accepted (score ≥ {min_score})")}))
            st.toast(f"{n} match(es) accepted")
            st.rerun()
        c3.caption(f"{(edit['Decision'] == DECISION_PENDING).sum()} pending · "
                   f"{(edit['Decision'] == DECISION_ACCEPT).sum()} accepted · "
                   f"{(edit['Decision'] == DECISION_REJECT).sum()} rejected"
                   + (f" (showing {len(edit)} of {len(prob_all)})" if len(edit) < len(prob_all) else ""))
        edited = st.data_editor(
            edit, hide_index=True, key="prob_editor",
            disabled=[c for c in edit.columns if c not in ("Decision", "Remarks")],
            column_config=money_config(edit, {
                "Decision": st.column_config.SelectboxColumn("Decision", options=list(DECISIONS), required=True),
                "Remarks": st.column_config.TextColumn("Remarks", max_chars=500),
                "Score": st.column_config.ProgressColumn("Score", min_value=0, max_value=100, format="%.1f"),
            }))
        if st.button("Save decisions", type="primary", icon=":material/save:"):
            n = merge_decisions(pd.DataFrame({"match_id": edited["Match ID"], "decision": edited["Decision"],
                                              "remarks": edited["Remarks"]}))
            st.toast(f"{n} change(s) saved")
            st.rerun()

    st.subheader("Remarks on other documents")
    st.caption("Add follow-up notes to mismatches and missing documents (they appear in the Excel report).")
    others = base.recon[base.recon["status"].isin([STATUS_MISMATCH, STATUS_MISSING_BOOKS, res.missing_portal_status])]
    if others.empty:
        st.caption("Nothing to annotate.")
    else:
        others = filter_bar(others, "rev_other", res)
        notes = pd.DataFrame({
            "Match ID": others["match_id"], "Status": others["status"], "GSTIN": others["gstin"],
            "Name": others["party_name"],
            "Invoice": others["invoice_no_books"].where(others["invoice_no_books"] != "", others["invoice_no_portal"]),
            "ITC (Books)": others["itc_books"], f"ITC ({short})": others["itc_portal"], "Reason": others["reason"],
            "Remarks": others["match_id"].map(decisions["remarks"]).fillna(""),
        })
        edited_notes = st.data_editor(notes, hide_index=True, key="notes_editor", height=380,
                                      disabled=[c for c in notes.columns if c != "Remarks"],
                                      column_config=money_config(notes))
        if st.button("Save remarks", icon=":material/save:"):
            changed = edited_notes[edited_notes["Remarks"] != notes["Remarks"]]
            n = merge_decisions(pd.DataFrame({"match_id": changed["Match ID"], "decision": "",
                                              "remarks": changed["Remarks"]})) if len(changed) else 0
            st.toast(f"{n} remark(s) saved")
            st.rerun()

    st.subheader("Audit trail")
    run_id = st.session_state["run_id"]
    audit = get_db().load_audit(run_id) if run_id else pd.DataFrame()
    if audit.empty:
        st.caption("No review actions recorded for this run yet.")
    else:
        show_table(audit.rename(columns={"at": "When", "username": "User", "match_id": "Match ID", "action": "Action",
                                         "old_decision": "Old Decision", "new_decision": "New Decision",
                                         "old_remarks": "Old Remarks", "new_remarks": "New Remarks"}),
                   height=300, key="audit_table")
