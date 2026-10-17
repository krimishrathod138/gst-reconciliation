"""Page (admins only): users, e-mail server settings and the e-mail log."""
from __future__ import annotations

import streamlit as st

from modules import ReconError
from modules.auth import ROLES
from modules.mailer import PASSWORD_ENV, SECURITY, SmtpSettings, build_message, env_password, send_message
from ui.state import get_auth, get_db, is_admin, show_table, username


def _users() -> None:
    auth = get_auth()
    st.subheader("Users")
    show_table(auth.list_users(), key="users_table")
    c1, c2 = st.columns(2)
    with c1, st.form("add_user", clear_on_submit=True):
        st.markdown("**Add a user**")
        name = st.text_input("Username")
        full = st.text_input("Full name")
        role = st.selectbox("Role", ROLES, index=1)
        pw = st.text_input("Initial password", type="password", help="8+ characters with letters and digits.")
        if st.form_submit_button("Add user", type="primary"):
            try:
                auth.create_user(name, pw, role=role, full_name=full, created_by=username())
                st.success(f"User '{name.strip().lower()}' added.")
                st.rerun()
            except ReconError as exc:
                st.error(str(exc))
    with c2:
        st.markdown("**Change a user**")
        users = auth.list_users()["username"].tolist()
        who = st.selectbox("User", users, key="adm_user")
        with st.form("edit_user", clear_on_submit=True):
            new_pw = st.text_input("New password (leave blank to keep)", type="password")
            new_role = st.selectbox("Role", ["(no change)", *ROLES])
            active = st.selectbox("Status", ["(no change)", "Active", "Deactivated"])
            if st.form_submit_button("Apply"):
                try:
                    if new_pw:
                        auth.reset_password(who, new_pw)
                    if new_role != "(no change)":
                        auth.set_role(who, new_role)
                    if active != "(no change)":
                        auth.set_active(who, active == "Active")
                    st.success(f"'{who}' updated.")
                    st.rerun()
                except ReconError as exc:
                    st.error(str(exc))


def _smtp() -> None:
    db = get_db()
    st.subheader("E-mail server (SMTP)")
    st.caption(f"Used for supplier follow-up e-mails. The password is **never stored**: set the environment variable "
               f"`{PASSWORD_ENV}` before starting the app, or type it when sending (kept for the session only).")
    current = SmtpSettings.from_store(db.get_settings("smtp_"))
    with st.form("smtp_form"):
        c1, c2, c3 = st.columns([3, 1, 1])
        host = c1.text_input("SMTP server", current.host, placeholder="e.g. smtp.office365.com / smtp.gmail.com")
        port = c2.number_input("Port", 1, 65535, int(current.port))
        security = c3.selectbox("Security", SECURITY, index=SECURITY.index(current.security))
        c4, c5, c6 = st.columns(3)
        user = c4.text_input("Username (blank = no login)", current.username)
        from_addr = c5.text_input("From address", current.from_address)
        from_name = c6.text_input("From name", current.from_name, placeholder="e.g. Accounts - Demo Traders")
        cc = st.text_input("Always Cc (optional)", current.cc)
        if st.form_submit_button("Save settings", type="primary"):
            try:
                s = SmtpSettings(host.strip(), int(port), security, user.strip(), from_addr.strip(), from_name.strip(),
                                 cc.strip())
                s.validate()
                db.set_settings(s.to_store(), username())
                st.success("E-mail settings saved.")
            except ReconError as exc:
                st.error(str(exc))
    with st.expander("Send a test e-mail"):
        to = st.text_input("Send test to", key="smtp_test_to")
        pw = env_password() or st.text_input("SMTP password (not stored)", type="password", key="smtp_test_pw")
        if st.button("Send test", disabled=not to):
            try:
                s = SmtpSettings.from_store(db.get_settings("smtp_"))
                send_message(s, pw, build_message(s, to, "GST Reconciliation - test e-mail",
                                                  "The e-mail settings work."))
                st.session_state["smtp_password"] = pw
                st.success(f"Test e-mail sent to {to}.")
            except ReconError as exc:
                st.error(str(exc))


def render() -> None:
    if not is_admin():
        st.error("Only administrators can open this page.")
        return
    st.header("Admin")
    tabs = st.tabs(["Users", "E-mail server", "E-mail log"])
    with tabs[0]:
        _users()
    with tabs[1]:
        _smtp()
    with tabs[2]:
        log = get_db().list_emails()
        if log.empty:
            st.caption("No e-mails sent yet.")
        else:
            show_table(log, key="email_log_all")
