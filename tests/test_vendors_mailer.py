"""Tests for the vendor master and e-mail sending (modules.vendors, modules.mailer, Database vendor store)."""
from __future__ import annotations

import base64
import email
import socketserver
import threading
from urllib.parse import unquote

import pandas as pd
import pytest

from conftest import GSTIN_KA, VALID_GSTIN
from modules import ReconError
from modules.database import Database
from modules.mailer import SmtpSettings, build_message, mailto_link, send_message, split_subject
from modules.vendors import map_vendor_columns, validate_vendors, vendors_from_result


# ----------------------------------------------------------------- vendors
def test_validate_vendors():
    raw = pd.DataFrame({
        "GSTIN": [f" {VALID_GSTIN.lower()} ", "27AAPFU0939F1ZA", GSTIN_KA, VALID_GSTIN, ""],
        "Supplier Name": ["ABC", "Bad", "KA Ltd", "ABC Pvt Ltd", ""],
        "Email ID": ["a@abc.in; accounts@abc.in", "x@y.in", "not-an-email", "a@abc.in", ""],
        "Mobile": ["98200 12345", "", "12", "+91 98200 12345", ""],
    })
    valid, rejected = validate_vendors(map_vendor_columns(raw))
    assert valid["gstin"].tolist() == [VALID_GSTIN]                         # duplicate GSTIN: last row kept
    assert valid.loc[0, "name"] == "ABC Pvt Ltd" and valid.loc[0, "phone"] == "+91 98200 12345"
    reasons = dict(zip(rejected["gstin"], rejected["error"]))
    assert "Checksum" in reasons["27AAPFU0939F1ZA"]
    assert "Invalid e-mail" in reasons[GSTIN_KA] and "Invalid phone" in reasons[GSTIN_KA]
    with pytest.raises(ReconError, match="GSTIN"):
        map_vendor_columns(pd.DataFrame({"Name": ["x"]}))


def test_vendor_store(tmp_path):
    db = Database(f"sqlite:///{(tmp_path / 'v.db').as_posix()}")
    rows = pd.DataFrame([{"gstin": VALID_GSTIN, "name": "ABC", "email": "a@abc.in", "phone": "9820012345"}])
    assert db.upsert_vendors(rows, "asha") == (1, 0)
    assert db.upsert_vendors(rows, "asha") == (0, 0)                        # unchanged
    assert db.upsert_vendors(rows.assign(email="b@abc.in"), "ravi") == (0, 1)
    v = db.get_vendor(VALID_GSTIN.lower())
    assert v["email"] == "b@abc.in" and db.list_vendors().loc[0, "updated_by"] == "ravi"
    assert db.delete_vendors([VALID_GSTIN]) == 1 and db.list_vendors().empty


def test_vendors_from_result(sample_dir):
    from modules.cleaner import clean_dataset
    from modules.loader import load_file, to_canonical
    from modules.matcher import reconcile
    books, _ = clean_dataset(to_canonical(load_file(sample_dir / "purchase_register.xlsx")))
    portal, _ = clean_dataset(to_canonical(load_file(sample_dir / "gstr2b_may2024.json", dataset="portal")), "portal")
    v = vendors_from_result(reconcile(books, portal).recon)
    assert v["gstin"].is_unique and len(v) == 10 and (v["name"] != "").all()   # 2 invalid GSTINs left out


def test_settings_never_store_passwords(tmp_path):
    db = Database(f"sqlite:///{(tmp_path / 's.db').as_posix()}")
    s = SmtpSettings(host="smtp.example.in", port=465, security="SSL", username="u", from_address="gst@co.in")
    db.set_settings(s.to_store(), "admin")
    assert SmtpSettings.from_store(db.get_settings("smtp_")) == s
    with pytest.raises(ReconError, match="never stored"):
        db.set_settings({"smtp_password": "x"})


# ----------------------------------------------------------------- message building
def test_split_subject_and_mailto():
    subject, body = split_subject("Subject: Pending invoices\n\nDear ABC,\nline")
    assert subject == "Pending invoices" and body.startswith("Dear ABC")
    link = mailto_link("a@x.in; b@y.in", "GST & ITC", "Line 1\nLine 2", cc="c@z.in")
    assert link.startswith("mailto:a@x.in,b@y.in?subject=GST%20%26%20ITC")
    assert "Line 1\nLine 2" in unquote(link) and "cc=c%40z.in" in link


