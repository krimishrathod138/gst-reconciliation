"""Login screen, first-run admin setup and the sidebar account box.

First-run protection: on your own computer (localhost) the admin account can be
created freely.  When the app is opened through any other address - e.g. deployed
on Streamlit Community Cloud - creating the first admin requires the setup code
from the ``admin_setup_code`` secret (or ``GST_RECON_SETUP_CODE`` environment
variable); without a configured code, setup is refused.  This stops a stranger who
finds the public link first from making themselves administrator.
"""
from __future__ import annotations

import hmac
import os

import streamlit as st

from config import APP_TITLE
from modules import ReconError
from ui.state import get_auth

LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "[::1]"}


def _centered():
    _, mid, _ = st.columns([1, 1.3, 1])
    return mid


def configured_setup_code() -> str:
    """Setup code from the environment or Streamlit secrets (``""`` when none is set)."""
    code = os.environ.get("GST_RECON_SETUP_CODE", "")
    if not code:
        try:
            code = str(st.secrets.get("admin_setup_code", ""))
        except Exception:  # noqa: BLE001 - no secrets.toml present (normal on a local install)
            code = ""
    return code.strip()


def request_host() -> str:
    """Host name the browser used to reach the app (``""`` if unknown, e.g. in tests)."""
    try:
        host = st.context.headers.get("Host", "") or ""
    except Exception:  # noqa: BLE001
        host = ""
    host = host.strip().lower()
    if host.startswith("["):                  # IPv6 literal, e.g. [::1]:8510
        return host.split("]")[0] + "]"
    return host.rsplit(":", 1)[0] if host.count(":") == 1 else host


def setup_requirement(host: str, code: str) -> str:
    """``"open"`` (local, no code), ``"code"`` (code required) or ``"blocked"`` (remote, no code configured)."""
    if code:
        return "code"
    if not host or host in LOCAL_HOSTS:
        return "open"
    return "blocked"


def render_setup() -> None:
    """First start: create the administrator account (there is no default password)."""
    code = configured_setup_code()
    need = setup_requirement(request_host(), code)
    with _centered():
        st.title(f"🧾 {APP_TITLE}")
        st.subheader("Create the administrator account")
        if need == "blocked":
            st.error("For security, the first administrator can only be created on the computer running the app, "
                     "or with a setup code. The owner must add the secret `admin_setup_code` in the hosting "
                     "settings (Streamlit Cloud: App → Settings → Secrets) and reload this page.")
            return
        st.caption("This is the first start. The account you create here can add other users. "
                   "Passwords need 8+ characters with letters and digits.")
        with st.form("setup_form"):
            entered = st.text_input("Setup code", type="password",
                                    help="The admin_setup_code secret set by the app owner.") if need == "code" else ""
            username = st.text_input("Username", placeholder="e.g. admin")
            full_name = st.text_input("Full name")
            pw = st.text_input("Password", type="password")
            pw2 = st.text_input("Confirm password", type="password")
            submitted = st.form_submit_button("Create admin and sign in", type="primary")
        if submitted:
            try:
                if need == "code" and not hmac.compare_digest(entered.strip().encode(), code.encode()):
                    raise ReconError("The setup code is not correct.")
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
