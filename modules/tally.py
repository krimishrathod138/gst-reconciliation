"""
Tally Purchase / Sales Register exports.

Tally (ERP 9 / Prime: *Display More Reports -> Account Books -> Purchase Register /
Sales Register -> Export -> Excel*) produces a sheet with company / period title
rows followed by one row per voucher:

    Date | Particulars | Voucher Type | Voucher No. | [Supplier Invoice No. / Reference No.]
    | [Supplier Invoice Date / Reference Date] | GSTIN/UIN | [Value] | Gross Total
    | <one column per ledger: "Purchase @ 18%", "Input CGST @ 9%", "Input IGST", "Round Off", ...>

This module recognises that layout and converts it into the standard register
columns (``Supplier GSTIN``, ``Invoice No``, ``Taxable Value``, ``IGST`` ...), which
the normal column-mapping then picks up automatically:

* **Invoice number / date** - purchases: supplier's invoice number and date
  (``Supplier Invoice No.`` / ``Reference No.`` / ``Voucher Ref. No.``), falling back
  to the voucher number / date; sales: the voucher number and date.
* **Tax heads** - sum of ledger columns whose names contain IGST / Integrated,
  CGST / Central, SGST / UTGST / State, Cess.
* **Taxable value** - an explicit ``Taxable Value`` / ``Assessable Value`` column if
  present, else the sum of purchase / sales ledger columns, else ``Value``, else
  ``Gross Total - taxes - round off``.
* **Document type** - Tally voucher types are translated to what the GST portal
  shows: in a *purchase* register a Tally *Debit Note* (purchase return) is the
  supplier's credit note (CN) and a Tally *Credit Note* is a debit note (DN); in a
  *sales* register Credit Note = CN and Debit Note = DN.
* ``Dr`` / ``Cr`` suffixes on amounts, cancelled vouchers and total rows are handled.
"""
from __future__ import annotations

import re

import numpy as np
import pandas as pd

from config import normalise_header
from modules.cleaner import clean_text, parse_amounts, text_id

# Normalised header names
_PARTICULARS = {"particulars", "partyname", "partyledger", "ledger"}
_VCH_TYPE = {"vouchertype", "vchtype"}
_VCH_NO = {"voucherno", "vchno", "vouchernumber"}
_GROSS = {"grosstotal", "gross", "total", "invoicevalue", "amount"}
_GSTIN = {"gstinuin", "gstin", "partygstinuin", "partygstin", "gstinofparty"}
_SUP_INV_NO = ("supplierinvoiceno", "supplierinvoicenumber", "referenceno", "vouchrefno", "voucherrefno",
               "refno", "supplierinvno", "partyinvoiceno", "billno")
_SUP_INV_DATE = ("supplierinvoicedate", "referencedate", "voucherrefdate", "refdate", "partyinvoicedate",
                 "billdate")
_TAXABLE_EXPLICIT = ("taxablevalue", "assessablevalue", "taxableamount")
_POS = ("placeofsupply", "pos", "stateofsupply")
_NON_LEDGER = (_PARTICULARS | _VCH_TYPE | _VCH_NO | _GROSS | _GSTIN | {"date", "value", "quantity", "rate",
               "narration", "vouchernarration", "address", "buyersaddress", "state", "statename", "pan"}
               | set(_SUP_INV_NO) | set(_SUP_INV_DATE) | set(_TAXABLE_EXPLICIT) | set(_POS))

_TAX_PATTERNS = {
    "igst": r"igst|integrated",
    "cgst": r"cgst|central",
    "sgst": r"sgst|utgst|statetax|stateut",
    "cess": r"cess",
}
_ROUND = r"round"
_TRADE_LEDGER = r"purchase|sales|sale|local|interstate|intrastate|import|export|sez|exempt|nilrated|nongst|jobwork|service"


def _norm_map(columns) -> dict[str, str]:
    return {c: normalise_header(c) for c in columns if not str(c).startswith("__")}


def is_tally_register(frame: pd.DataFrame) -> bool:
    """True when a table looks like a Tally Purchase / Sales Register export."""
    norms = set(_norm_map(frame.columns).values())
    return bool(norms & _PARTICULARS) and bool(norms & _VCH_TYPE) and bool(norms & _VCH_NO)


