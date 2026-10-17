"""Display formatting helpers shared by the matcher, reports and UI."""
from __future__ import annotations

import re

import numpy as np
import pandas as pd


def format_inr(value: float | int | None, symbol: bool = True, decimals: int = 2) -> str:
    """Format a number with Indian digit grouping.

    >>> format_inr(12345678.5)
    '₹1,23,45,678.50'
    >>> format_inr(-250)
    '-₹250.00'
    """
    if value is None or (isinstance(value, float) and np.isnan(value)) or value is pd.NA:
        return ""
    neg = value < 0
    text = f"{abs(float(value)):.{decimals}f}"
    whole, _, frac = text.partition(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        groups = re.findall(r"\d{1,2}", head[::-1])
        whole = ",".join(g[::-1] for g in reversed(groups)) + "," + tail
    out = ("₹" if symbol else "") + whole + (f".{frac}" if decimals else "")
    return f"-{out}" if neg else out


def format_date(value: object) -> str:
    """DD-MM-YYYY text for a date-like value (``""`` for missing)."""
    if value is None or pd.isna(value):
        return ""
    return pd.Timestamp(value).strftime("%d-%m-%Y")
