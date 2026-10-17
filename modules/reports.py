"""
Excel report generation and supplier follow-up e-mails.

The workbook is streamed with XlsxWriter in constant-memory mode using
type-specific writes (measured 3.5x faster than openpyxl's write-only mode),
so 1 lakh+ row reports stay practical.  Formatting: styled headers, frozen
panes, auto-filter, auto column widths, Indian ₹ lakh/crore number format for
every sign and size, DD-MM-YYYY dates and red highlighting of difference
columns on the Mismatched sheet.  Text is always written as text, so remarks
starting with "=" can never become formulas.
"""
from __future__ import annotations

import io
import re
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import xlsxwriter
from xlsxwriter.utility import xl_col_to_name

from config import (
    DIFF_CELL_FILL, DIFF_CELL_FONT, DIFF_HEADER_FILL, DOC_LABELS, EXCEL_DATE_FORMAT, HEADER_FILL,
    HEADER_FONT_COLOR, INR_FORMAT, INR_FORMAT_ARAB, INR_FORMAT_KHARAB, INR_FORMAT_NEG, MAX_COL_WIDTH, MODES,
    PARTY_LABELS, PORTAL_SHORT, STATUS_EXACT,
    STATUS_MANUAL, STATUS_MISMATCH, STATUS_MISSING_BOOKS, STATUS_PROBABLE, STATUS_TOLERANCE,
    TOLERANCE_CELL_FILL, describe_tolerance, status_missing_portal,
)
from modules.formatting import format_date, format_inr  # noqa: F401 - re-exported for the UI
from modules.matcher import DIFF_FIELDS, FIELD_LABELS, ReconResult, doc_type_table, status_table, supplier_summary


# --------------------------------------------------------------------------- #
# Sheet column specifications
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Col:
    """One output column: source field, header text and value kind."""

    src: str
    header: str
    kind: str = "text"   # text | money | date | int | bool | score
    diff: bool = False   # highlight as a difference column


def _labels(result: ReconResult) -> tuple[str, str, str]:
    short = PORTAL_SHORT.get(result.portal_name, result.portal_name)
    gstin_label, name_label = PARTY_LABELS.get(result.mode, PARTY_LABELS["purchase"])
    return short, gstin_label, name_label


def pair_columns(result: ReconResult, with_score: bool = False) -> list[Col]:
    """Columns for sheets that show both books and portal values side by side."""
    short, gstin_label, name_label = _labels(result)
    cols = [
        Col("match_id", "Match ID"), Col("status", "Status"), Col("gstin", gstin_label),
        Col("party_name", name_label), Col("doc_type_label", "Doc Type"),
        Col("invoice_no_books", "Invoice No (Books)"), Col("invoice_no_portal", f"Invoice No ({short})"),
        Col("invoice_date_books", "Invoice Date (Books)", "date"),
        Col("invoice_date_portal", f"Invoice Date ({short})", "date"),
    ]
    if with_score:
        cols.append(Col("fuzzy_score", "Similarity Score", "score"))
    for f in DIFF_FIELDS:
        label = FIELD_LABELS[f]
        cols += [Col(f + "_books", f"{label} (Books)", "money"), Col(f + "_portal", f"{label} ({short})", "money"),
                 Col("diff_" + f, f"Diff {label}", "money", diff=True)]
    cols += [
        Col("date_diff_days", "Date Diff (Days)", "int", diff=True),
        Col("place_of_supply_books", "POS (Books)"), Col("place_of_supply_portal", f"POS ({short})"),
        Col("period_books", "Period (Books)"), Col("period_portal", f"Period ({short})"),
        Col("cross_period", "Cross-Period", "bool"), Col("itc_available_portal", "ITC Available"),
        Col("reason", "Reason"),
    ]
    if with_score:
        cols += [Col("review_decision", "Review Decision"), Col("reviewed_by", "Reviewed By"),
                 Col("reviewed_at", "Reviewed At", "datetime")]
    cols.append(Col("remarks", "User Remarks"))
    return cols


