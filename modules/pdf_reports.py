"""
PDF export of the Summary and Supplier-wise reports (reportlab).

* **Summary** (A4 portrait): run header (company, period, tolerance rule), key
  figures, status-wise and document-type-wise tables - the same content as the
  Excel Summary sheet.
* **Supplier-wise** (A4 landscape): one row per supplier with ITC as per books and
  portal, matched, mismatched, missing and pending ITC, plus a totals row.  The
  header row repeats on every page; pages are numbered "Page x of y".

Amounts use Indian grouping with the ₹ sign.  The standard PDF fonts have no ₹
glyph, so a TrueType font that contains it is embedded (Arial / Segoe UI / Calibri
on Windows, DejaVu Sans on Linux, Arial Unicode on macOS).  If none is found the
symbol is written as "Rs." instead of an empty box.
"""
from __future__ import annotations

import io
from functools import lru_cache
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import LongTable, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
from reportlab.pdfgen import canvas as rl_canvas

from config import DOC_LABELS, HEADER_FILL, PORTAL_SHORT
from modules.formatting import format_inr
from modules.matcher import ReconResult, doc_type_table, status_table, supplier_summary
from modules.reports import report_header, summary_rows

_FONT_CANDIDATES = [
    ("C:/Windows/Fonts/arial.ttf", "C:/Windows/Fonts/arialbd.ttf"),
    ("C:/Windows/Fonts/segoeui.ttf", "C:/Windows/Fonts/segoeuib.ttf"),
    ("C:/Windows/Fonts/calibri.ttf", "C:/Windows/Fonts/calibrib.ttf"),
    ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
    ("/usr/share/fonts/dejavu/DejaVuSans.ttf", "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf"),
    ("/Library/Fonts/Arial Unicode.ttf", "/Library/Fonts/Arial Unicode.ttf"),
    ("/System/Library/Fonts/Supplemental/Arial.ttf", "/System/Library/Fonts/Supplemental/Arial Bold.ttf"),
]
_HEADER = colors.HexColor(f"#{HEADER_FILL}")
_GRID = colors.HexColor("#BFBFBF")
_ZEBRA = colors.HexColor("#F3F6FA")
_RED = colors.HexColor("#9C0006")


@lru_cache(maxsize=1)
def pdf_fonts() -> tuple[str, str, bool]:
    """``(regular, bold, has_rupee)`` - registers the first TrueType pair that has the ₹ glyph."""
    for regular, bold in _FONT_CANDIDATES:
        if not (Path(regular).exists() and Path(bold).exists()):
            continue
        try:
            reg, bld = TTFont("GSTSans", regular), TTFont("GSTSans-Bold", bold)
        except Exception:  # noqa: BLE001 - unreadable font file, try the next one
            continue
        if 0x20B9 in reg.face.charToGlyph and 0x20B9 in bld.face.charToGlyph:
            pdfmetrics.registerFont(reg)
            pdfmetrics.registerFont(bld)
            return "GSTSans", "GSTSans-Bold", True
    return "Helvetica", "Helvetica-Bold", False


def _inr(value: float | None, decimals: int = 2) -> str:
    text = format_inr(value, decimals=decimals)
    return text if pdf_fonts()[2] else text.replace("₹", "Rs.")


def _styles() -> dict[str, ParagraphStyle]:
    regular, bold, _ = pdf_fonts()
    return {
        "title": ParagraphStyle("title", fontName=bold, fontSize=16, leading=20, textColor=_HEADER, spaceAfter=4),
        "h2": ParagraphStyle("h2", fontName=bold, fontSize=11, leading=14, textColor=_HEADER, spaceBefore=8,
                             spaceAfter=4),
        "cell": ParagraphStyle("cell", fontName=regular, fontSize=8, leading=10),
        "cell_bold": ParagraphStyle("cell_bold", fontName=bold, fontSize=8, leading=10),
        "num": ParagraphStyle("num", fontName=regular, fontSize=8, leading=10, alignment=TA_RIGHT),
        "note": ParagraphStyle("note", fontName=regular, fontSize=7.5, leading=9, textColor=colors.grey),
    }


