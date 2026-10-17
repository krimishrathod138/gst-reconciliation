"""Unit tests for modules.cleaner."""
from __future__ import annotations

from datetime import date, datetime

import numpy as np
import pandas as pd
import pytest

from conftest import VALID_GSTIN, doc
from modules.cleaner import (
    clean_dataset, clean_gstin, derive_doc_type, fy_from_dates, gstin_check_char, gstin_errors,
    normalise_invoice_no, normalise_period, normalise_pos, parse_amounts, parse_dates, text_id, validate_gstin,
)


# ----------------------------------------------------------------- GSTIN
class TestGstin:
    def test_known_valid_gstin(self):
        assert validate_gstin(VALID_GSTIN) == ""
        assert gstin_check_char(VALID_GSTIN[:14]) == "V"

    @pytest.mark.parametrize("gstin, fragment", [
        ("", "blank"),
        (None, "blank"),
        ("27AAPFU0939F1Z", "15 characters"),
        ("27AAPFU0939F1ZVX", "15 characters"),
        ("27AAPFU0939F1ZA", "Checksum"),
        ("40AAPFU0939F1ZV", "state code"),
        ("2AAAPFU0939F1ZV", "state code"),
        ("271APFU0939F1ZV", "PAN"),
        ("27AAPFUA939F1ZV", "PAN"),
        ("27AAPFU0939F0ZV", "entity"),
        ("27AAPFU0939F1Z-", "invalid characters"),
    ])
    def test_invalid_gstins(self, gstin, fragment):
        assert fragment.lower() in validate_gstin(gstin).lower()

    def test_lowercase_is_validated_after_cleaning(self):
        cleaned = clean_gstin(pd.Series([" 27aapfu0939f1zv ", "27 AAPFU 0939F1ZV"]))
        assert cleaned.tolist() == [VALID_GSTIN, VALID_GSTIN]
        assert gstin_errors(cleaned).tolist() == ["", ""]

    def test_vectorised_matches_scalar(self):
        s = pd.Series([VALID_GSTIN, "BAD", VALID_GSTIN, ""])
        assert gstin_errors(s).tolist() == [validate_gstin(v) for v in s]


# ----------------------------------------------------------------- invoice numbers
class TestInvoiceNormalisation:
    @pytest.mark.parametrize("raw, expected", [
        ("INV/2024-25/0012", "INV12"),
        ("inv-12", "INV12"),
        ("INV 12", "INV12"),
        ("24-25/0045", "45"),
        ("0045/24-25", "45"),
        ("FY2024-25/12", "12"),
        ("FY 24-25/12", "12"),
        ("2023-2024/77", "77"),
        ("INV/12/2024/25", "INV12"),
        ("0000123", "123"),
        ("A.B#12", "AB12"),
        ("100", "100"),
        ("0", "0"),
        ("10-005", "105"),
        ("INV-12-34", "INV1234"),       # 12-34 is not a financial year
        ("INV/2024-26/5", "INV2024265"),  # 2024-26 is not a financial year
        ("24-25", "2425"),              # nothing left after FY removal -> fallback
        ("   ", ""),
    ])
    def test_cases(self, raw, expected):
        assert normalise_invoice_no(pd.Series([raw])).iloc[0] == expected

    def test_excel_float_numbers(self):
        assert normalise_invoice_no(pd.Series([123.0, 45, None])).tolist() == ["123", "45", ""]

    def test_text_id_keeps_leading_zero_strings(self):
        assert text_id(pd.Series(["00123", 123.0, 12.5])).tolist() == ["00123", "123", "12.5"]

    def test_equivalent_formats_normalise_identically(self):
        variants = ["SGS/24-25/0012", "SGS/0012", "SGS-12", "sgs 12", "SGS#12/2024-25"]
        assert normalise_invoice_no(pd.Series(variants)).nunique() == 1


# ----------------------------------------------------------------- dates
class TestDates:
    @pytest.mark.parametrize("value", [
        "15-04-2024", "15/04/2024", "15.04.2024", "15-04-24", "2024-04-15", "2024/04/15", "15-Apr-2024",
        "15 April 2024", "2024-04-15 00:00:00", 45397, "45397", datetime(2024, 4, 15, 13, 30), date(2024, 4, 15),
        pd.Timestamp("2024-04-15"), "20240415",
    ])
    def test_formats(self, value):
        assert parse_dates(pd.Series([value], dtype=object)).iloc[0] == pd.Timestamp("2024-04-15")

    def test_day_first_ambiguity(self):
        assert parse_dates(pd.Series(["04/05/2024"])).iloc[0] == pd.Timestamp("2024-05-04")

    def test_invalid_and_blank(self):
        out = parse_dates(pd.Series(["31-02-2024", "not a date", None, ""], dtype=object))
        assert out.isna().all()

    def test_datetime_series_normalised(self):
        s = pd.Series(pd.to_datetime(["2024-04-15 10:00", "2024-05-01 23:59"]))
        assert parse_dates(s).tolist() == [pd.Timestamp("2024-04-15"), pd.Timestamp("2024-05-01")]

    def test_financial_year(self):
        s = pd.Series(pd.to_datetime(["2024-03-31", "2024-04-01", "2025-01-15", None]))
        assert fy_from_dates(s).tolist() == ["2023-24", "2024-25", "2024-25", ""]

    @pytest.mark.parametrize("value, expected", [
        ("052024", "2024-05"), (52024, "2024-05"), ("05-2024", "2024-05"), ("2024-05", "2024-05"),
        ("May'24", "2024-05"), ("May-2024", "2024-05"), ("MAY 2024", "2024-05"), ("132024", ""),
        (pd.Timestamp("2024-05-10"), "2024-05"), ("", ""), (None, ""),
    ])
    def test_periods(self, value, expected):
        assert normalise_period(pd.Series([value], dtype=object)).iloc[0] == expected