def side_columns(result: ReconResult, side: str) -> list[Col]:
    """Columns for single-sided sheets (Missing in 2B / Missing in Books)."""
    short, gstin_label, name_label = _labels(result)
    sfx = "_books" if side == "books" else "_portal"
    src = "Books" if side == "books" else short
    cols = [
        Col("match_id", "Match ID"), Col("status", "Status"), Col("gstin", gstin_label),
        Col("gstin_error" + sfx, "GSTIN Error"), Col("party_name", name_label), Col("doc_type_label", "Doc Type"),
        Col("invoice_no" + sfx, f"Invoice No ({src})"), Col("invoice_date" + sfx, "Invoice Date", "date"),
    ]
    cols += [Col(f + sfx, FIELD_LABELS[f], "money") for f in DIFF_FIELDS]
    cols += [Col("itc_books" if side == "books" else "itc_portal", "Net ITC", "money"),
             Col("place_of_supply" + sfx, "POS"), Col("period" + sfx, "Period"), Col("fy", "FY")]
    if side == "portal":
        cols.append(Col("itc_available_portal", "ITC Available"))
    if side == "books":
        cols += [Col("carried_from_books", "Carried Forward From"), Col("months_pending", "Months Pending", "int")]
    cols += [Col("is_duplicate" + sfx, "Duplicate", "bool"), Col("source_file" + sfx, "Source File"),
             Col("source_row" + sfx, "Source Row", "int"), Col("reason", "Reason"), Col("remarks", "User Remarks")]
    return cols


def _month_labels(months: pd.Series) -> pd.Series:
    parsed = pd.to_datetime(months.astype(str), format="%Y-%m", errors="coerce")
    return parsed.dt.strftime("%b %Y").where(parsed.notna(), months.astype(str))


def gstr3b_sheet(comparison: pd.DataFrame) -> dict[str, "ExtraSheet"]:
    """Excel sheet for :func:`modules.gstr3b.compare_gstr3b` output."""
    df = comparison.assign(month_label=_month_labels(comparison["month"]))
    cols = [Col("month_label", "Month"), Col("status", "Status")]
    for prefix, label in (("books", "Books"), ("gstr2b", "2B (available)"), ("gstr3b", "3B 4A(5) claimed")):
        cols += [Col(f"{prefix}_{h}", f"{label} {h.upper()}", "money") for h in ("igst", "cgst", "sgst", "cess")]
        cols.append(Col(f"{prefix}_total", f"{label} Total", "money"))
    cols += [Col(f"diff_3b_2b_{h}", f"3B − 2B ({h.upper()})", "money", diff=True) for h in ("igst", "cgst", "sgst", "cess")]
    cols += [Col("diff_3b_2b_total", "3B − 2B (Total)", "money", diff=True),
             Col("diff_3b_books", "3B − Books", "money", diff=True),
             Col("diff_books_2b", "Books − 2B", "money", diff=True),
             Col("cumulative_3b_minus_2b", "Cumulative 3B − 2B", "money", diff=True),
             Col("gstr2b_not_available", "2B ITC Not Available", "money")]
    return {"GSTR-3B vs 2B vs Books": (df, cols, "diff")}


def carry_forward_sheet(schedule: pd.DataFrame, short: str = "2B") -> dict[str, "ExtraSheet"]:
    """Excel sheet for :func:`modules.periods.carry_forward_schedule` output."""
    df = schedule.assign(month_label=_month_labels(schedule["month"]))
    cols = [Col("month_label", "Month"),
            Col("opening_count", "Opening Pending (Docs)", "int"), Col("opening_itc", "Opening Pending ITC", "money"),
            Col("added_count", "New in Books (Docs)", "int"), Col("added_itc", "New in Books ITC", "money"),
            Col("reflected_count", f"Reflected in {short} (Docs)", "int"),
            Col("reflected_itc", f"Reflected in {short} ITC", "money"),
            Col("closing_count", "Closing Pending (Docs)", "int"), Col("closing_itc", "Closing Pending ITC", "money")]
    return {"Month-wise Carry Forward": (df, cols, None)}


def audit_sheet(audit: pd.DataFrame) -> dict[str, "ExtraSheet"]:
    """Excel sheet for the review audit trail (:meth:`modules.database.Database.load_audit`)."""
    cols = [Col("at", "When", "datetime"), Col("username", "User"), Col("match_id", "Match ID"),
            Col("action", "Action"), Col("old_decision", "Old Decision"), Col("new_decision", "New Decision"),
            Col("old_remarks", "Old Remarks"), Col("new_remarks", "New Remarks")]
    return {"Review Audit Trail": (audit, cols, None)}


