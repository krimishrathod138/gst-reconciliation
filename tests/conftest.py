"""Shared pytest fixtures and helpers."""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modules.cleaner import clean_dataset, gstin_check_char  # noqa: E402

VALID_GSTIN = "27AAPFU0939F1ZV"          # published example GSTIN with correct checksum


def make_gstin(state: str, pan: str) -> str:
    first14 = f"{state}{pan}1Z"
    return first14 + gstin_check_char(first14)


GSTIN_KA = make_gstin("29", "AABCK4321M")


def doc(inv: str, taxable: float, igst: float = 0.0, cgst: float = 0.0, sgst: float = 0.0, *,
        date: str = "15-04-2024", gstin: str = VALID_GSTIN, typ: str = "B2B", name: str = "ABC Ltd",
        period: str | None = None, **extra) -> dict:
    """One canonical row."""
    row = dict(gstin=gstin, party_name=name, invoice_no=inv, invoice_date=date, invoice_type=typ,
               taxable_value=taxable, igst=igst, cgst=cgst, sgst=sgst, cess=0, return_period=period)
    row.update(extra)
    return row


def clean(rows: list[dict], dataset: str = "books") -> pd.DataFrame:
    frame, _ = clean_dataset(pd.DataFrame(rows), dataset)
    return frame


@pytest.fixture
def sample_dir() -> Path:
    return ROOT / "sample_data"
