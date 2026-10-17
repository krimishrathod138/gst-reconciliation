"""
Data cleaning and normalisation.

All functions operate on whole pandas Series (vectorised string / datetime
operations) so that registers with 1 lakh+ rows clean in seconds.  Python-level
loops are limited to *unique* values (e.g. GSTIN checksums) or to a short list
of date formats.

The main entry point is :func:`clean_dataset`, which converts a canonical frame
(see :func:`modules.loader.apply_mapping`) into the typed frame used by the matcher.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime

import numpy as np
import pandas as pd

from config import (
    DATE_FORMATS, DOC_CREDIT, DOC_DEBIT, DOC_INVOICE, FIELD_SPECS, FY_YEAR_RANGE, PERIOD_FORMATS,
    STATE_CODES, TAX_FIELDS, VALID_STATE_CODES,
)

# --------------------------------------------------------------------------- #
# GSTIN validation
# --------------------------------------------------------------------------- #
_GSTIN_CHARS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
_PAN_RE = re.compile(r"^[A-Z]{5}[0-9]{4}[A-Z]$")


def gstin_check_char(first14: str) -> str:
    """Compute the 15th (checksum) character of a GSTIN from its first 14 characters.

    Uses the official mod-36 algorithm: alternate factors 1 and 2, add quotient and
    remainder of each product divided by 36, check = (36 - sum mod 36) mod 36.
    """
    total = 0
    for i, ch in enumerate(first14.upper()):
        product = _GSTIN_CHARS.index(ch) * (2 if i % 2 else 1)
        total += product // 36 + product % 36
    return _GSTIN_CHARS[(36 - total % 36) % 36]


def validate_gstin(gstin: object) -> str:
    """Validate one GSTIN.

    Returns:
        ``""`` when valid, otherwise a short reason suitable for reports.
    """
    g = "" if gstin is None or (isinstance(gstin, float) and np.isnan(gstin)) else str(gstin).strip().upper()
    if not g:
        return "GSTIN is blank"
    if len(g) != 15:
        return f"Must be 15 characters (found {len(g)})"
    if not g.isalnum() or not g.isascii():
        return "Contains invalid characters"
    if g[:2] not in VALID_STATE_CODES:
        return f"Invalid state code '{g[:2]}'"
    if not _PAN_RE.match(g[2:12]):
        return "Invalid PAN structure (characters 3-12)"
    if g[12] == "0":
        return "Invalid entity number (13th character)"
    expected = gstin_check_char(g[:14])
    if g[14] != expected:
        return f"Checksum failed (expected last character '{expected}')"
    return ""


def gstin_errors(series: pd.Series) -> pd.Series:
    """Vectorised :func:`validate_gstin` - validates each *unique* GSTIN once."""
    uniques = pd.unique(series)
    lookup = {u: validate_gstin(u) for u in uniques}
    return series.map(lookup).astype(object)


# --------------------------------------------------------------------------- #
# Text helpers
# --------------------------------------------------------------------------- #
def clean_text(series: pd.Series) -> pd.Series:
    """Strip, collapse internal whitespace and turn missing values into ``""``."""
    s = series.astype(object)
    s = s.where(s.notna(), "")
    return s.astype(str).str.strip().str.replace(r"\s+", " ", regex=True)


def clean_gstin(series: pd.Series) -> pd.Series:
    """Uppercase GSTINs and remove every whitespace character."""
    return clean_text(series).str.upper().str.replace(r"\s", "", regex=True)


def text_id(series: pd.Series) -> pd.Series:
    """Convert identifier cells to text; Excel numbers like ``123.0`` become ``"123"``."""
    s = series.astype(object)
    is_int_float = s.map(lambda v: isinstance(v, float) and v == v and float(v).is_integer())
    out = clean_text(s)
    if is_int_float.any():
        out[is_int_float] = s[is_int_float].map(lambda v: str(int(v)))
    return out


# --------------------------------------------------------------------------- #
# Invoice number normalisation
# --------------------------------------------------------------------------- #
def _build_fy_regex() -> re.Pattern[str]:
    """Regex matching financial-year tokens such as 2024-25, 2024/2025, 24-25, FY24-25."""
    alts: list[str] = []
    for y in range(*FY_YEAR_RANGE):
        n = y + 1
        yy, nn = f"{y % 100:02d}", f"{n % 100:02d}"
        alts += [rf"{y}\s*[-/]\s*{n}", rf"{y}\s*[-/]\s*{nn}", rf"{yy}\s*[-/]\s*{nn}"]
    return re.compile(r"(?<![0-9])(?:F\.?Y\.?\s*)?(?:" + "|".join(alts) + r")(?![0-9])")


FY_REGEX = _build_fy_regex()


def normalise_invoice_no(series: pd.Series) -> pd.Series:
    """Normalise invoice numbers for matching (display keeps the original).

    Steps: uppercase -> remove FY tokens ("2024-25", "24-25", "FY2024-25") ->
    drop leading zeros of every digit run ("INV/0012" -> "INV/12") -> remove all
    non-alphanumerics (spaces, / - . # etc.).  If nothing is left (e.g. the
    number *was* "24-25") the plain alphanumeric form is used instead.

    >>> normalise_invoice_no(pd.Series(["INV/2024-25/0012", "inv-12", "24-25/12"])).tolist()
    ['INV12', 'INV12', '12']
    """
    raw = text_id(series).str.upper()
    x = raw.str.replace(FY_REGEX, "", regex=True)
    x = x.str.replace(r"(?<![0-9])0+(?=[0-9])", "", regex=True)
    x = x.str.replace(r"[^A-Z0-9]", "", regex=True)
    fallback = raw.str.replace(r"[^A-Z0-9]", "", regex=True)
    return x.where(x != "", fallback)


# --------------------------------------------------------------------------- #
# Dates
# --------------------------------------------------------------------------- #
def parse_dates(series: pd.Series) -> pd.Series:
    """Parse dates given in mixed formats into ``datetime64`` (time stripped).

    Handles real datetime cells, Excel serial numbers and strings in any of
    :data:`config.DATE_FORMATS` (day-first formats take precedence).  Unparseable
    values become ``NaT``.
    """
    if pd.api.types.is_datetime64_any_dtype(series):
        s = pd.to_datetime(series)
        if getattr(s.dt, "tz", None) is not None:
            s = s.dt.tz_localize(None)
        return s.dt.normalize().astype("datetime64[ns]")

    obj = series.astype(object)
    out = pd.Series(pd.NaT, index=series.index, dtype="datetime64[ns]")

    is_dt = obj.map(lambda v: isinstance(v, (datetime, date)) and not pd.isna(v))
    if is_dt.any():
        out.loc[is_dt] = pd.to_datetime(obj[is_dt].map(pd.Timestamp)).to_numpy()

    rest = obj.where(~is_dt)
    num = pd.to_numeric(rest, errors="coerce")
    serial = num.between(20000, 80000)  # 1954..2119 as Excel serial days
    if serial.any():
        out.loc[serial] = pd.to_datetime(num[serial], unit="D", origin="1899-12-30").to_numpy()

    remaining = out.isna() & rest.notna()
    strs = clean_text(rest[remaining])
    strs = strs[strs != ""]
    for fmt in DATE_FORMATS:
        if strs.empty:
            break
        parsed = pd.to_datetime(strs, format=fmt, errors="coerce")
        ok = _plausible(parsed)
        if ok.any():
            out.loc[parsed.index[ok]] = parsed[ok].astype("datetime64[ns]").to_numpy()
            strs = strs[~ok]
    if not strs.empty:
        parsed = pd.to_datetime(strs, errors="coerce", dayfirst=True, format="mixed")
        ok = _plausible(parsed)
        out.loc[parsed.index[ok]] = parsed[ok].astype("datetime64[ns]").to_numpy()
    return out.dt.normalize()


def _plausible(parsed: pd.Series) -> pd.Series:
    """True where a parsed date exists and its year is between 1990 and 2100 (rejects '132024' -> 1320)."""
    if not pd.api.types.is_datetime64_any_dtype(parsed):
        parsed = pd.to_datetime(parsed, errors="coerce")
    return parsed.notna() & parsed.dt.year.between(1990, 2100)


def format_dates(series: pd.Series, fmt: str = "%d-%m-%Y") -> pd.Series:
    """Format datetimes as DD-MM-YYYY strings (``""`` for NaT)."""
    return series.dt.strftime(fmt).fillna("")


def fy_from_dates(dates: pd.Series) -> pd.Series:
    """Indian financial year label (April-March), e.g. 15-05-2024 -> ``"2024-25"``."""
    valid = dates.notna()
    out = pd.Series("", index=dates.index, dtype=object)
    if valid.any():
        d = dates[valid]
        start = (d.dt.year - (d.dt.month < 4).astype(int)).astype(int)
        out[valid] = start.astype(str) + "-" + ((start + 1) % 100).astype(str).str.zfill(2)
    return out


def normalise_period(series: pd.Series) -> pd.Series:
    """Normalise return periods (``052024``, ``05-2024``, ``May'24``, ``2024-05``, dates) to ``YYYY-MM``.

    Unrecognised or blank values become ``""``.
    """
    obj = series.astype(object)
    out = pd.Series("", index=series.index, dtype=object)

    is_dt = obj.map(lambda v: isinstance(v, (datetime, date)) and not pd.isna(v))
    if is_dt.any():
        out[is_dt] = pd.to_datetime(obj[is_dt].map(pd.Timestamp)).dt.strftime("%Y-%m")

    txt = text_id(obj.where(~is_dt)).str.strip()
    todo = (out == "") & (txt != "")

    # MMYYYY (also 5-digit when the leading zero was lost in Excel)
    digits = todo & txt.str.fullmatch(r"\d{5,6}")
    if digits.any():
        z = txt[digits].str.zfill(6)
        mm, yyyy = z.str[:2], z.str[2:]
        good = mm.astype(int).between(1, 12)
        out[z.index[good]] = (yyyy + "-" + mm)[good]

    for pattern, year_first in ((r"^(\d{1,2})\s*[-/.]\s*(\d{4})$", False), (r"^(\d{4})\s*[-/.]\s*(\d{1,2})$", True)):
        todo = (out == "") & (txt != "")
        if not todo.any():
            break
        ext = txt[todo].str.extract(pattern).dropna()
        if ext.empty:
            continue
        yyyy, mm = (ext[0], ext[1]) if year_first else (ext[1], ext[0])
        good = mm.astype(int).between(1, 12)
        out[ext.index[good]] = (yyyy + "-" + mm.str.zfill(2))[good]

    todo = (out == "") & (txt != "")
    strs = txt[todo]
    for fmt in PERIOD_FORMATS:
        if strs.empty:
            break
        parsed = pd.to_datetime(strs.str.title(), format=fmt, errors="coerce")
        ok = parsed.notna()
        if ok.any():
            out[parsed.index[ok]] = parsed[ok].dt.strftime("%Y-%m")
            strs = strs[~ok]
    if not strs.empty:
        parsed = parse_dates(strs)
        ok = parsed.notna()
        out[parsed.index[ok]] = parsed[ok].dt.strftime("%Y-%m")
    return out


# --------------------------------------------------------------------------- #
# Amounts, document types, place of supply
# --------------------------------------------------------------------------- #
def round_money(values: pd.Series) -> pd.Series:
    """Round to 2 decimals, half away from zero (12.345 -> 12.35 despite binary floats)."""
    return (values + np.sign(values) * 1e-9).round(2)


def parse_amounts(series: pd.Series) -> pd.Series:
    """Convert amounts such as ``"₹ 1,23,456.789"`` or ``"(500)"`` to floats rounded to 2 decimals.

    Blank / unparseable values become 0.
    """
    if pd.api.types.is_numeric_dtype(series):
        return round_money(pd.to_numeric(series, errors="coerce").astype(float).fillna(0.0))
    obj = series.astype(object)
    num = pd.to_numeric(obj, errors="coerce").astype(float)
    need = num.isna() & obj.notna()
    if need.any():
        txt = obj[need].astype(str).str.strip()
        txt = txt.str.replace(r"(?i)rs\.?|inr|₹|,|\s", "", regex=True)
        txt = txt.str.replace(r"^\((.*)\)$", r"-\1", regex=True)
        txt = txt.str.replace(r"^(.*)-$", r"-\1", regex=True)  # trailing minus (SAP style)
        num[need] = pd.to_numeric(txt, errors="coerce")
    return round_money(num.fillna(0.0))


_CREDIT_TYPE = r"CREDIT|^CN$|^C$|^CRN$|^CDNR?A?$|^CDNR?-?C$|^B2B-?CDNRA?$"
_DEBIT_TYPE = r"DEBIT|^DN$|^D$|^DRN$|^CDNR?-?D$"


def derive_doc_type(invoice_type: pd.Series, note_type: pd.Series | None = None) -> pd.Series:
    """Map free-text document types to ``INV`` / ``CN`` / ``DN``.

    A populated *note_type* (C/D/Credit/Debit) wins over *invoice_type*; a bare
    ``CDNR`` without note type is treated as a credit note (the common case).
    """
    it = clean_text(invoice_type).str.upper()
    nt = clean_text(note_type if note_type is not None else pd.Series("", index=invoice_type.index)).str.upper()
    nt_debit = nt.str.contains(r"^D|DEBIT", regex=True)
    nt_credit = nt.str.contains(r"^C|CREDIT", regex=True) & ~nt_debit
    it_debit = it.str.contains(_DEBIT_TYPE, regex=True)
    it_credit = it.str.contains(_CREDIT_TYPE, regex=True) & ~it_debit
    doc = np.select([nt_debit, nt_credit, it_debit, it_credit],
                    [DOC_DEBIT, DOC_CREDIT, DOC_DEBIT, DOC_CREDIT], DOC_INVOICE)
    return pd.Series(doc, index=invoice_type.index, dtype=object)


_STATE_NAME_TO_CODE: dict[str, str] = {v.upper(): k for k, v in STATE_CODES.items()}
_STATE_NAME_TO_CODE.update({"ANDHRA PRADESH": "37", "DADRA AND NAGAR HAVELI": "26", "ORISSA": "21",
                            "PONDICHERRY": "34", "NEW DELHI": "07", "JAMMU & KASHMIR": "01",
                            "ANDAMAN & NICOBAR ISLANDS": "35", "UTTARANCHAL": "05"})


def normalise_pos(series: pd.Series) -> pd.Series:
    """Place of supply as a two-digit state code ("27-Maharashtra", "Maharashtra", "27" -> "27")."""
    txt = clean_text(series)
    code = txt.str.extract(r"^(\d{1,2})(?!\d)")[0].str.zfill(2)
    by_name = txt.str.upper().str.replace(r"^\d+\s*[-:]?\s*", "", regex=True).map(_STATE_NAME_TO_CODE)
    return code.fillna(by_name).fillna(txt).astype(object)


# --------------------------------------------------------------------------- #
# Dataset cleaning
# --------------------------------------------------------------------------- #
CLEAN_COLUMNS: tuple[str, ...] = (
    "_rid", "gstin", "gstin_error", "party_name", "invoice_no", "inv_norm", "invoice_date", "doc_type",
    "invoice_type", "taxable_value", "igst", "cgst", "sgst", "cess", "total_tax", "invoice_value",
    "place_of_supply", "period", "month", "fy", "itc_available", "sign", "is_duplicate", "dup_count",
    "dup_seq", "section", "source_file", "source_row", "carried_from",
)


@dataclass
class CleanReport:
    """What the cleaner changed or flagged, for display in the UI."""

    dataset: str
    rows_in: int = 0
    rows_out: int = 0
    dropped_blank: int = 0
    dropped_no_invoice: pd.DataFrame = field(default_factory=pd.DataFrame)
    invalid_dates: int = 0
    invalid_gstins: int = 0
    duplicate_rows: int = 0
    computed_invoice_value: int = 0

    def messages(self) -> list[str]:
        """Human-readable summary lines."""
        msgs = [f"{self.rows_out:,} documents ready ({self.rows_in:,} rows read)"]
        if self.dropped_blank:
            msgs.append(f"{self.dropped_blank:,} blank/total rows ignored")
        if len(self.dropped_no_invoice):
            msgs.append(f"{len(self.dropped_no_invoice):,} rows without invoice number excluded")
        if self.invalid_dates:
            msgs.append(f"{self.invalid_dates:,} rows with unreadable dates")
        if self.invalid_gstins:
            msgs.append(f"{self.invalid_gstins:,} rows with invalid GSTIN")
        if self.duplicate_rows:
            msgs.append(f"{self.duplicate_rows:,} rows are duplicates")
        return msgs


def empty_clean_frame() -> pd.DataFrame:
    """Empty frame with the cleaned column layout."""
    return pd.DataFrame({c: pd.Series(dtype="datetime64[ns]" if c == "invoice_date" else object)
                         for c in CLEAN_COLUMNS})


def clean_dataset(df: pd.DataFrame | None, dataset: str = "books") -> tuple[pd.DataFrame, CleanReport]:
    """Normalise a canonical frame into the typed layout used by the matcher.

    Args:
        df: Canonical frame (columns = :data:`config.FIELD_SPECS` keys, plus optional
            ``source_file``, ``source_row``, ``section``).
        dataset: ``"books"`` or ``"portal"`` (only used for labelling).

    Returns:
        ``(clean_frame, report)``.  Amounts are stored as positive magnitudes;
        ``sign`` is -1 for credit notes so that ``sign * amount`` nets correctly.
    """
    report = CleanReport(dataset=dataset)
    if df is None or df.empty:
        return empty_clean_frame(), report
    work = df.reset_index(drop=True).copy()
    for col in [f.key for f in FIELD_SPECS] + ["source_file", "source_row", "section"]:
        if col not in work:
            work[col] = None
    report.rows_in = len(work)

    gstin = clean_gstin(work["gstin"])
    inv = text_id(work["invoice_no"])
    blank = (gstin == "") & (inv == "")
    no_inv = (inv == "") & ~blank
    report.dropped_blank = int(blank.sum())
    report.dropped_no_invoice = work[no_inv].copy()
    keep = ~(blank | no_inv)
    work, gstin, inv = work[keep].reset_index(drop=True), gstin[keep].reset_index(drop=True), inv[keep].reset_index(drop=True)

    out = pd.DataFrame(index=work.index)
    out["_rid"] = np.arange(len(work))
    out["gstin"] = gstin
    out["gstin_error"] = gstin_errors(gstin)
    out["party_name"] = clean_text(work["party_name"])
    out["invoice_no"] = inv
    out["inv_norm"] = normalise_invoice_no(inv)
    out["invoice_date"] = parse_dates(work["invoice_date"])
    out["doc_type"] = derive_doc_type(work["invoice_type"], work["note_type"])
    out["invoice_type"] = clean_text(work["invoice_type"])

    for col in ("taxable_value", *TAX_FIELDS, "invoice_value"):
        out[col] = parse_amounts(work[col]).abs()
    out["total_tax"] = out[list(TAX_FIELDS)].sum(axis=1).round(2)
    computed = (out["invoice_value"] == 0) & ((out["taxable_value"] + out["total_tax"]) > 0)
    out.loc[computed, "invoice_value"] = (out["taxable_value"] + out["total_tax"])[computed].round(2)
    report.computed_invoice_value = int(computed.sum())

    out["place_of_supply"] = normalise_pos(work["place_of_supply"])
    out["month"] = out["invoice_date"].dt.strftime("%Y-%m").fillna("")
    period = normalise_period(work["return_period"])
    out["period"] = period.where(period != "", out["month"])
    out["fy"] = fy_from_dates(out["invoice_date"])
    itc = clean_text(work["itc_available"]).str.upper()
    out["itc_available"] = np.where(itc.str.startswith("N"), "N", "Y")
    out["sign"] = np.where(out["doc_type"] == DOC_CREDIT, -1, 1)

    out["section"] = clean_text(work["section"])
    out["source_file"] = clean_text(work["source_file"])
    out["source_row"] = pd.to_numeric(work["source_row"], errors="coerce").astype("Int64")
    out["carried_from"] = ""
    out = recompute_keys(out)

    report.rows_out = len(out)
    report.invalid_dates = int(out["invoice_date"].isna().sum())
    report.invalid_gstins = int((out["gstin_error"] != "").sum())
    report.duplicate_rows = int(out["is_duplicate"].sum())
    return out[list(CLEAN_COLUMNS)], report


def recompute_keys(clean: pd.DataFrame) -> pd.DataFrame:
    """(Re)assign ``_rid`` and the duplicate flags of a cleaned frame.

    Needed whenever cleaned frames are filtered or combined (month-range
    selection, carried-forward documents) so that row ids stay unique.
    """
    out = clean.reset_index(drop=True).copy()
    if "carried_from" not in out:
        out["carried_from"] = ""
    out["carried_from"] = out["carried_from"].astype(object).where(out["carried_from"].notna(), "")
    out["_rid"] = np.arange(len(out))
    key = out["gstin"].astype(str) + "|" + out["doc_type"].astype(str) + "|" + out["inv_norm"].astype(str)
    grp = out.groupby(key, sort=False)
    out["dup_count"] = grp["_rid"].transform("size").astype(int) if len(out) else pd.Series(dtype=int)
    out["dup_seq"] = grp.cumcount().astype(int)
    out["is_duplicate"] = out["dup_count"] > 1
    return out


def combine_clean(frames: list[pd.DataFrame]) -> pd.DataFrame:
    """Concatenate cleaned frames and recompute keys."""
    frames = [f for f in frames if f is not None and len(f)]
    if not frames:
        return empty_clean_frame()
    return recompute_keys(pd.concat(frames, ignore_index=True))[list(CLEAN_COLUMNS)]


def duplicates_frame(clean: pd.DataFrame) -> pd.DataFrame:
    """Rows flagged as duplicates (same GSTIN + doc type + normalised invoice number)."""
    return clean[clean["is_duplicate"]].sort_values(["gstin", "inv_norm", "dup_seq"])


def invalid_gstin_frame(clean: pd.DataFrame) -> pd.DataFrame:
    """Rows whose GSTIN failed validation."""
    return clean[clean["gstin_error"] != ""]