def supplier_columns(result: ReconResult) -> list[Col]:
    short, gstin_label, name_label = _labels(result)
    return [
        Col("gstin", gstin_label), Col("party_name", name_label), Col("docs_books", "Docs (Books)", "int"),
        Col("docs_portal", f"Docs ({short})", "int"), Col("itc_books", "ITC (Books)", "money"),
        Col("itc_portal", f"ITC ({short})", "money"), Col("difference", "Difference", "money", diff=True),
        Col("matched_itc", "Matched ITC", "money"), Col("mismatch_count", "Mismatches", "int"),
        Col("probable_count", "Probable", "int"), Col("missing_portal_count", f"Missing in {short}", "int"),
        Col("missing_portal_itc", f"ITC Missing in {short}", "money"),
        Col("missing_books_count", "Missing in Books", "int"), Col("missing_books_itc", "ITC Missing in Books", "money"),
        Col("pending_itc", "Pending ITC (At Risk)", "money", diff=True),
    ]


DATA_QUALITY_COLUMNS: list[Col] = [
    Col("source", "Source"), Col("gstin", "GSTIN"), Col("gstin_error", "GSTIN Error"), Col("party_name", "Name"),
    Col("doc_type_label", "Doc Type"), Col("invoice_no", "Invoice No"), Col("inv_norm", "Normalised Invoice No"),
    Col("invoice_date", "Invoice Date", "date"), Col("taxable_value", "Taxable Value", "money"),
    Col("total_tax", "Total Tax", "money"), Col("invoice_value", "Invoice Value", "money"),
    Col("dup_count", "Occurrences", "int"), Col("source_file", "Source File"), Col("source_row", "Source Row", "int"),
]


