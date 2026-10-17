"""
Sending follow-up e-mails through the user's own SMTP server.

Server settings (host, port, security, username, from address) are stored in the
``app_settings`` table by an admin.  The SMTP **password is never stored**: it is
read from the ``GST_RECON_SMTP_PASSWORD`` environment variable or typed in for the
current session.  Every send attempt is recorded in ``email_log`` by the caller.
For users who prefer their own mail program, :func:`mailto_link` builds a link that
opens a pre-filled message, and the UI also offers the text to copy.
"""
from __future__ import annotations

import os
import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formataddr, make_msgid
from urllib.parse import quote

import pandas as pd

from modules import ReconError
from modules.vendors import split_emails, validate_email_list

SETTINGS_PREFIX = "smtp_"
SECURITY = ("STARTTLS", "SSL", "None")
PASSWORD_ENV = "GST_RECON_SMTP_PASSWORD"


@dataclass
class SmtpSettings:
    host: str = ""
    port: int = 587
    security: str = "STARTTLS"
    username: str = ""
    from_address: str = ""
    from_name: str = ""
    cc: str = ""
    timeout: int = 30

    @classmethod
    def from_store(cls, values: dict[str, str]) -> "SmtpSettings":
        v = {k.removeprefix(SETTINGS_PREFIX): val for k, val in values.items() if k.startswith(SETTINGS_PREFIX)}
        try:
            port = int(v.get("port") or 587)
        except ValueError:
            port = 587
        return cls(host=v.get("host", ""), port=port, security=v.get("security", "STARTTLS") or "STARTTLS",
                   username=v.get("username", ""), from_address=v.get("from_address", ""),
                   from_name=v.get("from_name", ""), cc=v.get("cc", ""))

    def to_store(self) -> dict[str, str]:
        return {f"{SETTINGS_PREFIX}{k}": str(getattr(self, k))
                for k in ("host", "port", "security", "username", "from_address", "from_name", "cc")}

    @property
    def configured(self) -> bool:
        return bool(self.host and self.from_address)

    def validate(self) -> None:
        if not self.host:
            raise ReconError("SMTP server is not configured (ask an admin: Settings -> E-mail server).")
        if self.security not in SECURITY:
            raise ReconError(f"Security must be one of {', '.join(SECURITY)}.")
        if not 1 <= int(self.port) <= 65535:
            raise ReconError("SMTP port must be between 1 and 65535.")
        err = validate_email_list(self.from_address)
        if err or not self.from_address:
            raise ReconError(f"From address is not valid. {err}")


def env_password() -> str:
    return os.environ.get(PASSWORD_ENV, "")


def split_subject(text: str) -> tuple[str, str]:
    """Split a drafted e-mail (first line ``Subject: ...``) into subject and body."""
    lines = (text or "").splitlines()
    if lines and lines[0].lower().startswith("subject:"):
        return lines[0].split(":", 1)[1].strip(), "\n".join(lines[1:]).lstrip("\n")
    return "", text or ""


def build_message(settings: SmtpSettings, to: str, subject: str, body: str, cc: str = "",
                  attachments: list[tuple[str, bytes, str]] | None = None) -> EmailMessage:
    """Compose the message (validates recipient addresses)."""
    recipients = split_emails(to)
    if not recipients:
        raise ReconError("Enter at least one recipient e-mail address.")
    for label, value in (("To", to), ("Cc", cc)):
        err = validate_email_list(value)
        if err:
            raise ReconError(f"{label}: {err}")
    if not subject.strip():
        raise ReconError("The subject is empty.")
    msg = EmailMessage()
    msg["From"] = formataddr((settings.from_name, settings.from_address)) if settings.from_name else settings.from_address
    msg["To"] = ", ".join(recipients)
    if split_emails(cc):
        msg["Cc"] = ", ".join(split_emails(cc))
    msg["Subject"] = subject.strip()
    msg["Message-ID"] = make_msgid(domain=settings.from_address.split("@")[-1])
    msg.set_content(body)
    for filename, data, mime in attachments or []:
        maintype, _, subtype = mime.partition("/")
        msg.add_attachment(data, maintype=maintype, subtype=subtype or "octet-stream", filename=filename)
    return msg


def send_message(settings: SmtpSettings, password: str, msg: EmailMessage) -> str:
    """Send via SMTP; returns the Message-ID.  Raises :class:`ReconError` with a readable reason."""
    settings.validate()
    try:
        if settings.security == "SSL":
            server: smtplib.SMTP = smtplib.SMTP_SSL(settings.host, int(settings.port), timeout=settings.timeout,
                                                    context=ssl.create_default_context())
        else:
            server = smtplib.SMTP(settings.host, int(settings.port), timeout=settings.timeout)
        with server:
            server.ehlo()
            if settings.security == "STARTTLS":
                server.starttls(context=ssl.create_default_context())
                server.ehlo()
            if settings.username:
                if not password:
                    raise ReconError(f"SMTP password missing - enter it for this session or set {PASSWORD_ENV}.")
                server.login(settings.username, password)
            server.send_message(msg)
    except ReconError:
        raise
    except smtplib.SMTPAuthenticationError as exc:
        raise ReconError("The mail server rejected the username / password.") from exc
    except smtplib.SMTPRecipientsRefused as exc:
        raise ReconError(f"The mail server refused the recipient(s): {', '.join(exc.recipients)}") from exc
    except (smtplib.SMTPException, OSError) as exc:
        raise ReconError(f"Could not send e-mail via {settings.host}:{settings.port} - {exc}") from exc
    return str(msg["Message-ID"])


def mailto_link(to: str, subject: str, body: str, cc: str = "") -> str:
    """``mailto:`` URL that opens a pre-filled message in the user's mail program."""
    params = [f"subject={quote(subject)}", f"body={quote(body)}"]
    if cc:
        params.append(f"cc={quote(','.join(split_emails(cc)))}")
    return f"mailto:{quote(','.join(split_emails(to)), safe='@,')}?" + "&".join(params)


def pending_csv(rows: pd.DataFrame) -> bytes:
    """CSV attachment listing a supplier's pending documents."""
    return rows.to_csv(index=False).encode("utf-8-sig")