def test_build_message_validation():
    s = SmtpSettings(host="h", from_address="gst@co.in", from_name="GST Team")
    with pytest.raises(ReconError, match="recipient"):
        build_message(s, "", "Sub", "Body")
    with pytest.raises(ReconError, match="Invalid e-mail"):
        build_message(s, "bad-address", "Sub", "Body")
    msg = build_message(s, "a@x.in", "Sub", "Body", cc="c@z.in",
                        attachments=[("pending.csv", b"a,b\n1,2\n", "text/csv")])
    assert msg["From"] == "GST Team <gst@co.in>" and msg["Cc"] == "c@z.in"
    assert [p.get_filename() for p in msg.iter_attachments()] == ["pending.csv"]


# ----------------------------------------------------------------- real SMTP conversation
class _SMTPHandler(socketserver.StreamRequestHandler):
    def handle(self):
        send = lambda line: self.wfile.write((line + "\r\n").encode())
        send("220 test ESMTP")
        while True:
            line = self.rfile.readline().decode().rstrip("\r\n")
            cmd = line.upper()
            if not line:
                return
            if cmd.startswith("EHLO"):
                send("250-test")
                send("250 AUTH PLAIN LOGIN")
            elif cmd.startswith("AUTH PLAIN"):
                _, user, pw = base64.b64decode(line.split()[2]).split(b"\0")
                ok = (user, pw) == (b"gst@co.in", b"Right-pass1")
                send("235 ok" if ok else "535 bad credentials")
            elif cmd.startswith("MAIL FROM"):
                send("250 ok")
            elif cmd.startswith("RCPT TO"):
                send("550 no such user" if "refuse" in line else "250 ok")
            elif cmd == "DATA":
                send("354 go")
                data = []
                while (row := self.rfile.readline()) not in (b".\r\n", b""):
                    data.append(row)
                self.server.messages.append(b"".join(data))
                send("250 queued")
            elif cmd == "QUIT":
                send("221 bye")
                return
            else:
                send("250 ok")


@pytest.fixture
def smtp_server():
    server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _SMTPHandler)
    server.messages = []
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server
    server.shutdown()
    server.server_close()


def test_send_through_smtp(smtp_server):
    s = SmtpSettings(host="127.0.0.1", port=smtp_server.server_address[1], security="None",
                     username="gst@co.in", from_address="gst@co.in")
    msg = build_message(s, "abc@supplier.in", "Pending invoices", "Dear ABC,\nPlease upload.",
                        attachments=[("pending.csv", b"inv\nA1\n", "text/csv")])
    msg_id = send_message(s, "Right-pass1", msg)
    assert msg_id.startswith("<") and len(smtp_server.messages) == 1
    received = email.message_from_bytes(smtp_server.messages[0])
    assert received["Subject"] == "Pending invoices" and received["To"] == "abc@supplier.in"
    assert [p.get_filename() for p in received.walk() if p.get_filename()] == ["pending.csv"]


def test_send_errors_are_readable(smtp_server):
    port = smtp_server.server_address[1]
    s = SmtpSettings(host="127.0.0.1", port=port, security="None", username="gst@co.in", from_address="gst@co.in")
    msg = build_message(s, "abc@supplier.in", "S", "B")
    with pytest.raises(ReconError, match="rejected the username"):
        send_message(s, "wrong", msg)
    with pytest.raises(ReconError, match="password missing"):
        send_message(s, "", msg)
    with pytest.raises(ReconError, match="refused the recipient"):
        send_message(s, "Right-pass1", build_message(s, "refuse@supplier.in", "S", "B"))
    with pytest.raises(ReconError, match="Could not send"):
        send_message(SmtpSettings(host="127.0.0.1", port=1, security="None", from_address="gst@co.in"), "", msg)
    with pytest.raises(ReconError, match="not configured"):
        send_message(SmtpSettings(), "", msg)