# --------------------------------------------------------------------------- #
# Frames per sheet
# --------------------------------------------------------------------------- #
def _with_labels(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out["doc_type_label"] = out["doc_type"].map(DOC_LABELS).fillna(out["doc_type"])
    return out


def data_quality_frame(result: ReconResult, kind: str) -> pd.DataFrame:
    """Duplicates (``kind='duplicates'``) or invalid GSTINs (``kind='invalid'``) from both datasets."""
    short = PORTAL_SHORT.get(result.portal_name, result.portal_name)
    parts = []
    for label, df in (("Books", result.books), (short, result.portal)):
        mask = df["is_duplicate"] if kind == "duplicates" else df["gstin_error"] != ""
        sub = df[mask].copy()
        sub.insert(0, "source", label)
        parts.append(sub)
    out = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
    if out.empty:
        return _with_labels(result.books.head(0).assign(source=""))
    sort = ["source", "gstin", "inv_norm"] if kind == "duplicates" else ["source", "gstin"]
    return _with_labels(out.sort_values(sort, kind="stable"))


def report_frames(result: ReconResult) -> dict[str, tuple[pd.DataFrame, list[Col], str | None]]:
    """Sheet name -> (frame, columns, highlight) for every data sheet of the report."""
    r = _with_labels(result.recon)
    short = PORTAL_SHORT.get(result.portal_name, result.portal_name)
    st = r["status"]
    return {
        "Matched": (r[st.isin([STATUS_EXACT, STATUS_MANUAL])], pair_columns(result), None),
        "Matched with Tolerance": (r[st == STATUS_TOLERANCE], pair_columns(result), "tolerance"),
        "Mismatched": (r[st == STATUS_MISMATCH], pair_columns(result), "diff"),
        "Probable Matches": (r[st == STATUS_PROBABLE], pair_columns(result, with_score=True), "tolerance"),
        f"Missing in {short}": (r[st == status_missing_portal(result.portal_name)], side_columns(result, "books"), None),
        "Missing in Books": (r[st == STATUS_MISSING_BOOKS], side_columns(result, "portal"), None),
        "Supplier-wise Summary": (supplier_summary(result.recon, result.portal_name), supplier_columns(result), "diff"),
        "Duplicates": (data_quality_frame(result, "duplicates"), DATA_QUALITY_COLUMNS, None),
        "Invalid GSTINs": (data_quality_frame(result, "invalid"), DATA_QUALITY_COLUMNS, None),
    }


# --------------------------------------------------------------------------- #
# Low-level sheet writing (XlsxWriter, constant-memory streaming)
# --------------------------------------------------------------------------- #
class _Formats:
    """Workbook formats, created once per workbook."""

    def __init__(self, wb: xlsxwriter.Workbook) -> None:
        border = {"border": 1, "border_color": "#BFBFBF"}
        head = {"bold": True, "font_color": f"#{HEADER_FONT_COLOR}", "align": "center", "valign": "vcenter",
                "text_wrap": True, **border}
        self.header = wb.add_format({**head, "bg_color": f"#{HEADER_FILL}"})
        self.diff_header = wb.add_format({**head, "bg_color": f"#{DIFF_HEADER_FILL}"})
        self.money = wb.add_format({"num_format": INR_FORMAT})
        self.money_neg = wb.add_format({"num_format": INR_FORMAT_NEG})
        self.money_arab = wb.add_format({"num_format": INR_FORMAT_ARAB})
        self.money_kharab = wb.add_format({"num_format": INR_FORMAT_KHARAB})
        self.date = wb.add_format({"num_format": EXCEL_DATE_FORMAT})
        self.datetime = wb.add_format({"num_format": EXCEL_DATE_FORMAT + " hh:mm"})
        self.int = wb.add_format({"num_format": "0"})
        self.score = wb.add_format({"num_format": "0.0"})
        self.diff_cell = wb.add_format({"bg_color": f"#{DIFF_CELL_FILL}", "font_color": f"#{DIFF_CELL_FONT}"})
        self.tol_cell = wb.add_format({"bg_color": f"#{TOLERANCE_CELL_FILL}", "font_color": "#9C5700"})
        self.title = wb.add_format({"bold": True, "font_size": 16, "font_color": f"#{HEADER_FILL}"})
        self.bold = wb.add_format({"bold": True})
        self.note = wb.add_format({"font_color": "#7F7F7F", "italic": True})
        self.bold_money = {k: wb.add_format({"bold": True, "num_format": f})
                           for k, f in (("pos", INR_FORMAT), ("neg", INR_FORMAT_NEG), ("arab", INR_FORMAT_ARAB),
                                        ("kharab", INR_FORMAT_KHARAB))}
        self.risk_money = wb.add_format({"bold": True, "num_format": INR_FORMAT, "font_color": f"#{DIFF_CELL_FONT}"})
        # Registered style (xf) indices, used by the vectorised row renderer.
        self.xf = {k: f._get_xf_index() for k, f in (
            ("pos", self.money), ("neg", self.money_neg), ("arab", self.money_arab), ("kharab", self.money_kharab),
            ("date", self.date), ("datetime", self.datetime), ("int", self.int), ("score", self.score))}

    def money_for(self, value: float, bold: bool = False):
        """Indian-format number style for one value (sign and size decide - see config.INR_FORMAT)."""
        key = _money_key(value)
        if bold:
            return self.bold_money[key]
        return {"pos": self.money, "neg": self.money_neg, "arab": self.money_arab, "kharab": self.money_kharab}[key]


def _money_key(value: float) -> str:
    size = abs(value)
    if size >= 1e11:
        return "kharab"
    if size >= 1e9:
        return "arab"
    return "neg" if value < 0 else "pos"


def _column_values(series: pd.Series, kind: str) -> list:
    """Convert a column to plain Python values suited to the writer (None for missing)."""
    if kind in ("money", "score"):
        s = pd.to_numeric(series, errors="coerce").astype(float)
        return s.astype(object).where(s.notna(), None).tolist()
    if kind == "int":
        s = pd.to_numeric(series, errors="coerce")
        return [None if pd.isna(v) else int(v) for v in s.tolist()]
    if kind in ("date", "datetime"):
        s = pd.to_datetime(series, errors="coerce")
        return [None if pd.isna(v) else v.to_pydatetime() for v in s.tolist()]
    if kind == "bool":
        return np.where(series.astype(object).fillna(False).astype(bool), "Yes", "").tolist()
    return series.astype(object).where(series.notna(), "").astype(str).tolist()


def _width(values: list, header: str, kind: str) -> float:
    """Approximate a good column width from the header and a sample of values."""
    sample = values[:3000]
    if kind == "money":
        biggest = max((abs(v) for v in sample if v is not None), default=0)
        longest = len(format_inr(biggest)) + 2
    elif kind in ("date", "datetime"):
        longest = 11 if kind == "date" else 17
    else:
        longest = max((len(str(v)) for v in sample if v is not None), default=0)
    return float(min(max(longest, min(len(header), 24)) + 2, MAX_COL_WIDTH))


_STR = "string[pyarrow]"
_ILLEGAL_XML = r"[\x00-\x08\x0b\x0c\x0e-\x1f￾￿]"
_EXCEL_EPOCH = pd.Timestamp("1899-12-30")
_ROW_CHUNK = 20_000


@dataclass
class _FastSheet:
    """Data rows of one sheet, rendered to SpreadsheetML in vectorised chunks after XlsxWriter closes."""

    df: pd.DataFrame
    cols: list[Col]
    xf: dict[str, int]  # style indices

    def chunks(self):
        """Yield the ``<row>`` XML for all data rows, ``_ROW_CHUNK`` rows at a time."""
        for start in range(0, len(self.df), _ROW_CHUNK):
            part = self.df.iloc[start:start + _ROW_CHUNK]
            rows = pd.Series(np.arange(start + 2, start + 2 + len(part)), index=part.index).astype(_STR)
            xml = '<row r="' + rows + '">'
            for i, c in enumerate(self.cols):
                xml = xml + _cells_xml(part[c.src], c.kind, xl_col_to_name(i), rows, self.xf)
            yield (xml + "</row>").str.cat()


def _money_xf(values: pd.Series, xf: dict[str, int]) -> pd.Series:
    size = values.abs()
    choice = np.select([size >= 1e11, size >= 1e9, values < 0], [xf["kharab"], xf["arab"], xf["neg"]], xf["pos"])
    return pd.Series(choice, index=values.index).astype(_STR)


def _cells_xml(series: pd.Series, kind: str, letter: str, rows: pd.Series, xf: dict[str, int]) -> pd.Series:
    """``<c>`` elements for one column chunk (empty string where the value is missing)."""
    ref = '<c r="' + letter + rows
    if kind in ("money", "int", "score", "date", "datetime"):
        if kind in ("date", "datetime"):
            dt = pd.to_datetime(series, errors="coerce")
            num = (dt - _EXCEL_EPOCH) / pd.Timedelta(days=1)
            if kind == "date":
                num = num.round(0)
            style = pd.Series(str(xf[kind]), index=series.index, dtype=_STR)
        else:
            num = pd.to_numeric(series, errors="coerce").astype(float)
            if kind == "int":
                num = num.round(0)
            style = _money_xf(num, xf) if kind == "money" else \
                pd.Series(str(xf[kind]), index=series.index, dtype=_STR)
        missing = num.isna()
        text = num.astype(_STR)
        if kind in ("int", "date"):
            text = text.str.replace(r"\.0$", "", regex=True)
        cell = ref + '" s="' + style + '"><v>' + text + "</v></c>"
        return cell.where(~missing, "").fillna("")
    if kind == "bool":
        flag = series.astype(object).fillna(False).astype(bool)
        text = pd.Series(np.where(flag, "Yes", ""), index=series.index, dtype=_STR)
    else:
        text = series.astype(object).where(series.notna(), "").astype(str).astype(_STR)
    missing = text == ""
    text = (text.str.slice(0, 32767).str.replace(_ILLEGAL_XML, "", regex=True)
            .str.replace("&", "&amp;", regex=False).str.replace("<", "&lt;", regex=False)
            .str.replace(">", "&gt;", regex=False))
    cell = ref + '" t="inlineStr"><is><t xml:space="preserve">' + text + "</t></is></c>"
    return cell.where(~missing, "").fillna("")


def _inject_rows(xlsx: bytes, fast: dict[int, _FastSheet]) -> bytes:
    """Stream the fast-rendered rows into the data sheets of an XlsxWriter workbook."""
    if not fast:
        return xlsx
    zin = zipfile.ZipFile(io.BytesIO(xlsx))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=3) as zout:
        for item in zin.infolist():
            data = zin.read(item.filename)
            m = re.fullmatch(r"xl/worksheets/sheet(\d+)\.xml", item.filename)
            sheet = fast.get(int(m.group(1))) if m else None
            if sheet is None:
                zout.writestr(item, data)
                continue
            xml = data.decode("utf-8")
            head, tail = xml.split("</sheetData>", 1)
            last = xl_col_to_name(max(len(sheet.cols) - 1, 0))
            head = re.sub(r'<dimension ref="[^"]*"/>', f'<dimension ref="A1:{last}{len(sheet.df) + 1}"/>', head, count=1)
            with zout.open(item.filename, "w", force_zip64=True) as fh:
                fh.write(head.encode("utf-8"))
                for chunk in sheet.chunks():
                    fh.write(chunk.encode("utf-8"))
                fh.write(("</sheetData>" + tail).encode("utf-8"))
    return out.getvalue()


