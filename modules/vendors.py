"""
Vendor (supplier / customer) master: validation and bulk import.

Each vendor is keyed by GSTIN and holds name, e-mail(s), phone, contact person and
notes.  Several e-mail addresses may be given separated by commas or semicolons.
Records are stored through :class:`modules.database.Database` (``vendors`` table).
"""
from __future__ import annotations

import re

import pandas as pd

from config import normalise_header
from modules import ReconError
from modules.cleaner import clean_gstin, clean_text, validate_gstin

VENDOR_FIELDS = ("gstin", "name", "email", "phone", "contact_person", "notes")
_ALIASES = {
    "gstin": ("gstin", "gstinuin", "suppliergstin", "vendorgstin", "partygstin", "customergstin", "gstno"),
    "name": ("name", "suppliername", "vendorname", "partyname", "tradename", "legalname", "customername"),
    "email": ("email", "emailid", "emailaddress", "mail", "emails"),
    "phone": ("phone", "mobile", "phoneno", "mobileno", "contactno", "telephone", "phonenumber"),
    "contact_person": ("contactperson", "contact", "contactname", "attention"),
    "notes": ("notes", "remarks", "comments"),
}
_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+'-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")


def split_emails(text: str) -> list[str]:
    """``"a@x.com; b@y.in"`` -> ``["a@x.com", "b@y.in"]``."""
    return [e.strip() for e in re.split(r"[,;\s]+", text or "") if e.strip()]


def validate_email_list(text: str) -> str:
    """``""`` if every address is valid (or the field is blank), else a reason."""
    bad = [e for e in split_emails(text) if not _EMAIL_RE.match(e)]
    return f"Invalid e-mail: {', '.join(bad)}" if bad else ""


def validate_phone(text: str) -> str:
    digits = re.sub(r"[\s()+-]", "", text or "")
    if not digits:
        return ""
    return "" if digits.isdigit() and 10 <= len(digits) <= 15 else f"Invalid phone: {text}"


def map_vendor_columns(frame: pd.DataFrame) -> pd.DataFrame:
    """Rename vendor-list columns (Excel / CSV) to :data:`VENDOR_FIELDS`."""
    norms = {c: normalise_header(c) for c in frame.columns if not str(c).startswith("__")}
    rename: dict[str, str] = {}
    for field, aliases in _ALIASES.items():
        col = next((c for a in aliases for c, n in norms.items() if n == a and c not in rename), None)
        if col is not None:
            rename[col] = field
    if "gstin" not in rename.values():
        raise ReconError("The vendor list needs a GSTIN column.")
    out = frame.rename(columns=rename)
    for field in VENDOR_FIELDS:
        if field not in out:
            out[field] = ""
    return out[list(VENDOR_FIELDS)]


def validate_vendors(frame: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Clean and validate vendor rows.

    Returns:
        ``(valid, rejected)`` - *rejected* has an ``error`` column explaining why.
        Duplicate GSTINs keep the last row.
    """
    df = frame.copy()
    for field in VENDOR_FIELDS:
        if field not in df:
            df[field] = ""
        df[field] = clean_text(df[field])
    df["gstin"] = clean_gstin(df["gstin"])
    df["email"] = df["email"].map(lambda t: ", ".join(split_emails(t)))
    errors = pd.Series("", index=df.index, dtype=object)
    for check in (df["gstin"].map(validate_gstin), df["email"].map(validate_email_list), df["phone"].map(validate_phone)):
        errors = errors.where(check == "", (errors + "; " + check).str.lstrip("; "))
    df["error"] = errors
    blank = (df[list(VENDOR_FIELDS)] == "").all(axis=1)
    df = df[~blank]
    valid = df[df["error"] == ""].drop_duplicates("gstin", keep="last")
    return valid[list(VENDOR_FIELDS)].reset_index(drop=True), df[df["error"] != ""].reset_index(drop=True)


def vendors_from_result(recon: pd.DataFrame) -> pd.DataFrame:
    """Vendor rows (GSTIN + name) for every valid GSTIN found in a reconciliation."""
    rows = recon[["gstin", "party_name"]].rename(columns={"party_name": "name"})
    rows = rows[rows["gstin"].map(validate_gstin) == ""]
    rows = rows.sort_values("name", ascending=False).drop_duplicates("gstin")
    return rows.assign(email="", phone="", contact_person="", notes="")[list(VENDOR_FIELDS)].reset_index(drop=True)
