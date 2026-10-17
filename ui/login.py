"""Login screen, first-run admin setup and the sidebar account box."""
from __future__ import annotations

import streamlit as st

from config import APP_TITLE
from modules import ReconError
from ui.state import get_auth


def _centered():
    _, mid, _ = st.columns([1, 1.3, 1])
    return mid


def render_setup() -> None:
    """First start: create the administrator account (there is no default password)."""
    with _centered():
        st.title(f"🧾 {APP_TITLE}")
        st.subheader("Create the administrator account")
        st.caption("This is the first start. The account you create here can add other users. "
                   "Passwords need 8+ characters with letters and digits.")
        with st.form("setup_form"):
            username = st.text_input("Username", placeholder="e.g. admin")
            full_name = st.text_input("Full name")
            pw = st.text_input("Password", type="password")
            pw2 = st.text_input("Confirm password", type="password")
            submitted = st.form_submit_button("Create admin and sign in", type="primary")
        if submitted:
            try:
                if pw != pw2:
                    raise ReconError("The two passwords do not match.")
                get_auth().create_user(username, pw, role="admin", full_name=full_name, created_by="setup")
                st.session_state["user"] = get_auth().authenticate(username, pw)
                st.rerun()
            except ReconError as exc:
                st.error(str(exc))


def render_login() -> None:
    with _centered():
        st.title(f"🧾 {APP_TITLE}")
        st.subheader("Sign in")
        with st.form("login_form"):
            username = st.text_input("Username")
            pw = st.text_input("Password", type="password")
            submitted = st.form_submit_button("Sign in", type="primary")
        if submitted:
            try:
                st.session_state["user"] = get_auth().authenticate(username, pw)
                st.rerun()
            except ReconError as exc:
                st.error(str(exc))
        st.caption("Forgot your password? Ask an administrator to reset it on the Admin page.")


def account_box() -> None:
    """Signed-in user, change password and sign-out (sidebar)."""
    user = st.session_state["user"]
    st.caption(f"Signed in as **{user.display}** ({user.role})")
    with st.expander("Change my password"):
        with st.form("change_pw", clear_on_submit=True):
            old = st.text_input("Current password", type="password")
            new = st.text_input("New password", type="password")
            new2 = st.text_input("Confirm new password", type="password")
            if st.form_submit_button("Change password"):
                try:
                    if new != new2:
                        raise ReconError("The new passwords do not match.")
                    get_auth().change_password(user.username, old, new)
                    st.success("Password changed.")
                except ReconError as exc:
                    st.error(str(exc))
    if st.button("Sign out", icon=":material/logout:", key="logout"):
        for key in list(st.session_state.keys()):
            del st.session_state[key]
        st.rerun()