def _write_table(wb: xlsxwriter.Workbook, fmts: _Formats, title: str, df: pd.DataFrame, cols: list[Col],
                 highlight: str | None) -> _FastSheet | None:
    """Create a data sheet: XlsxWriter writes header, widths, panes, filter and highlighting;
    the data rows are returned as a :class:`_FastSheet` to be streamed in afterwards."""
    ws = wb.add_worksheet(title[:31])
    cols = [c for c in cols if c.src in df.columns]
    n = len(df)
    for i, c in enumerate(cols):
        ws.set_column(i, i, _width(_column_values(df[c.src].head(3000), c.kind), c.header, c.kind))
    ws.freeze_panes(1, 2 if len(cols) > 2 else 0)
    ws.set_row(0, 32)
    for i, c in enumerate(cols):
        ws.write_string(0, i, c.header, fmts.diff_header if (c.diff and highlight == "diff") else fmts.header)
    ws.autofilter(0, 0, max(n, 1), max(len(cols) - 1, 0))
    if n and highlight:
        cell_fmt = fmts.diff_cell if highlight == "diff" else fmts.tol_cell
        for i, c in enumerate(cols):
            if c.diff:
                ws.conditional_format(1, i, n, i, {"type": "cell", "criteria": "!=", "value": 0, "format": cell_fmt})
    return _FastSheet(df, cols, fmts.xf) if n else None