# ----------------------------------------------------------------- amounts / types / POS
class TestAmountsAndTypes:
    def test_amount_strings(self):
        s = pd.Series(["1,23,456.789", "₹ 500", "Rs. 1,000", "", None, "(100)", "250-", "abc", 12.345], dtype=object)
        assert parse_amounts(s).tolist() == [123456.79, 500.0, 1000.0, 0.0, 0.0, -100.0, -250.0, 0.0, 12.35]

    def test_numeric_series_rounding(self):
        assert parse_amounts(pd.Series([1.005, 2.675, np.nan])).tolist() == [1.01, 2.68, 0.0]

    def test_doc_types(self):
        it = pd.Series(["B2B", "Regular", "CDNR", "Credit Note", "Debit Note", "CN", "DN", None, "CDNR", "SEZWP"])
        nt = pd.Series([None, None, None, None, None, None, None, None, "D", None])
        assert derive_doc_type(it, nt).tolist() == ["INV", "INV", "CN", "CN", "DN", "CN", "DN", "INV", "DN", "INV"]

    def test_place_of_supply(self):
        s = pd.Series(["27-Maharashtra", "Karnataka", "7", "29", "", None, "Unknownland"])
        assert normalise_pos(s).tolist() == ["27", "29", "07", "29", "", "", "Unknownland"]


# ----------------------------------------------------------------- clean_dataset
class TestCleanDataset:
    def test_blank_and_total_rows_dropped(self):
        rows = [doc("A1", 100, 18), dict(gstin=None, invoice_no=None, party_name="Total", taxable_value=100),
                dict(gstin=VALID_GSTIN, invoice_no="", taxable_value=5)]
        out, rep = clean_dataset(pd.DataFrame(rows))
        assert len(out) == 1
        assert rep.dropped_blank == 1 and len(rep.dropped_no_invoice) == 1

    def test_amounts_are_magnitudes_and_sign_marks_credit_notes(self):
        out, _ = clean_dataset(pd.DataFrame([doc("CN1", -1000, -180, typ="Credit Note"), doc("I1", 1000, 180)]))
        assert out["taxable_value"].tolist() == [1000.0, 1000.0]
        assert out["sign"].tolist() == [-1, 1]
        assert out["doc_type"].tolist() == ["CN", "INV"]

    def test_invoice_value_computed_when_missing(self):
        out, rep = clean_dataset(pd.DataFrame([doc("A1", 1000, 0, 90, 90)]))
        assert out.loc[0, "invoice_value"] == 1180.0 and out.loc[0, "total_tax"] == 180.0
        assert rep.computed_invoice_value == 1

    def test_duplicates_flagged_with_sequence(self):
        rows = [doc("INV/001", 100, 18), doc("INV-1", 100, 18), doc("INV 2", 100, 18),
                doc("INV1", 100, 18, typ="Credit Note")]  # same number but a credit note - not a duplicate
        out, rep = clean_dataset(pd.DataFrame(rows))
        assert out["is_duplicate"].tolist() == [True, True, False, False]
        assert out["dup_seq"].tolist() == [0, 1, 0, 0]
        assert rep.duplicate_rows == 2

    def test_period_defaults_to_invoice_month(self):
        out, _ = clean_dataset(pd.DataFrame([doc("A", 1, date="05-04-2024"), doc("B", 1, date="05-04-2024", period="052024")]))
        assert out["period"].tolist() == ["2024-04", "2024-05"]
        assert out["fy"].tolist() == ["2024-25", "2024-25"]

    def test_original_invoice_kept_for_display(self):
        out, _ = clean_dataset(pd.DataFrame([doc(" INV/24-25/007 ", 1)]))
        assert out.loc[0, "invoice_no"] == "INV/24-25/007"
        assert out.loc[0, "inv_norm"] == "INV7"

    def test_invalid_gstin_flagged_not_dropped(self):
        out, rep = clean_dataset(pd.DataFrame([doc("A", 1, gstin="27AAPFU0939F1ZA")]))
        assert len(out) == 1 and "Checksum" in out.loc[0, "gstin_error"] and rep.invalid_gstins == 1

    def test_empty_input(self):
        out, rep = clean_dataset(pd.DataFrame())
        assert out.empty and rep.rows_in == 0
        out, _ = clean_dataset(None)
        assert out.empty

    def test_missing_optional_columns(self):
        frame = pd.DataFrame([{"gstin": VALID_GSTIN, "invoice_no": "X1", "invoice_date": "01-04-2024",
                               "taxable_value": "1,000"}])
        out, _ = clean_dataset(frame)
        assert out.loc[0, "igst"] == 0 and out.loc[0, "doc_type"] == "INV" and out.loc[0, "itc_available"] == "Y"

    def test_large_input_is_fast(self):
        import time
        n = 100_000
        frame = pd.DataFrame({
            "gstin": [VALID_GSTIN] * n, "invoice_no": [f"INV/24-25/{i:06d}" for i in range(n)],
            "invoice_date": ["15-04-2024"] * n, "taxable_value": np.arange(n, dtype=float), "igst": 18.0,
        })
        t = time.perf_counter()
        out, _ = clean_dataset(frame)
        assert len(out) == n and out["inv_norm"].iloc[5] == "INV5"
        assert time.perf_counter() - t < 30
