"""
Input loading.

* Purchase / sales registers in Excel (.xlsx/.xlsm/.xls) or CSV, with automatic
  header-row detection (title rows above the table and two-row merged headers,
  as in the portal's GSTR-2B Excel, are handled).
* GSTR-2B Excel downloaded from the GST portal (B2B / B2B-CDNR / amendment sheets).
* Official JSON downloads: GSTR-2B (``data.docdata``), GSTR-2A and GSTR-1
  (``b2b[].inv[].itms[]`` / ``cdnr[].nt[]``).
* Column-mapping helpers that translate user headers to canonical field keys.

Everything returned by this module is *raw* (strings, numbers as read); type
conversion and normalisation happen in :mod:`modules.cleaner`.
"""
from __future__ import annotations

import csv
import io
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, BinaryIO, Union

import numpy as np
import pandas as pd

from config import FIELD_BY_KEY, FIELD_SPECS, REQUIRED_FIELDS, normalise_header
from modules import ReconError
from modules.tally import convert_tally_register, is_tally_register

FileSource = Union[str, Path, bytes, BinaryIO]

EXCEL_EXTS = {".xlsx", ".xlsm", ".xls"}
CSV_EXTS = {".csv", ".txt"}
JSON_EXTS = {".json"}

# Portal Excel sheets we understand -> default invoice_type for that sheet.
PORTAL_SHEETS: dict[str, str] = {
    "b2b": "B2B", "b2ba": "B2B", "b2bcdnr": "CDNR", "b2bcdnra": "CDNR", "cdnr": "CDNR", "cdnra": "CDNR",
}
HEADER_SCAN_ROWS = 30
_ALL_ALIASES: frozenset[str] = frozenset(a for f in FIELD_SPECS for a in f.norm_aliases)


@dataclass
class LoadedFile:
    """Result of loading one uploaded file.

    Attributes:
        name: Original file name.
        kind: ``"table"`` (needs column mapping), ``"portal_json"`` or ``"portal_excel"``.
        frame: Raw table (``kind == "table"``) or canonical frame (portal kinds).
        sheets: Sheet names when the file is a workbook.
        sheet: Sheet actually read.
        meta: Extra information (e.g. return period / GSTIN found in JSON).
        notes: Human-readable notes for the UI.
    """

    name: str
    kind: str
    frame: pd.DataFrame
    sheets: list[str] = field(default_factory=list)
    sheet: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    @property
    def canonical(self) -> bool:
        return self.kind != "table"


# --------------------------------------------------------------------------- #
# Low level helpers
# --------------------------------------------------------------------------- #
def _read_source(source: FileSource, name: str | None = None) -> tuple[bytes, str]:
    """Return ``(bytes, file_name)`` for a path, raw bytes or file-like object (e.g. Streamlit upload)."""
    if isinstance(source, (str, Path)):
        path = Path(source)
        if not path.exists():
            raise ReconError(f"File not found: {path}")
        return path.read_bytes(), name or path.name
    if isinstance(source, bytes):
        return source, name or "uploaded_file"
    if hasattr(source, "getvalue"):
        data = source.getvalue()
    else:
        data = source.read()
    return data, name or getattr(source, "name", "uploaded_file")


def list_sheets(data: bytes) -> list[str]:
    """List worksheet names of an Excel workbook given as bytes."""
    try:
        with pd.ExcelFile(io.BytesIO(data)) as xls:
            return [str(s) for s in xls.sheet_names]
    except Exception as exc:  # noqa: BLE001 - convert any engine error into a user message
        raise ReconError(f"Could not open the Excel workbook ({exc}). Is the file corrupted or password protected?") from exc


def _alias_hits(values: pd.Series) -> int:
    """Count cells in a row that look like known column headers."""
    hits = 0
    for v in values.dropna():
        n = normalise_header(v)
        if n and (n in _ALL_ALIASES or any(n.startswith(a) for a in _ALL_ALIASES if len(a) >= 6)):
            hits += 1
    return hits