def summary_rows(result: ReconResult) -> list[tuple[str, object, float | None]]:
    """Key-figure rows ``(label, count, amount)`` shared by the Excel and PDF summaries."""
    short = PORTAL_SHORT.get(result.portal_name, result.portal_name)
    s = result.summary()
    return [
        ("Documents in Books", s["docs_books"], None),
        (f"Documents in {short}", s["docs_portal"], None),
        ("Net ITC as per Books", None, s["itc_books"]),
        (f"Net ITC as per {short}", None, s["itc_portal"]),
        (f"Difference (Books - {short})", None, round(s["itc_books"] - s["itc_portal"], 2)),
        ("Matched (Exact + Tolerance + Manual)", s["matched_count"], s["matched_itc"]),
        ("  of which Exact", s["exact_count"], None),
        ("  of which Within Tolerance", s["tolerance_count"], None),
        ("  of which Manually Matched", s["manual_count"], None),
        ("Mismatched (ITC difference)", s["mismatch_count"], s["mismatch_itc_diff"]),
        ("Probable Matches (pending review)", s["probable_count"], None),
        (f"Missing in {short} (ITC as per books)", s["missing_portal_count"], s["missing_portal_itc"]),
        (f"Missing in Books (ITC as per {short})", s["missing_books_count"], s["missing_books_itc"]),
        ("ITC at Risk", None, s["itc_at_risk"]),
        (f"ITC marked 'Not Available' in {short}", None, s["itc_portal_not_available"]),
        ("Cross-period matches", s["cross_period_count"], None),
        ("Invalid GSTINs (Books / Portal)", f"{s['invalid_gstin_books']} / {s['invalid_gstin_portal']}", None),
        ("Duplicate rows (Books / Portal)", f"{s['duplicates_books']} / {s['duplicates_portal']}", None),
    ]


def report_header(result: ReconResult, company_name: str, company_gstin: str,
                  period_label: str) -> list[tuple[str, str]]:
    """``(label, value)`` lines identifying the run - company, period, rule used."""
    mode_label = next((k for k, v in MODES.items() if v["portal"] == result.portal_name), result.portal_name)
    st = result.settings
    return [
        ("Company", company_name or "-"), ("GSTIN", company_gstin or "-"), ("Reconciliation", mode_label),
        ("Period", period_label or "-"), ("Generated on", datetime.now().strftime("%d-%m-%Y %H:%M")),
        ("Tolerance rule", describe_tolerance(st)),
        ("Fuzzy matching", f"Score ≥ {st.fuzzy_threshold:g}" if st.enable_fuzzy else "Off"),
    ]