def _first(frame: pd.DataFrame, norms: dict[str, str], names) -> str | None:
    for name in names:
        for col, n in norms.items():
            if n == name:
                return col
    return None


def _sum_columns(frame: pd.DataFrame, cols: list[str]) -> pd.Series:
    if not cols:
        return pd.Series(0.0, index=frame.index)
    return sum((tally_amounts(frame[c]) for c in cols), pd.Series(0.0, index=frame.index))


def tally_amounts(series: pd.Series, signed: bool = False) -> pd.Series:
    """Amounts as exported by Tally ("1,180.00 Dr", "(50.00)", blanks).

    Args:
        signed: keep the sign (for Round Off, which can reduce the voucher total);
            a ``Cr`` suffix then means negative.  Otherwise magnitudes are returned.
    """
    txt = series.astype(object).where(series.notna(), None)
    credit = pd.Series(False, index=series.index)
    if not pd.api.types.is_numeric_dtype(series):
        credit = txt.map(lambda v: isinstance(v, str) and bool(re.search(r"Cr\.?\s*$", v, flags=re.I))).astype(bool)
        txt = txt.map(lambda v: re.sub(r"\s*(Dr|Cr)\.?\s*$", "", v, flags=re.I) if isinstance(v, str) else v)
    values = parse_amounts(pd.Series(txt, index=series.index))
    if not signed:
        return values.abs()
    return values.where(~credit, -values.abs())


def _note_type(voucher_type: pd.Series, mode: str) -> pd.Series:
    vt = clean_text(voucher_type).str.upper()
    debit_vch = vt.str.contains(r"DEBIT|\bDN\b|PURCHASE RETURN|PURCHASES RETURN", regex=True)
    credit_vch = vt.str.contains(r"CREDIT|\bCN\b|SALES RETURN|SALE RETURN", regex=True) & ~debit_vch
    if mode == "purchase":   # our debit note = supplier's credit note (and vice versa)
        return pd.Series(np.select([debit_vch, credit_vch], ["C", "D"], ""), index=voucher_type.index)
    return pd.Series(np.select([credit_vch, debit_vch], ["C", "D"], ""), index=voucher_type.index)