def detect_header(raw: pd.DataFrame, scan_rows: int = HEADER_SCAN_ROWS) -> tuple[int, int]:
    """Locate the header row in a sheet read with ``header=None``.

    Returns:
        ``(first_header_row, header_row_count)``; the count is 2 for two-level
        headers such as the portal GSTR-2B Excel ("Invoice details" over
        "Invoice number", "Invoice Date", ...).
    """
    if raw.empty:
        return 0, 1
    head = raw.head(scan_rows)
    hits = [(_alias_hits(head.iloc[i]), i) for i in range(len(head))]
    best_hits, best = max(hits, key=lambda t: (t[0], -t[1]))
    if best_hits < 2:
        return 0, 1
    if best > 0 and hits[best - 1][0] >= 2:
        return best - 1, 2
    if best + 1 < len(hits) and hits[best + 1][0] >= 2:
        return best, 2
    return best, 1


def _unique_names(names: list[str]) -> list[str]:
    """Make column names unique by suffixing duplicates with ``_2``, ``_3``..."""
    seen: dict[str, int] = {}
    out: list[str] = []
    for n in names:
        if n in seen:
            seen[n] += 1
            out.append(f"{n}_{seen[n]}")
        else:
            seen[n] = 1
            out.append(n)
    return out


def frame_from_raw(raw: pd.DataFrame) -> pd.DataFrame:
    """Turn a header-less sheet into a table using :func:`detect_header`.

    Adds ``__source_row__`` holding the 1-based row number in the original file,
    so users can trace every record back to their register.
    """
    if raw.empty:
        return pd.DataFrame()
    start, count = detect_header(raw)
    top = raw.iloc[start]
    if count == 2:
        bottom = raw.iloc[start + 1]
        names = [
            str(b).strip() if pd.notna(b) and str(b).strip() else (str(t).strip() if pd.notna(t) else "")
            for t, b in zip(top, bottom)
        ]
    else:
        names = [str(t).strip() if pd.notna(t) else "" for t in top]
    names = [n if n else f"Column_{i + 1}" for i, n in enumerate(names)]
    body = raw.iloc[start + count:].copy()
    body.columns = _unique_names(names)
    body["__source_row__"] = np.arange(start + count + 1, start + count + 1 + len(body))
    # Drop fully blank rows and unnamed empty columns.
    data_cols = [c for c in body.columns if c != "__source_row__"]
    body = body.dropna(how="all", subset=data_cols)
    empty_unnamed = [c for c in data_cols if c.startswith("Column_") and body[c].isna().all()]
    body = body.drop(columns=empty_unnamed)
    return body.reset_index(drop=True)


def _decode_text(data: bytes) -> str:
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    raise ReconError("Could not decode the CSV file. Please save it as UTF-8.")


def _read_csv_raw(text: str) -> pd.DataFrame:
    """Read CSV text header-less, sniffing the delimiter and tolerating ragged title rows."""
    try:
        delim = csv.Sniffer().sniff(text[:5000], delimiters=",;\t|").delimiter
    except csv.Error:
        delim = ","
    try:
        return pd.read_csv(io.StringIO(text), header=None, dtype=str, sep=delim)
    except pd.errors.ParserError:
        rows = list(csv.reader(io.StringIO(text), delimiter=delim))
        width = max((len(r) for r in rows), default=0)
        raw = pd.DataFrame([r + [None] * (width - len(r)) for r in rows])
        return raw.replace("", np.nan)


def read_table(data: bytes, name: str, sheet: str | int | None = None) -> pd.DataFrame:
    """Read a CSV or one Excel sheet into a raw table with detected headers."""
    ext = Path(name).suffix.lower()
    try:
        if ext in CSV_EXTS:
            raw = _read_csv_raw(_decode_text(data))
        elif ext in EXCEL_EXTS:
            raw = pd.read_excel(io.BytesIO(data), sheet_name=0 if sheet is None else sheet,
                                header=None, dtype=object)
        else:
            raise ReconError(f"Unsupported file type '{ext}'. Use .xlsx, .xls, .csv or .json.")
    except ReconError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ReconError(f"Could not read '{name}': {exc}") from exc
    frame = frame_from_raw(raw)
    if frame.empty:
        raise ReconError(f"'{name}'{f' (sheet {sheet})' if sheet is not None else ''} contains no data rows.")
    return frame