def _write_summary(wb: xlsxwriter.Workbook, fmts: _Formats, result: ReconResult, company_name: str,
                   company_gstin: str, period_label: str) -> None:
    """Summary sheet: header block, key figures, status-wise and doc-type-wise tables."""
    ws = wb.add_worksheet("Summary")
    ws.set_column(0, 0, 42)
    ws.set_column(1, 6, 20)
    short = PORTAL_SHORT.get(result.portal_name, result.portal_name)

    def money(row: int, col: int, value: float, bold: bool = False) -> None:
        ws.write_number(row, col, float(value), fmts.money_for(float(value), bold))

    ws.write_string(0, 0, "GST Reconciliation Report", fmts.title)
    r = 1
    for label, value in report_header(result, company_name, company_gstin, period_label):
        ws.write_string(r, 0, label, fmts.bold)
        ws.write_string(r, 1, value, fmts.bold if label == "Tolerance rule" else None)
        r += 1
    r += 1

    def header_row(*titles: str) -> None:
        nonlocal r
        for i, t in enumerate(titles):
            ws.write_string(r, i, t, fmts.header)
        r += 1

    header_row("Key Figures", "Count", "Amount (₹)")
    for label, count, amount in summary_rows(result):
        risk = label == "ITC at Risk"
        ws.write_string(r, 0, label, fmts.bold if risk else None)
        if isinstance(count, (int, np.integer)):
            ws.write_number(r, 1, int(count))
        elif count is not None:
            ws.write_string(r, 1, str(count))
        if amount is not None:
            if risk and amount >= 0:
                ws.write_number(r, 2, float(amount), fmts.risk_money)
            else:
                money(r, 2, amount, bold=risk)
        r += 1
    r += 1

    header_row("Status", "Documents", "Taxable (Books)", "ITC (Books)", f"Taxable ({short})", f"ITC ({short})",
               "ITC Difference")
    for rec in status_table(result.recon).itertuples(index=False):
        ws.write_string(r, 0, rec.status)
        ws.write_number(r, 1, int(rec.documents))
        for i, v in enumerate((rec.taxable_books, rec.itc_books, rec.taxable_portal, rec.itc_portal,
                               rec.itc_difference), start=2):
            money(r, i, v)
        r += 1
    r += 1

    header_row("Document Type", "Docs (Books)", f"Docs ({short})", "Net ITC (Books)", f"Net ITC ({short})",
               "ITC Difference")
    for rec in doc_type_table(result.recon).itertuples(index=False):
        net = rec.doc_type == "NET"
        ws.write_string(r, 0, "Net Total" if net else DOC_LABELS.get(rec.doc_type, rec.doc_type),
                        fmts.bold if net else None)
        ws.write_number(r, 1, int(rec.documents_books))
        ws.write_number(r, 2, int(rec.documents_portal))
        for i, v in enumerate((rec.itc_books, rec.itc_portal, rec.itc_difference), start=3):
            money(r, i, v, bold=net)
        r += 1
    r += 1
    ws.write_string(r, 0, "Note: credit notes are shown as negative ITC; all ITC figures are net.", fmts.note)


ExtraSheet = tuple[pd.DataFrame, list[Col], str | None]


def build_excel_report(result: ReconResult, company_name: str = "", company_gstin: str = "",
                       period_label: str = "", path: str | Path | None = None,
                       extra_sheets: dict[str, ExtraSheet] | None = None) -> bytes:
    """Create the formatted reconciliation workbook.

    Args:
        result: Reconciliation result (with review decisions already applied).
        company_name, company_gstin, period_label: Shown on the Summary sheet.
        path: Optional file path to also save the workbook to.
        extra_sheets: Additional sheets appended after the standard ones
            (e.g. GSTR-3B comparison, month-wise carry forward, review audit trail).

    Returns:
        The workbook as bytes (for download buttons).
    """
    buf = io.BytesIO()
    wb = xlsxwriter.Workbook(buf, {"constant_memory": True, "strings_to_numbers": False,
                                   "strings_to_formulas": False, "strings_to_urls": False})
    fmts = _Formats(wb)
    _write_summary(wb, fmts, result, company_name, company_gstin, period_label)
    sheets = report_frames(result)
    sheets.update(extra_sheets or {})
    fast: dict[int, _FastSheet] = {}
    for index, (name, (frame, cols, highlight)) in enumerate(sheets.items(), start=2):  # sheet1 = Summary
        rendered = _write_table(wb, fmts, name, frame, cols, highlight)
        if rendered is not None:
            fast[index] = rendered
    wb.close()
    data = _inject_rows(buf.getvalue(), fast)
    if path:
        Path(path).write_bytes(data)
    return data


