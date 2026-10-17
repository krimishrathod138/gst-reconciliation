"""Tests for the PDF exports (modules.pdf_reports) - text is extracted back with pypdf."""
from __future__ import annotations

import io

import pytest
from pypdf import PdfReader

from conftest import clean, doc
from modules.matcher import reconcile
from modules.pdf_reports import pdf_fonts, summary_pdf, supplier_pdf


def pdf_text(data: bytes) -> tuple[str, int]:
    reader = PdfReader(io.BytesIO(data))
    return "\n".join(p.extract_text() for p in reader.pages), len(reader.pages)


@pytest.fixture(scope="module")
def sample(request):
    import pandas as pd
    from pathlib import Path
    from modules.cleaner import clean_dataset
    from modules.loader import load_file, to_canonical
    root = Path(__file__).resolve().parents[1] / "sample_data"
    books, _ = clean_dataset(to_canonical(load_file(root / "purchase_register.xlsx")))
    portal, _ = clean_dataset(pd.concat([to_canonical(load_file(root / f, dataset="portal"))
                                         for f in ("gstr2b_apr2024.json", "gstr2b_may2024.json")]), "portal")
    return reconcile(books, portal)


def test_summary_pdf_content(sample):
    data = summary_pdf(sample, "Demo Traders Pvt Ltd", "27AABCD1234E1Z8", "Apr 2024 - May 2024")
    assert data[:5] == b"%PDF-"
    text, pages = pdf_text(data)
    assert "GST Reconciliation - Summary" in text and "Demo Traders Pvt Ltd" in text
    rupee = "₹" if pdf_fonts()[2] else "Rs."
    assert "Within ±₹10 per amount field (₹ only); dates must match" in text   # rule used for the run
    assert "ITC at Risk" in text and "Page 1 of" in text
    assert f"{rupee}39,360.00" in text                      # ITC at risk, Indian grouping
    assert "Exact Match" in text and "Net Total" in text


def test_supplier_pdf_content(sample):
    text, pages = pdf_text(supplier_pdf(sample, "Demo Traders Pvt Ltd", period_label="Apr-May 2024"))
    assert "Supplier-wise Summary" in text and "TOTAL" in text and "10 suppliers" not in text
    assert "Rajkot Engineering Works" in text and "24AAGFR8765Q1ZA" in text
    only, _ = pdf_text(supplier_pdf(sample, only_pending=True))
    assert "(pending only)" in only


def test_supplier_pdf_many_rows_paginates():
    books = clean([doc(f"I{i}", 1000, 180, gstin=g, name=f"Supplier {i}") for i, g in enumerate(_gstins(120))])
    r = reconcile(books, clean([], "portal"))
    text, pages = pdf_text(supplier_pdf(r))
    assert pages >= 3 and f"Page {pages} of {pages}" in text
    assert text.count("GSTIN") >= pages                      # header repeated on every page


def test_empty_result_pdfs():
    r = reconcile(clean([]), clean([], "portal"))
    assert summary_pdf(r)[:5] == b"%PDF-" and "No suppliers" in pdf_text(supplier_pdf(r))[0]


def test_rupee_font_available_on_windows():
    import sys
    if sys.platform == "win32":
        assert pdf_fonts()[2], "a Windows font with the ₹ glyph should be found"


def _gstins(n: int) -> list[str]:
    from modules.cleaner import gstin_check_char
    out = []
    for i in range(n):
        first14 = f"27AAAC{chr(65 + i // 26 % 26)}{chr(65 + i % 26)}{1000 + i:04d}A1Z"[:14]
        out.append(first14 + gstin_check_char(first14))
    return out