def convert_tally_register(frame: pd.DataFrame, mode: str = "purchase") -> tuple[pd.DataFrame, list[str]]:
    """Convert a Tally register export to standard register columns.

    Args:
        frame: Raw table read by :func:`modules.loader.read_table`.
        mode: ``"purchase"`` or ``"sales"`` (decides invoice-number source and CN/DN meaning).

    Returns:
        ``(standard_frame, notes)`` - notes list which Tally columns were used.
    """
    norms = _norm_map(frame.columns)
    notes: list[str] = []
    col_party = _first(frame, norms, sorted(_PARTICULARS))
    col_vtype = _first(frame, norms, sorted(_VCH_TYPE))
    col_vno = _first(frame, norms, sorted(_VCH_NO))
    col_date = _first(frame, norms, ["date", "voucherdate", "vchdate"])
    col_gstin = _first(frame, norms, ["gstinuin", "gstin", "partygstinuin", "partygstin", "gstinofparty"])
    col_gross = _first(frame, norms, ["grosstotal", "gross", "invoicevalue", "total", "amount"])
    col_pos = _first(frame, norms, _POS)

    party = clean_text(frame[col_party]) if col_party else pd.Series("", index=frame.index)
    vno = text_id(frame[col_vno]) if col_vno else pd.Series("", index=frame.index)
    cancelled = party.str.contains(r"cancel", case=False, regex=True)
    if mode == "purchase":
        col_supinv = _first(frame, norms, _SUP_INV_NO)
        col_supdate = _first(frame, norms, _SUP_INV_DATE)
        sup = text_id(frame[col_supinv]) if col_supinv else pd.Series("", index=frame.index)
        invoice_no = sup.where(sup != "", vno)
        fallback = int(((sup == "") & (vno != "") & ~cancelled).sum())
        notes.append(f"Invoice no: '{col_supinv}'" if col_supinv else "Invoice no: voucher number (no supplier "
                     "invoice column found - enable 'Supplier Invoice No.' in the Tally export for best matching)")
        if col_supinv and fallback:
            notes.append(f"{fallback} voucher(s) without supplier invoice no - voucher number used")
        date_src = frame[col_date] if col_date else pd.Series(None, index=frame.index)
        if col_supdate:
            sup_date = frame[col_supdate]
            invoice_date = sup_date.where(sup_date.notna() & (sup_date.astype(str).str.strip() != ""), date_src)
        else:
            invoice_date = date_src
    else:
        invoice_no = vno
        invoice_date = frame[col_date] if col_date else pd.Series(None, index=frame.index)
        notes.append("Invoice no: voucher number")

    ledger_cols = [c for c, n in norms.items() if n not in _NON_LEDGER and c != col_pos]
    taxes = {}
    used: set[str] = set()
    for head, pattern in _TAX_PATTERNS.items():
        cols = [c for c in ledger_cols if c not in used and re.search(pattern, norms[c])]
        used |= set(cols)
        taxes[head] = _sum_columns(frame, cols)
        if cols:
            notes.append(f"{head.upper()}: {', '.join(map(str, cols))}")
    round_cols = [c for c in ledger_cols if c not in used and re.search(_ROUND, norms[c])]
    used |= set(round_cols)

    col_taxable = _first(frame, norms, _TAXABLE_EXPLICIT)
    trade_cols = [c for c in ledger_cols if c not in used and re.search(_TRADE_LEDGER, norms[c])]
    gross = tally_amounts(frame[col_gross]) if col_gross else pd.Series(0.0, index=frame.index)
    total_tax = taxes["igst"] + taxes["cgst"] + taxes["sgst"] + taxes["cess"]
    if col_taxable:
        taxable = tally_amounts(frame[col_taxable])
        notes.append(f"Taxable value: '{col_taxable}'")
    elif col_gross:
        # Every non-tax ledger on the voucher (purchase, freight, packing ...) forms the taxable value.
        round_off = sum((tally_amounts(frame[c], signed=True) for c in round_cols), pd.Series(0.0, index=frame.index))
        taxable = (gross - total_tax - round_off).clip(lower=0)
        notes.append("Taxable value: Gross Total - taxes - round off")
    elif trade_cols:
        taxable = _sum_columns(frame, trade_cols)
        notes.append(f"Taxable value: {', '.join(map(str, trade_cols))}")
    else:
        col_value = _first(frame, norms, ["value"])
        taxable = tally_amounts(frame[col_value]) if col_value else pd.Series(0.0, index=frame.index)
        notes.append("Taxable value: 'Value'" if col_value else "Taxable value: not found (set to 0)")

    out = pd.DataFrame({
        "Supplier GSTIN": frame[col_gstin] if col_gstin else None,
        "Supplier Name": party,
        "Invoice No": invoice_no,
        "Invoice Date": invoice_date,
        "Invoice Type": clean_text(frame[col_vtype]) if col_vtype else "",
        "Note Type": _note_type(frame[col_vtype], mode) if col_vtype else "",
        "Taxable Value": taxable.round(2),
        "IGST": taxes["igst"].round(2),
        "CGST": taxes["cgst"].round(2),
        "SGST": taxes["sgst"].round(2),
        "Cess": taxes["cess"].round(2),
        "Total Invoice Value": gross.round(2),
        "Place of Supply": frame[col_pos] if col_pos else None,
    }, index=frame.index)
    if "__source_row__" in frame:
        out["__source_row__"] = frame["__source_row__"]

    # Drop cancelled vouchers and total / blank rows.
    totals = party.str.fullmatch(r"(grand\s*)?total", case=False) & (vno == "")
    blank = (vno == "") & (invoice_no == "") & (party == "")
    if cancelled.any():
        notes.append(f"{int(cancelled.sum())} cancelled voucher(s) ignored")
    out = out[~(cancelled | totals | blank)].reset_index(drop=True)
    notes.insert(0, f"Tally {'Purchase' if mode == 'purchase' else 'Sales'} Register detected - {len(out)} vouchers")
    return out, notes
