"""
Financial-year / month-range handling and carry-forward of pending documents.

* A run covers one Indian financial year (April-March) and a month range inside it.
  Books are selected by **invoice month**, portal documents by **return period**
  (the month in which they appear in GSTR-2B / 2A / 1).
* Documents that were still *Missing in <portal>* at the end of the previous saved
  run are **carried forward** into the next run (tagged ``carried_from``) and keep
  being carried until they appear on the portal.
* :func:`carry_forward_schedule` produces the month-by-month movement:
  opening pending -> new documents -> reflected on the portal -> closing pending.

Months are plain ``"YYYY-MM"`` strings throughout.
"""
from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd

from modules.cleaner import combine_clean, recompute_keys
from modules.matcher import ReconResult


def fy_months(fy: str) -> list[str]:
    """Months of a financial year: ``"2024-25"`` -> ``["2024-04", ..., "2025-03"]``."""
    start = int(fy[:4])
    return [f"{start + (m < 4)}-{m:02d}" for m in (*range(4, 13), *range(1, 4))]


def fy_of_month(month: str) -> str:
    """Financial year of a month: ``"2025-02"`` -> ``"2024-25"``."""
    y, m = int(month[:4]), int(month[5:7])
    start = y if m >= 4 else y - 1
    return f"{start}-{(start + 1) % 100:02d}"


def month_label(month: str) -> str:
    """``"2024-05"`` -> ``"May 2024"`` (``""`` for blank)."""
    if not month:
        return ""
    return date(int(month[:4]), int(month[5:7]), 1).strftime("%b %Y")


def range_label(from_month: str, to_month: str) -> str:
    """Human label for a month range, e.g. ``"Apr 2024 - Jun 2024"``."""
    a, b = month_label(from_month), month_label(to_month)
    return a if a == b else f"{a} - {b}"


def available_fys(*frames: pd.DataFrame) -> list[str]:
    """Financial years present in the month / period columns of cleaned frames (newest first)."""
    months: set[str] = set()
    for f in frames:
        for col in ("month", "period"):
            if f is not None and col in f:
                months |= {m for m in f[col].dropna().astype(str) if len(m) == 7}
    return sorted({fy_of_month(m) for m in months}, reverse=True)


def filter_books(books: pd.DataFrame, from_month: str, to_month: str) -> pd.DataFrame:
    """Books documents whose invoice month is inside the range."""
    keep = books["month"].between(from_month, to_month)
    return recompute_keys(books[keep])


def filter_portal(portal: pd.DataFrame, from_month: str, to_month: str) -> pd.DataFrame:
    """Portal documents whose return period is inside the range."""
    keep = portal["period"].between(from_month, to_month)
    return recompute_keys(portal[keep])


def pending_documents(previous: ReconResult, label: str) -> pd.DataFrame:
    """Books documents still missing on the portal at the end of *previous* (review decisions applied).

    Each keeps its original ``carried_from`` label, or gets *label* when first carried.
    """
    recon = previous.recon
    missing = recon[(recon["status"] == previous.missing_portal_status) & recon["has_books"]]
    rids = pd.to_numeric(missing["_rid_books"], errors="coerce").dropna().astype(int)
    books = previous.books
    if "carried_from" not in books:
        books = books.assign(carried_from="")
    carried = books[books["_rid"].astype(int).isin(set(rids))].copy()
    first_time = carried["carried_from"].fillna("").astype(str) == ""
    carried.loc[first_time, "carried_from"] = label
    return carried


def add_carried(books: pd.DataFrame, carried: pd.DataFrame | None) -> tuple[pd.DataFrame, int]:
    """Append carried-forward documents to this run's books.

    A carried document that is also present in the new books (same GSTIN, doc type
    and normalised invoice number) is dropped - the new entry wins.

    Returns:
        ``(combined_books, number_of_documents_carried)``.
    """
    if carried is None or carried.empty:
        return books, 0
    key = ["gstin", "doc_type", "inv_norm"]
    new_keys = set(map(tuple, books[key].astype(str).to_numpy())) if len(books) else set()
    keep = [tuple(k) not in new_keys for k in carried[key].astype(str).to_numpy()]
    carried = carried[keep]
    return combine_clean([books, carried]), int(len(carried))


def carry_forward_schedule(result: ReconResult, months: list[str]) -> pd.DataFrame:
    """Month-by-month movement of books documents until they appear on the portal.

    For every month: pending at the start (opening), new books documents,
    documents reflected on the portal in that month's return (matched, within
    tolerance, mismatched or probable), and still pending at the end (closing).
    Carried-forward documents and invoices dated before the range count as opening
    in the first month.  Values are net ITC (credit notes negative).
    """
    cols = ["month", "opening_count", "opening_itc", "added_count", "added_itc", "reflected_count",
            "reflected_itc", "closing_count", "closing_itc"]
    if not months:
        return pd.DataFrame(columns=cols)
    recon = result.recon
    b = recon[recon["has_books"]]
    index = {m: i for i, m in enumerate(months)}
    carried = b.get("carried_from_books", pd.Series("", index=b.index)).fillna("") != ""
    start = b["month_books"].map(index).astype(float)
    start = start.where(~carried & (b["month_books"] >= months[0]), -1.0).fillna(-1.0)
    # month in which the document appeared on the portal; beyond the range = still pending
    end = b["period_portal"].map(index).astype(float)
    end = end.where(b["has_portal"], np.inf)
    end = end.where(end.notna() | ~b["has_portal"], np.inf)
    end = np.maximum(end, start)
    before = b["has_portal"] & (b["period_portal"] < months[0])
    end = end.where(~before, start)
    itc = b["itc_books"].astype(float)
    rows = []
    for i, m in enumerate(months):
        opening = (start < i) & (end >= i)
        added = start == i
        reflected = (end == i) & (start <= i)
        closing = (start <= i) & (end > i)
        rows.append([m, int(opening.sum()), itc[opening].sum(), int(added.sum()), itc[added].sum(),
                     int(reflected.sum()), itc[reflected].sum(), int(closing.sum()), itc[closing].sum()])
    return pd.DataFrame(rows, columns=cols).round(2)


def add_pending_age(recon: pd.DataFrame, to_month: str) -> pd.DataFrame:
    """Add ``months_pending``: months a books document waited for the portal (or is still waiting)."""
    out = recon.copy()

    def month_no(s: pd.Series) -> pd.Series:
        s = s.fillna("").astype(str)
        ok = s.str.fullmatch(r"\d{4}-\d{2}")
        return (s.where(ok, "0000-00").str[:4].astype(int) * 12 + s.where(ok, "0000-00").str[5:].astype(int)).where(ok)

    start = month_no(out["month_books"])
    end = month_no(out["period_portal"]).where(out["has_portal"], month_no(pd.Series(to_month, index=out.index)))
    age = (end - start).clip(lower=0)
    out["months_pending"] = age.where(out["has_books"]).astype("Float64")
    return out