class _NumberedCanvas(rl_canvas.Canvas):
    """Canvas that writes 'Page x of y' and a footer once the page count is known."""

    footer = ""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._pages: list[dict] = []

    def showPage(self):  # noqa: N802 - reportlab API
        self._pages.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        total = len(self._pages)
        for state in self._pages:
            self.__dict__.update(state)
            self.setFont(pdf_fonts()[0], 7)
            self.setFillColor(colors.grey)
            width = self._pagesize[0]
            self.drawString(15 * mm, 8 * mm, self.footer)
            self.drawRightString(width - 15 * mm, 8 * mm, f"Page {self._pageNumber} of {total}")
            super().showPage()
        super().save()


def _table(data: list[list], widths: list[float], numeric_from: int = 1, bold_last: bool = False,
           repeat: bool = True) -> Table:
    regular, bold, _ = pdf_fonts()
    cls = LongTable if repeat else Table
    t = cls(data, colWidths=widths, repeatRows=1 if repeat else 0)
    style = [
        ("FONT", (0, 0), (-1, 0), bold, 8), ("FONT", (0, 1), (-1, -1), regular, 8),
        ("BACKGROUND", (0, 0), (-1, 0), _HEADER), ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("GRID", (0, 0), (-1, -1), 0.25, _GRID), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ALIGN", (numeric_from, 1), (-1, -1), "RIGHT"), ("ALIGN", (0, 0), (-1, 0), "CENTER"),
        ("TOPPADDING", (0, 0), (-1, -1), 2), ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
    ]
    for r in range(2, len(data), 2):
        style.append(("BACKGROUND", (0, r), (-1, r), _ZEBRA))
    if bold_last and len(data) > 1:
        style += [("FONT", (0, -1), (-1, -1), bold, 8), ("LINEABOVE", (0, -1), (-1, -1), 0.8, _HEADER)]
    t.setStyle(TableStyle(style))
    return t


def _header_block(result: ReconResult, company_name: str, company_gstin: str, period_label: str,
                  title: str, width: float) -> list:
    st = _styles()
    rows = [[Paragraph(f"<b>{k}</b>", st["cell"]), Paragraph(v, st["cell"])]
            for k, v in report_header(result, company_name, company_gstin, period_label)]
    t = Table(rows, colWidths=[35 * mm, width - 35 * mm])
    t.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("TOPPADDING", (0, 0), (-1, -1), 1),
                           ("BOTTOMPADDING", (0, 0), (-1, -1), 1)]))
    return [Paragraph(title, st["title"]), t, Spacer(1, 4 * mm)]


def _build(story: list, pagesize, footer: str) -> bytes:
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=pagesize, leftMargin=15 * mm, rightMargin=15 * mm, topMargin=14 * mm,
                            bottomMargin=16 * mm, title=footer, author="GST Reconciliation")
    canvas_cls = type("FooterCanvas", (_NumberedCanvas,), {"footer": footer})
    doc.build(story, canvasmaker=canvas_cls)
    return buf.getvalue()


def summary_pdf(result: ReconResult, company_name: str = "", company_gstin: str = "",
                period_label: str = "") -> bytes:
    """Summary report as PDF bytes (A4 portrait)."""
    st = _styles()
    short = PORTAL_SHORT.get(result.portal_name, result.portal_name)
    width = A4[0] - 30 * mm
    story = _header_block(result, company_name, company_gstin, period_label, "GST Reconciliation - Summary", width)

    story.append(Paragraph("Key figures", st["h2"]))
    data = [["Item", "Count", "Amount"]]
    indent = ParagraphStyle("indent", parent=st["cell"], leftIndent=10)
    for label, count, amount in summary_rows(result):
        style = st["cell_bold"] if label == "ITC at Risk" else indent if label.startswith("  ") else st["cell"]
        data.append([Paragraph(label.strip(), style),
                     "" if count is None else (f"{count:,}" if isinstance(count, int) else str(count)),
                     "" if amount is None else _inr(amount)])
    t = _table(data, [width * 0.58, width * 0.14, width * 0.28], repeat=False)
    risk_row = next(i for i, r in enumerate(data) if isinstance(r[0], Paragraph) and "ITC at Risk" in r[0].text)
    t.setStyle(TableStyle([("TEXTCOLOR", (2, risk_row), (2, risk_row), _RED),
                           ("FONT", (2, risk_row), (2, risk_row), pdf_fonts()[1], 8)]))
    story.append(t)

    story.append(Paragraph("Status-wise", st["h2"]))
    data = [["Status", "Docs", "Taxable (Books)", "ITC (Books)", f"Taxable ({short})", f"ITC ({short})", "ITC Diff"]]
    for r in status_table(result.recon).itertuples(index=False):
        data.append([r.status, f"{int(r.documents):,}", _inr(r.taxable_books), _inr(r.itc_books),
                     _inr(r.taxable_portal), _inr(r.itc_portal), _inr(r.itc_difference)])
    story.append(_table(data, [width * w for w in (0.22, 0.07, 0.15, 0.14, 0.15, 0.14, 0.13)], repeat=False))

    story.append(Paragraph("Document type-wise (net)", st["h2"]))
    data = [["Document type", "Docs (Books)", f"Docs ({short})", "Net ITC (Books)", f"Net ITC ({short})", "ITC Diff"]]
    for r in doc_type_table(result.recon).itertuples(index=False):
        data.append(["Net Total" if r.doc_type == "NET" else DOC_LABELS.get(r.doc_type, r.doc_type),
                     f"{int(r.documents_books):,}", f"{int(r.documents_portal):,}", _inr(r.itc_books),
                     _inr(r.itc_portal), _inr(r.itc_difference)])
    story.append(_table(data, [width * w for w in (0.24, 0.12, 0.12, 0.18, 0.18, 0.16)], bold_last=True, repeat=False))
    story.append(Spacer(1, 3 * mm))
    story.append(Paragraph("Credit notes are shown as negative ITC; all ITC figures are net.", st["note"]))
    return _build(story, A4, f"GST Reconciliation Summary - {company_name or ''} {period_label or ''}".strip())


