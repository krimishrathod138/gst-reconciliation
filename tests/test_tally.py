"""Tests for Tally register import (modules.tally)."""
from __future__ import annotations

import pandas as pd
import pytest

from conftest import VALID_GSTIN
from modules.cleaner import clean_dataset
from modules.loader import load_file, missing_required, suggest_mapping, to_canonical
from modules.matcher import reconcile
from modules.tally import convert_tally_register, is_tally_register, tally_amounts


def tally_frame(rows: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(rows)


BASE = {"Date": "2-Apr-24", "Particulars": "ABC Ltd", "Voucher Type": "Purchase", "Voucher No.": "15",
        "Supplier Invoice No.": "ABC/24-25/0012", "Supplier Invoice Date": "1-Apr-24", "GSTIN/UIN": VALID_GSTIN,
        "Gross Total": 11800, "Purchase @ 18%": 10000, "Input CGST @ 9%": 900, "Input SGST @ 9%": 900,
        "Round Off": None}


def test_detection():
    assert is_tally_register(tally_frame([BASE]))
    assert not is_tally_register(pd.DataFrame({"GSTIN": [1], "Invoice No": [1], "Taxable Value": [1]}))


def test_amount_parsing_dr_cr():
    s = pd.Series(["1,180.00 Dr", "50.00 Cr", "(20)", None, 7.5], dtype=object)
    assert tally_amounts(s).tolist() == [1180.0, 50.0, 20.0, 0.0, 7.5]
    assert tally_amounts(s, signed=True).tolist() == [1180.0, -50.0, -20.0, 0.0, 7.5]


def test_purchase_conversion_uses_supplier_invoice_and_tax_ledgers():
    out, notes = convert_tally_register(tally_frame([BASE]), "purchase")
    rec = out.iloc[0]
    assert rec["Invoice No"] == "ABC/24-25/0012" and rec["Invoice Date"] == "1-Apr-24"
    assert (rec["Taxable Value"], rec["CGST"], rec["SGST"], rec["IGST"]) == (10000, 900, 900, 0)
    assert rec["Total Invoice Value"] == 11800 and rec["Supplier Name"] == "ABC Ltd"
    assert not missing_required(suggest_mapping(out.columns))
    assert any("Supplier Invoice No." in n for n in notes)


def test_negative_round_off_and_freight_in_taxable():
    row = {**BASE, "Gross Total": 12036, "Freight Inward": 200, "Input CGST @ 9%": 918, "Input SGST @ 9%": 918,
           "Round Off": "0.00 Cr"}
    row2 = {**BASE, "Voucher No.": "16", "Supplier Invoice No.": "X2", "Gross Total": "11,800.00 Dr",
            "Purchase @ 18%": 10000.4, "Input CGST @ 9%": 900.04, "Input SGST @ 9%": 900.04, "Round Off": "0.48 Cr"}
    out, _ = convert_tally_register(tally_frame([row, row2]), "purchase")
    assert out["Taxable Value"].tolist() == [10200.0, 10000.40]   # freight is part of the taxable value


def test_voucher_types_map_to_gst_document_types():
    rows = [{**BASE, "Voucher Type": t, "Voucher No.": str(i), "Supplier Invoice No.": f"D{i}"}
            for i, t in enumerate(["Purchase", "Debit Note", "Credit Note"])]
    out, _ = convert_tally_register(tally_frame(rows), "purchase")
    docs, _ = clean_dataset(to_canonical_frame(out))
    assert docs["doc_type"].tolist() == ["INV", "CN", "DN"]   # purchase return = supplier's credit note
    sales = [{"Date": "3-May-24", "Particulars": "Cust", "Voucher Type": t, "Voucher No.": f"S{i}",
              "GSTIN/UIN": VALID_GSTIN, "Gross Total": 118, "Sales @ 18%": 100, "Output IGST": 18}
             for i, t in enumerate(["Sales", "Credit Note", "Debit Note"])]
    out, _ = convert_tally_register(tally_frame(sales), "sales")
    docs, _ = clean_dataset(to_canonical_frame(out))
    assert docs["doc_type"].tolist() == ["INV", "CN", "DN"]
    assert docs["invoice_no"].tolist() == ["S0", "S1", "S2"] and docs["igst"].tolist() == [18, 18, 18]


def test_voucher_number_fallback_and_ignored_rows():
    rows = [{**BASE, "Supplier Invoice No.": None, "Voucher No.": "77"},
            {"Date": "2-Apr-24", "Particulars": "(cancelled)", "Voucher Type": "Purchase", "Voucher No.": "78"},
            {"Particulars": "Grand Total", "Gross Total": 11800}]
    out, notes = convert_tally_register(tally_frame(rows), "purchase")
    assert out["Invoice No"].tolist() == ["77"]
    assert any("1 voucher(s) without supplier invoice" in n for n in notes)
    assert any("1 cancelled" in n for n in notes)


def test_explicit_taxable_column_and_no_gross():
    row = {k: v for k, v in BASE.items() if k != "Gross Total"}
    out, _ = convert_tally_register(tally_frame([row]), "purchase")
    assert out.loc[0, "Taxable Value"] == 10000    # purchase ledger sum when there is no Gross Total
    out, _ = convert_tally_register(tally_frame([{**BASE, "Taxable Value": 9999}]), "purchase")
    assert out.loc[0, "Taxable Value"] == 9999


def to_canonical_frame(standard: pd.DataFrame) -> pd.DataFrame:
    from modules.loader import apply_mapping
    return apply_mapping(standard, suggest_mapping(standard.columns))


@pytest.mark.parametrize("mode, books, plain, portal, name", [
    ("purchase", "tally_purchase_register.xlsx", "purchase_register.xlsx",
     ["gstr2b_apr2024.json", "gstr2b_may2024.json"], "GSTR-2B"),
    ("sales", "tally_sales_register.xlsx", "sales_register.xlsx", ["gstr1_may2024.json"], "GSTR-1"),
])
def test_tally_sample_reconciles_like_plain_register(sample_dir, mode, books, plain, portal, name):
    lf = load_file(sample_dir / books, mode=mode)
    assert lf.meta.get("tally") and lf.kind == "table"
    p, _ = clean_dataset(pd.concat([to_canonical(load_file(sample_dir / f, dataset="portal")) for f in portal]), "portal")
    tally_books, _ = clean_dataset(to_canonical(lf))
    plain_books, _ = clean_dataset(to_canonical(load_file(sample_dir / plain, mode=mode)))
    a = reconcile(tally_books, p, portal_name=name, mode=mode)
    b = reconcile(plain_books, p, portal_name=name, mode=mode)
    assert a.recon["status"].value_counts().to_dict() == b.recon["status"].value_counts().to_dict()
    assert a.summary()["itc_at_risk"] == b.summary()["itc_at_risk"]


def test_portal_upload_never_treated_as_tally(sample_dir):
    assert not load_file(sample_dir / "tally_purchase_register.xlsx", dataset="portal").meta.get("tally")
