"""Page: supplier-wise summary and follow-up e-mails (send, copy or open in the mail program)."""
from __future__ import annotations

import pandas as pd
import streamlit as st

from config import PARTY_LABELS, STATUS_MISMATCH
from modules import ReconError
from modules.formatting import format_inr
from modules.mailer import SmtpSettings, build_message, env_password, mailto_link, pending_csv, send_message, \
    split_subject
from modules.matcher import supplier_summary
from modules.reports import display_frame, side_columns, supplier_columns, supplier_email
from ui.state import csv_download, filter_bar, get_db, require_result, short_name, show_table, username, with_recon


def smtp_ready() -> tuple[SmtpSettings, str]:
    """Stored SMTP settings and the password for this session (env var or typed in)."""
    return SmtpSettings.from_store(get_db().get_settings("smtp_")), env_password() or st.session_state["smtp_password"]


def pending_rows(res, gstin: str) -> pd.DataFrame:
    rows = res.recon[(res.recon["gstin"] == gstin) &
                     res.recon["status"].isin([res.missing_portal_status, STATUS_MISMATCH])]
    return display_frame(rows, side_columns(res, "books"))


def send_one(res, settings: SmtpSettings, password: str, gstin: str, to: str, cc: str, subject: str, body: str,
             attach: bool) -> str:
    """Send and log one e-mail; returns the Message-ID (raises ReconError on failure, after logging it)."""
    db = get_db()
    attachments = [(f"pending_{gstin}.csv", pending_csv(pending_rows(res, gstin)), "text/csv")] if attach else []
    try:
        msg = build_message(settings, to, subject, body, cc, attachments)
        msg_id = send_message(settings, password, msg)
    except ReconError as exc:
        db.log_email(st.session_state["run_id"], gstin, to, subject, "failed", str(exc), username())
        raise
    db.log_email(st.session_state["run_id"], gstin, to, subject, "sent", "", username())
    return msg_id


def render() -> None:
    res = require_result()
    if res is None:
        return
    short = short_name(res)
    party = PARTY_LABELS[res.mode][1]
    st.header(f"{party.split()[0]}-wise summary & follow-up")
    recon = filter_bar(res.recon, "sup", res)
    view = with_recon(res, recon)
    summary = supplier_summary(recon, res.portal_name)
    only_pending = st.toggle("Show only suppliers with pending items", value=True)
    shown = summary[(summary["pending_itc"] > 0) | (summary["missing_portal_count"] > 0) |
                    (summary["mismatch_count"] > 0) | (summary["missing_books_count"] > 0)] if only_pending else summary
    table = display_frame(shown, supplier_columns(res))
    show_table(table, height=340, key="sup_table")
    csv_download(table, "Download supplier summary (CSV)", "supplier_summary.csv", "dl_sup")

    st.subheader("Follow-up e-mail")
    pending = summary[(summary["missing_portal_count"] > 0) | (summary["mismatch_count"] > 0)]
    if pending.empty:
        st.success(f"No supplier has documents missing in {short} or mismatched (for the selected filters).")
        return
    labels = {g: f"{n or '(no name)'} — {g} · {format_inr(p)} pending"
              for g, n, p in zip(pending["gstin"], pending["party_name"], pending["pending_itc"])}
    gstin = st.selectbox(party, list(labels), format_func=labels.get)
    meta = st.session_state["meta"]
    args = (st.session_state["company_name"], st.session_state["company_gstin"], meta.get("period_label", ""))
    draft_subject, draft_body = split_subject(supplier_email(view, gstin, *args))
    vendor = get_db().get_vendor(gstin) or {}
    settings, password = smtp_ready()

    c1, c2 = st.columns([2, 1])
    with c2:
        st.markdown("**Contact (vendor master)**")
        if vendor:
            st.markdown(f"{vendor.get('contact_person') or ''}  \n📧 {vendor.get('email') or '—'}  \n"
                        f"📞 {vendor.get('phone') or '—'}")
        else:
            st.caption("Not in the vendor master yet - add the e-mail on the **Vendor Master** page.")
    with c1:
        to = st.text_input("To", value=vendor.get("email", ""), key=f"mail_to_{gstin}")
        cc = st.text_input("Cc", value=settings.cc, key=f"mail_cc_{gstin}")
        subject = st.text_input("Subject", value=draft_subject, key=f"mail_subj_{gstin}")
        body = st.text_area("Message", draft_body, height=320, key=f"mail_body_{gstin}")
        attach = st.checkbox("Attach the list of pending documents (CSV)", value=True, key=f"mail_att_{gstin}")

    b1, b2, b3 = st.columns(3)
    with b1:
        if not settings.configured:
            st.button("Send e-mail", disabled=True, help="An admin must set up the e-mail server first (Admin page).")
        else:
            if settings.username and not password:
                st.session_state["smtp_password"] = st.text_input(
                    f"SMTP password for {settings.username} (kept for this session only)", type="password")
                password = st.session_state["smtp_password"]
            if st.button("Send e-mail", type="primary", icon=":material/send:", disabled=not to):
                try:
                    send_one(res, settings, password, gstin, to, cc, subject, body, attach)
                    st.success(f"E-mail sent to {to}.")
                except ReconError as exc:
                    st.error(f"Not sent: {exc}")
    with b2:
        st.link_button("Open in my mail app", mailto_link(to, subject, body, cc), icon=":material/mail:",
                       disabled=not to, help="Opens a pre-filled message in Outlook / your default mail program.")
    with b3:
        st.download_button("Download text (.txt)", f"Subject: {subject}\n\n{body}".encode("utf-8"),
                           file_name=f"followup_{gstin}.txt", mime="text/plain", icon=":material/download:")
    with st.expander("Copy the text", icon=":material/content_copy:"):
        st.caption("Use the copy icon at the top-right of the box.")
        st.code(f"Subject: {subject}\n\n{body}", language=None)

    with st.expander(f"Send to all {len(pending)} suppliers with pending items"):
        vendors = get_db().list_vendors().set_index("gstin")
        plan = pd.DataFrame({"GSTIN": pending["gstin"], "Name": pending["party_name"],
                             "E-mail": pending["gstin"].map(vendors["email"]) if len(vendors) else "",
                             "Pending ITC": pending["pending_itc"]}).fillna({"E-mail": ""})
        show_table(plan, key="bulk_plan")
        ready = plan[plan["E-mail"] != ""]
        st.caption(f"{len(ready)} have an e-mail address in the vendor master; the others are skipped.")
        confirm = st.checkbox(f"Yes, send {len(ready)} e-mails now", key="bulk_confirm")
        if st.button("Send all", disabled=not (confirm and settings.configured and len(ready))):
            results = []
            for g, address in zip(ready["GSTIN"], ready["E-mail"]):
                subj, text = split_subject(supplier_email(view, g, *args))
                try:
                    send_one(res, settings, password, g, address, settings.cc, subj, text, True)
                    results.append((g, address, "sent", ""))
                except ReconError as exc:
                    results.append((g, address, "failed", str(exc)))
            show_table(pd.DataFrame(results, columns=["GSTIN", "E-mail", "Status", "Error"]), key="bulk_result")

    log = get_db().list_emails(st.session_state["run_id"]) if st.session_state["run_id"] else pd.DataFrame()
    if len(log):
        st.subheader("E-mails sent for this run")
        show_table(log.drop(columns="run_id"), height=220, key="mail_log")