# --------------------------------------------------------------------------- #
# Column mapping
# --------------------------------------------------------------------------- #
def suggest_mapping(columns: list[str] | pd.Index) -> dict[str, str | None]:
    """Guess which user column corresponds to each canonical field.

    Pass 1 looks for exact (normalised) alias matches in alias priority order;
    pass 2 accepts headers that *start with* a long alias, e.g. "Taxable Value as per books".
    """
    norm = {c: normalise_header(c) for c in columns if not str(c).startswith("__")}
    used: set[str] = set()
    mapping: dict[str, str | None] = {f.key: None for f in FIELD_SPECS}
    for spec in FIELD_SPECS:
        for alias in spec.norm_aliases:
            hit = next((c for c, n in norm.items() if n == alias and c not in used), None)
            if hit is not None:
                mapping[spec.key] = hit
                used.add(hit)
                break
    for spec in FIELD_SPECS:
        if mapping[spec.key] is not None:
            continue
        for alias in sorted(spec.norm_aliases, key=len, reverse=True):
            if len(alias) < 5:
                continue
            hit = next((c for c, n in norm.items() if c not in used and n.startswith(alias)), None)
            if hit is not None:
                mapping[spec.key] = hit
                used.add(hit)
                break
    return mapping


def missing_required(mapping: dict[str, str | None]) -> list[str]:
    """Labels of required fields that have not been mapped."""
    return [FIELD_BY_KEY[k].label for k in REQUIRED_FIELDS if not mapping.get(k)]


def apply_mapping(raw: pd.DataFrame, mapping: dict[str, str | None], source_file: str = "") -> pd.DataFrame:
    """Build a canonical frame (one column per :data:`FIELD_SPECS` key) from a raw table."""
    missing = missing_required(mapping)
    if missing:
        raise ReconError(f"Map these required columns first: {', '.join(missing)}")
    unknown = [c for c in mapping.values() if c and c not in raw.columns]
    if unknown:
        raise ReconError(f"Mapped column(s) not found in file: {', '.join(map(str, unknown))}")
    out = pd.DataFrame(index=raw.index)
    for spec in FIELD_SPECS:
        src = mapping.get(spec.key)
        out[spec.key] = raw[src].to_numpy() if src else None
    out["source_row"] = raw["__source_row__"].to_numpy() if "__source_row__" in raw else np.arange(2, len(raw) + 2)
    out["source_file"] = source_file
    return out.reset_index(drop=True)


def empty_canonical() -> pd.DataFrame:
    """An empty canonical frame with all expected columns."""
    return pd.DataFrame(columns=[f.key for f in FIELD_SPECS] + ["source_row", "source_file", "section"])


# --------------------------------------------------------------------------- #
# Portal Excel (GSTR-2B / 2A download)
# --------------------------------------------------------------------------- #
def portal_sheets(sheets: list[str]) -> list[str]:
    """Sheets of a workbook that look like portal document sections."""
    return [s for s in sheets if normalise_header(s) in PORTAL_SHEETS]


def is_portal_workbook(sheets: list[str]) -> bool:
    """True when the workbook has the portal's B2B (+ CDNR) sheet layout."""
    found = {normalise_header(s) for s in portal_sheets(sheets)}
    return "b2b" in found and len(sheets) > 1


def read_portal_excel(data: bytes, name: str, sheets: list[str] | None = None) -> tuple[pd.DataFrame, list[str]]:
    """Read every recognised section sheet of a portal Excel file into one canonical frame.

    Returns:
        ``(canonical_frame, notes)``.
    """
    sheets = sheets if sheets is not None else list_sheets(data)
    frames: list[pd.DataFrame] = []
    notes: list[str] = []
    for sheet in portal_sheets(sheets):
        try:
            raw = read_table(data, name, sheet)
        except ReconError:
            continue  # empty section
        mapping = suggest_mapping(raw.columns)
        if missing_required(mapping):
            notes.append(f"Sheet '{sheet}' skipped - could not identify: {', '.join(missing_required(mapping))}")
            continue
        canon = apply_mapping(raw, mapping, source_file=f"{name} [{sheet}]")
        default_type = PORTAL_SHEETS[normalise_header(sheet)]
        canon["invoice_type"] = canon["invoice_type"].where(
            canon["invoice_type"].notna() & (default_type == "B2B"), default_type)
        canon["section"] = sheet
        # Portal sheets end with total rows in some versions; drop rows without an invoice number.
        canon = canon[canon["invoice_no"].notna() & (canon["invoice_no"].astype(str).str.strip() != "")]
        frames.append(canon)
        notes.append(f"Sheet '{sheet}': {len(canon)} documents")
    if not frames:
        raise ReconError(f"No readable B2B / CDNR sheets found in '{name}'.")
    ignore = {"readme", "summary", "itcavailable", "itcnotavailable", "itcreversal", "itcrejected", "gstr2b"}
    other = [s for s in sheets if normalise_header(s) not in PORTAL_SHEETS and not any(
        normalise_header(s).startswith(i) for i in ignore)]
    if other:
        notes.append("Not reconciled (no invoice-level counterpart in the register): sheets " + ", ".join(other))
    return pd.concat(frames, ignore_index=True), notes