def supplier_pdf(result: ReconResult, company_name: str = "", company_gstin: str = "", period_label: str = "",
                 only_pending: bool = False) -> bytes:
    """Supplier-wise report as PDF bytes (A4 landscape)."""
    st = _styles()
    short = PORTAL_SHORT.get(result.portal_name, result.portal_name)
    party = "Supplier" if result.mode == "purchase" else "Customer"
    page = landscape(A4)
    width = page[0] - 30 * mm
    sup = supplier_summary(result.recon, result.portal_name)
    if only_pending:
        sup = sup[(sup["pending_itc"] > 0) | (sup["missing_portal_count"] > 0) | (sup["mismatch_count"] > 0)
                  | (sup["missing_books_count"] > 0)]
    title = f"GST Reconciliation - {party}-wise Summary" + (" (pending only)" if only_pending else "")
    story = _header_block(result, company_name, company_gstin, period_label, title, width)
    head = ["GSTIN", f"{party} name", "Docs\nBooks", f"Docs\n{short}", "ITC Books", f"ITC {short}", "Difference",
            "Matched ITC", "Mis-\nmatch", f"Missing\nin {short}", f"ITC missing\nin {short}", "Missing\nin Books",
            "Pending ITC\n(at risk)"]
    data = [head]
    for r in sup.itertuples(index=False):
        data.append([r.gstin, Paragraph(r.party_name or "-", st["cell"]), f"{int(r.docs_books):,}",
                     f"{int(r.docs_portal):,}", _inr(r.itc_books), _inr(r.itc_portal), _inr(r.difference),
                     _inr(r.matched_itc), f"{int(r.mismatch_count):,}", f"{int(r.missing_portal_count):,}",
                     _inr(r.missing_portal_itc), f"{int(r.missing_books_count):,}", _inr(r.pending_itc)])
    if len(sup):
        t = sup.sum(numeric_only=True)
        data.append(["TOTAL", f"{len(sup):,} {party.lower()}s", f"{int(t.docs_books):,}", f"{int(t.docs_portal):,}",
                     _inr(t.itc_books), _inr(t.itc_portal), _inr(t.difference), _inr(t.matched_itc),
                     f"{int(t.mismatch_count):,}", f"{int(t.missing_portal_count):,}", _inr(t.missing_portal_itc),
                     f"{int(t.missing_books_count):,}", _inr(t.pending_itc)])
    shares = (0.105, 0.155, 0.045, 0.045, 0.085, 0.085, 0.08, 0.085, 0.045, 0.055, 0.08, 0.055, 0.08)
    table = _table(data, [width * s for s in shares], numeric_from=2, bold_last=bool(len(sup)))
    pending_col = len(head) - 1
    table.setStyle(TableStyle([("TEXTCOLOR", (pending_col, 1), (pending_col, -1), _RED)]))
    story.append(table)
    if sup.empty:
        story.append(Paragraph("No suppliers to show.", st["note"]))
    return _build(story, page, f"{party}-wise Summary - {company_name or ''} {period_label or ''}".strip())