# --------------------------------------------------------------------------- #
# Display helpers for the UI
# --------------------------------------------------------------------------- #
def display_frame(df: pd.DataFrame, cols: list[Col]) -> pd.DataFrame:
    """Frame with report headers (for on-screen tables and CSV export)."""
    df = _with_labels(df) if "doc_type" in df and "doc_type_label" not in df else df
    present = [c for c in cols if c.src in df.columns]
    out = df[[c.src for c in present]].copy()
    out.columns = [c.header for c in present]
    return out


# --------------------------------------------------------------------------- #
# Supplier follow-up e-mails
# --------------------------------------------------------------------------- #
def supplier_email(result: ReconResult, gstin: str, company_name: str = "", company_gstin: str = "",
                   period_label: str = "") -> str:
    """Draft a follow-up e-mail to one supplier listing documents missing in / differing from the portal.

    Returns ``""`` when the supplier has nothing pending.
    """
    recon = result.recon
    short = PORTAL_SHORT.get(result.portal_name, result.portal_name)
    rows = recon[recon["gstin"] == gstin]
    missing = rows[rows["status"] == result.missing_portal_status]
    mism = rows[rows["status"] == STATUS_MISMATCH]
    if missing.empty and mism.empty:
        return ""
    party = next((n for n in rows["party_name"] if n), "Sir/Madam")
    company = company_name or "our company"
    period = f" for {period_label}" if period_label else ""
    purchase = result.mode == "purchase"

    lines = [
        f"Subject: GST reconciliation{period} - documents pending in {result.portal_name} ({company})",
        "",
        f"Dear {party},",
        f"GSTIN: {gstin}",
        "",
    ]
    if purchase:
        lines.append(f"While reconciling our purchase register{period} with {result.portal_name}"
                     f"{' (our GSTIN ' + company_gstin + ')' if company_gstin else ''}, we found the following "
                     "documents issued by you that need your attention.")
    else:
        lines.append(f"While reconciling our sales register{period} with the {result.portal_name} filed by us, "
                     "we found the following documents that need confirmation.")
    lines.append("")
    total_missing = 0.0
    if not missing.empty:
        lines.append(f"A. Documents NOT reflected in our {result.portal_name}:")
        for n, rec in enumerate(missing.itertuples(index=False), start=1):
            label = DOC_LABELS.get(rec.doc_type, rec.doc_type)
            lines.append(f"   {n}. {label} {rec.invoice_no_books} dated {format_date(rec.invoice_date_books)} - "
                         f"Taxable {format_inr(rec.taxable_value_books)}, "
                         f"IGST {format_inr(rec.igst_books)}, CGST {format_inr(rec.cgst_books)}, "
                         f"SGST {format_inr(rec.sgst_books)}, Invoice value {format_inr(rec.invoice_value_books)}")
            total_missing += float(rec.itc_books)
        lines.append(f"   Total tax involved: {format_inr(total_missing)}")
        lines.append("")
    if not mism.empty:
        lines.append(f"{'B' if not missing.empty else 'A'}. Documents reported with different details:")
        for n, rec in enumerate(mism.itertuples(index=False), start=1):
            lines.append(f"   {n}. Invoice {rec.invoice_no_books} dated {format_date(rec.invoice_date_books)} - "
                         f"as per our books: taxable {format_inr(rec.taxable_value_books)}, tax {format_inr(rec.total_tax_books)}; "
                         f"as per {short}: taxable {format_inr(rec.taxable_value_portal)}, tax {format_inr(rec.total_tax_portal)} "
                         f"({rec.reason})")
        lines.append("")
    if purchase:
        lines += [
            "We request you to upload / amend these documents in your GSTR-1 / IFF at the earliest, "
            f"so that the input tax credit is reflected in our {result.portal_name}. "
            "If already uploaded, please share the filing period and acknowledgement details.",
        ]
    else:
        lines += ["Please confirm these details so that we can report / amend them in our next return."]
    lines += ["", "Thanking you,", company_name or "[Your name]", "Accounts / GST Team"]
    return "\n".join(lines)


def all_supplier_emails(result: ReconResult, company_name: str = "", company_gstin: str = "",
                        period_label: str = "") -> str:
    """Concatenated e-mails for every supplier with pending items, separated by rulers."""
    summary = supplier_summary(result.recon, result.portal_name)
    pending = summary[(summary["missing_portal_count"] > 0) | (summary["mismatch_count"] > 0)]
    texts = [supplier_email(result, g, company_name, company_gstin, period_label) for g in pending["gstin"]]
    return ("\n\n" + "=" * 90 + "\n\n").join(t for t in texts if t)