# --------------------------------------------------------------------------- #
# Portal JSON (GSTR-2B / GSTR-2A / GSTR-1)
# --------------------------------------------------------------------------- #
def _num(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _amounts(doc: dict[str, Any]) -> dict[str, float]:
    """Taxable value and taxes of a document, flat (2B) or item-wise (2A / GSTR-1)."""
    items = doc.get("itms")
    if isinstance(items, list) and items:
        tot = {"taxable_value": 0.0, "igst": 0.0, "cgst": 0.0, "sgst": 0.0, "cess": 0.0}
        for item in items:
            det = item.get("itm_det", item) if isinstance(item, dict) else {}
            tot["taxable_value"] += _num(det.get("txval"))
            tot["igst"] += _num(det.get("iamt", det.get("igst")))
            tot["cgst"] += _num(det.get("camt", det.get("cgst")))
            tot["sgst"] += _num(det.get("samt", det.get("sgst")))
            tot["cess"] += _num(det.get("csamt", det.get("cess")))
        return tot
    return {
        "taxable_value": _num(doc.get("txval")),
        "igst": _num(doc.get("igst", doc.get("iamt"))),
        "cgst": _num(doc.get("cgst", doc.get("camt"))),
        "sgst": _num(doc.get("sgst", doc.get("samt"))),
        "cess": _num(doc.get("cess", doc.get("csamt"))),
    }


def parse_portal_json(data: bytes | str | dict[str, Any]) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Parse an official GST portal JSON (GSTR-2B, GSTR-2A or GSTR-1).

    Returns:
        ``(canonical_frame, meta)`` where meta has ``gstin``, ``return_period`` and ``format``.
    """
    if isinstance(data, dict):
        obj = data
    else:
        try:
            obj = json.loads(data.decode("utf-8-sig") if isinstance(data, bytes) else data)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ReconError(f"Invalid JSON file: {exc}") from exc
    if not isinstance(obj, dict):
        raise ReconError("Unexpected JSON structure - expected an object at the top level.")

    root = obj.get("data", obj) if isinstance(obj.get("data"), dict) else obj
    docdata = root.get("docdata", root)
    period = root.get("rtnprd") or root.get("fp") or root.get("ret_period") or ""
    fmt = "GSTR-2B" if "docdata" in root else ("GSTR-1" if "fp" in root else "GSTR-2A/GSTR-1")
    records: list[dict[str, Any]] = []

    for section in ("b2b", "b2ba"):
        for party in docdata.get(section, []) or []:
            ctin = party.get("ctin", "")
            pname = party.get("trdnm", party.get("lglnm", ""))
            sup_period = party.get("supprd") or period
            for inv in party.get("inv", []) or []:
                records.append({
                    "gstin": ctin, "party_name": pname,
                    "invoice_no": inv.get("inum", ""),
                    "invoice_date": inv.get("dt", inv.get("idt", "")),
                    "invoice_type": "B2B", "note_type": None,
                    **_amounts(inv),
                    "invoice_value": _num(inv.get("val")),
                    "place_of_supply": inv.get("pos", ""),
                    "return_period": sup_period,
                    "itc_available": inv.get("itcavl", "Y"),
                    "section": section.upper(),
                })
    for section in ("cdnr", "cdnra", "cdn", "cdna"):
        for party in docdata.get(section, []) or []:
            ctin = party.get("ctin", "")
            pname = party.get("trdnm", party.get("lglnm", ""))
            sup_period = party.get("supprd") or period
            for nt in party.get("nt", []) or []:
                records.append({
                    "gstin": ctin, "party_name": pname,
                    "invoice_no": nt.get("ntnum", nt.get("nt_num", "")),
                    "invoice_date": nt.get("dt", nt.get("nt_dt", "")),
                    "invoice_type": "CDNR", "note_type": nt.get("typ", nt.get("ntty", "C")),
                    **_amounts(nt),
                    "invoice_value": _num(nt.get("val")),
                    "place_of_supply": nt.get("pos", ""),
                    "return_period": sup_period,
                    "itc_available": nt.get("itcavl", "Y"),
                    "section": section.upper(),
                })

    if not records:
        if not any(k in docdata for k in ("b2b", "b2ba", "cdnr", "cdnra", "cdn")):
            raise ReconError("JSON not recognised as a GSTR-2B / GSTR-2A / GSTR-1 download "
                             "(no 'b2b' or 'cdnr' sections found).")
        frame = empty_canonical()
    else:
        frame = pd.DataFrame.from_records(records)
        for col in empty_canonical().columns:
            if col not in frame:
                frame[col] = None
        frame["source_row"] = np.arange(1, len(frame) + 1)
    handled = {"b2b", "b2ba", "cdnr", "cdnra", "cdn", "cdna"}
    skipped = {}
    for key, value in docdata.items():
        if key.lower() in handled or not isinstance(value, (list, dict)) or not value:
            continue
        skipped[key.upper()] = sum(len(v.get("inv", v.get("nt", v.get("doclist", [v])))) if isinstance(v, dict) else 1
                                   for v in value) if isinstance(value, list) else 1
    meta = {"gstin": root.get("gstin", ""), "return_period": period, "format": fmt, "documents": len(records),
            "skipped_sections": skipped}
    return frame, meta


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #
def _tally_or_table(fname: str, frame: pd.DataFrame, dataset: str, mode: str, sheets: list[str] | None = None,
                    sheet: str | None = None) -> LoadedFile:
    """Wrap a raw table, converting Tally register exports to standard columns first."""
    if dataset == "books" and is_tally_register(frame):
        converted, notes = convert_tally_register(frame, mode)
        return LoadedFile(fname, "table", converted, sheets=sheets or [], sheet=sheet, meta={"tally": True},
                          notes=notes)
    return LoadedFile(fname, "table", frame, sheets=sheets or [], sheet=sheet)


def load_file(source: FileSource, name: str | None = None, dataset: str = "books",
              sheet: str | None = None, mode: str = "purchase") -> LoadedFile:
    """Load any supported file.

    Args:
        source: Path, bytes or file-like object (Streamlit ``UploadedFile`` works).
        name: File name (needed when *source* is bytes).
        dataset: ``"books"`` or ``"portal"``; portal workbooks get section detection.
        sheet: Specific Excel sheet to read; ``None`` = auto.
        mode: ``"purchase"`` or ``"sales"`` - used to interpret Tally voucher types.

    Raises:
        ReconError: with a user-friendly message on any problem.
    """
    data, fname = _read_source(source, name)
    if not data:
        raise ReconError(f"'{fname}' is empty.")
    ext = Path(fname).suffix.lower()

    if ext in JSON_EXTS:
        frame, meta = parse_portal_json(data)
        frame["source_file"] = fname
        notes = [f"{meta['format']} JSON - {meta['documents']} documents"
                 + (f", period {meta['return_period']}" if meta["return_period"] else "")]
        if meta["skipped_sections"]:
            notes.append("Not reconciled (no invoice-level counterpart in the register): "
                         + ", ".join(f"{k} ({v})" for k, v in meta["skipped_sections"].items()))
        return LoadedFile(fname, "portal_json", frame, meta=meta, notes=notes)

    if ext in EXCEL_EXTS:
        sheets = list_sheets(data)
        if dataset == "portal" and sheet is None and is_portal_workbook(sheets):
            frame, notes = read_portal_excel(data, fname, sheets)
            return LoadedFile(fname, "portal_excel", frame, sheets=sheets, notes=notes)
        chosen = sheet if sheet is not None else sheets[0]
        frame = read_table(data, fname, chosen)
        return _tally_or_table(fname, frame, dataset, mode, sheets, chosen)

    if ext in CSV_EXTS:
        return _tally_or_table(fname, read_table(data, fname), dataset, mode)

    raise ReconError(f"Unsupported file type '{ext}' for '{fname}'. Use .xlsx, .xls, .csv or .json.")


def to_canonical(loaded: LoadedFile, mapping: dict[str, str | None] | None = None) -> pd.DataFrame:
    """Canonical frame for a loaded file, applying *mapping* when it is a plain table."""
    if loaded.canonical:
        frame = loaded.frame.copy()
        if "source_file" not in frame or frame["source_file"].isna().all():
            frame["source_file"] = loaded.name
        return frame
    mapping = mapping if mapping is not None else suggest_mapping(loaded.frame.columns)
    frame = apply_mapping(loaded.frame, mapping, source_file=loaded.name)
    frame["section"] = None
    return frame
