"""End-to-end tests of the Streamlit app (headless, via streamlit.testing.AppTest).

Every test gets its own temporary database through ``GST_RECON_DB_URL``.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from modules.auth import AuthService
from modules.database import Database

ROOT = Path(__file__).resolve().parents[1]
ADMIN = ("admin", "Admin1234")
PAGES = ["Dashboard", "Review Probable Matches", "Supplier Follow-up", "Month-wise & Carry Forward",
         "GSTR-3B Comparison", "Vendor Master", "Data Quality", "Reports", "History"]


@pytest.fixture
def db_url(tmp_path, monkeypatch):
    url = f"sqlite:///{(tmp_path / 'app.db').as_posix()}"
    monkeypatch.setenv("GST_RECON_DB_URL", url)
    monkeypatch.chdir(ROOT)
    return url


def new_app() -> AppTest:
    at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=600)
    at.run()
    return at


def btn(at: AppTest, label: str):
    return next(b for b in at.button if b.label == label)


def ok(at: AppTest, where: str = "") -> None:
    problems = [str(e.value)[:300] for e in at.exception] + [e.value for e in at.error]
    assert not problems, f"{where}: {problems}"


def login(at: AppTest, user: str, pw: str) -> AppTest:
    at.text_input[0].set_value(user)
    at.text_input[1].set_value(pw)
    btn(at, "Sign in").click().run()
    return at


def go(at: AppTest, page: str) -> AppTest:
    at.sidebar.radio(key="nav").set_value(page).run()
    ok(at, page)
    return at


def admin_app(db_url: str) -> AppTest:
    AuthService(Database(db_url), iterations=1_000).create_user(*ADMIN, full_name="Admin")
    at = login(new_app(), *ADMIN)
    ok(at, "login")
    return at


def run_sample(at: AppTest, mode: str = "Purchase Register vs GSTR-2B", kind: str = "Excel register",
               fy: str | None = None, months: tuple[str, str] | None = None, carry: bool = True) -> AppTest:
    go(at, "Reconcile")
    at.selectbox(key="mode_label").set_value(mode).run()
    at.selectbox(key="sample_kind").set_value(kind).run()
    btn(at, "Load sample data").click().run()
    at.sidebar.text_input(key="company_gstin").set_value("27AABCD1234E1Z8").run()
    if fy:
        at.selectbox(key="rec_fy").set_value(fy).run()
        if months:
            at.select_slider(key=f"rec_months_{fy}").set_value(months).run()
    at.checkbox(key="rec_carry").set_value(carry).run()
    btn(at, "Run reconciliation").click().run()
    ok(at, "run")
    return at


def successes(at: AppTest) -> str:
    return " | ".join(s.value for s in at.success)


# ----------------------------------------------------------------- login
def test_first_run_creates_admin_then_login_required(db_url):
    at = new_app()
    assert any("Create the administrator account" in s.value for s in at.subheader)
    for i, value in enumerate(["asha", "Asha Rao", "Secret123", "Secret123"]):
        at.text_input[i].set_value(value)
    btn(at, "Create admin and sign in").click().run()
    ok(at, "setup")
    assert "Admin" in at.sidebar.radio(key="nav").options                    # first account is an admin
    fresh = new_app()                                                         # a new browser session
    assert any("Sign in" in s.value for s in fresh.subheader)
    login(fresh, "asha", "wrong-pass1")
    assert any("Invalid username or password" in e.value for e in fresh.error)
    login(fresh, "asha", "Secret123")
    ok(fresh, "login")
    assert fresh.sidebar.radio(key="nav").value == "Reconcile"


def test_non_admin_has_no_admin_page(db_url):
    auth = AuthService(Database(db_url), iterations=1_000)
    auth.create_user(*ADMIN)
    auth.create_user("ravi", "Ravi12345", role="user")
    at = login(new_app(), "ravi", "Ravi12345")
    ok(at, "login")
    assert "Admin" not in at.sidebar.radio(key="nav").options


# ----------------------------------------------------------------- full flow
def test_purchase_flow_every_page(db_url):
    at = run_sample(admin_app(db_url))
    text = successes(at)
    assert "Exact Match: 29" in text and "Mismatch: 7" in text and "Probable Match: 4" in text

    go(at, "Dashboard")
    metrics = {m.label: m.value for m in at.metric}
    assert metrics["⚠️ ITC at risk"] == "₹39,360" and metrics["Documents in books"] == "53"
    assert any("₹ only" in i.value for i in at.info)                          # rule shown on the dashboard

    go(at, "Review Probable Matches")
    at.slider[0].set_value(85).run()
    btn(at, "Bulk accept").click().run()
    ok(at, "bulk accept")
    audit = Database(db_url).load_audit(1)
    assert len(audit) == 4 and set(audit["username"]) == {"admin"} and set(audit["action"]) == {"Accepted"}
    go(at, "Dashboard")
    assert {m.label: m.value for m in at.metric}["Matched"] == "38"          # 34 + 4 manually matched

    go(at, "Vendor Master")
    btn(at, "Add suppliers from current reconciliation").click().run()
    ok(at, "vendors")
    assert len(Database(db_url).list_vendors()) == 10

    go(at, "Supplier Follow-up")
    assert btn(at, "Send e-mail").disabled                                    # no SMTP server configured yet
    assert any("Subject:" in c.value for c in at.code)                        # copy-text box

    go(at, "GSTR-3B Comparison")
    btn(at, "Use and save these figures").click().run()                       # zeros for Apr and May
    ok(at, "3B save")
    assert Database(db_url).load_gstr3b(1) is not None
    assert any("Short claimed" in m.delta for m in at.metric if m.delta)

    go(at, "Month-wise & Carry Forward")
    assert any(m.label == "Still pending at end (docs)" for m in at.metric)

    go(at, "Reports")
    btn(at, "Generate Excel report").click().run()
    btn(at, "Generate PDFs").click().run()
    ok(at, "reports")
    labels = [d.label for d in at.get("download_button")]
    assert "Download Excel report" in labels and "Download Summary (PDF)" in labels
    assert "Download Supplier-wise (PDF)" in labels

    go(at, "Data Quality")
    go(at, "Admin")
    go(at, "History")
    btn(at, "Load run").click().run()
    ok(at, "load run")
    assert any("Run #1 loaded" in s.value for s in at.success)
    go(at, "Dashboard")                                                       # reloaded run keeps the decisions
    assert {m.label: m.value for m in at.metric}["Matched"] == "38"


def test_send_followup_email_from_app(db_url):
    import email
    import socketserver
    import threading

    import pandas as pd
    from test_vendors_mailer import _SMTPHandler
    from modules.mailer import SmtpSettings

    server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _SMTPHandler)
    server.messages = []
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        db = Database(db_url)
        db.set_settings(SmtpSettings(host="127.0.0.1", port=server.server_address[1], security="None",
                                     from_address="gst@demotraders.in").to_store(), "admin")
        at = run_sample(admin_app(db_url))
        rec = db.load_run(1)[0].recon
        gstin = rec.loc[rec["status"] == "Missing in GSTR-2B", "gstin"].iloc[0]
        db.upsert_vendors(pd.DataFrame([{"gstin": gstin, "name": "X", "email": "accounts@supplier.in"}]), "admin")
        go(at, "Supplier Follow-up")
        at.selectbox[0].set_value(gstin).run()
        assert at.text_input(key=f"mail_to_{gstin}").value == "accounts@supplier.in"   # from the vendor master
        btn(at, "Send e-mail").click().run()
        ok(at, "send")
        assert any("E-mail sent to accounts@supplier.in" in s.value for s in at.success)
        received = email.message_from_bytes(server.messages[0])
        assert received["To"] == "accounts@supplier.in" and "GST reconciliation" in received["Subject"]
        assert any(p.get_filename() == f"pending_{gstin}.csv" for p in received.walk())
        log = db.list_emails(1)
        assert log.loc[0, "status"] == "sent" and log.loc[0, "sent_by"] == "admin"
    finally:
        server.shutdown()
        server.server_close()


def test_admin_adds_user(db_url):
    at = go(admin_app(db_url), "Admin")
    form_inputs = [t for t in at.text_input if t.label in ("Username", "Full name", "Initial password")]
    for widget, value in zip(form_inputs, ["meena", "Meena S", "Meena1234"]):
        widget.set_value(value)
    btn(at, "Add user").click().run()
    ok(at, "add user")
    assert "meena" in set(AuthService(Database(db_url)).list_users()["username"])


def test_tally_sample(db_url):
    at = run_sample(admin_app(db_url), kind="Tally export")
    assert any("Tally register export detected" in i.value for i in at.info)
    assert "Exact Match: 29" in successes(at)


def test_carry_forward_between_runs(db_url):
    at = run_sample(admin_app(db_url), fy="2024-25", months=("2024-04", "2024-04"))
    april = Database(db_url).load_run(1)[0]
    missing_april = int((april.recon["status"] == "Missing in GSTR-2B").sum())
    assert missing_april > 0
    at = run_sample(at, fy="2024-25", months=("2024-05", "2024-05"))
    carry = next(i.value for i in at.info if "Carry forward" in i.value)
    assert f"{missing_april} document(s) carried forward from run #1" in carry
    may = Database(db_url).load_run(2)[0]
    carried = may.recon[may.recon["carried_from_books"] != ""]
    assert len(carried) == missing_april and set(carried["carried_from_books"]) == {"Apr 2024"}
    assert carried["has_portal"].sum() == 3        # the 3 April invoices the supplier uploaded in May's 2B
    runs = Database(db_url).list_runs().set_index("run_id")
    assert (runs.loc[2, "fy"], runs.loc[2, "from_month"], runs.loc[2, "to_month"]) == ("2024-25", "2024-05", "2024-05")
