"""
GSTR-3B comparison: ITC claimed in GSTR-3B vs ITC available in GSTR-2B vs ITC in books.

Input
-----
* Official GSTR-3B JSON (portal / offline tool): ``ret_period`` and Table 4
  ``itc_elg`` -> ``itc_avl`` (4A: IMPG, IMPS, ISRC, ISD, **OTH = 4A(5) All other ITC**),
  ``itc_rev`` (4B) and ``itc_net`` (4C).  Several months = several files.
* A simple table (Excel / CSV / manual entry): Month | IGST | CGST | SGST | Cess.

Comparison basis
----------------
GSTR-2B's B2B and CDNR sections - the documents this tool reconciles - feed
Table **4A(5) "All other ITC"** of GSTR-3B, so that is the claimed figure compared
month by month.  Books ITC is taken by invoice month; 2B ITC by return period and
only where ITC is available (``itc_available != "N"``).  Credit notes are negative.
Section 16(2)(aa) / Rule 36(4): ITC claimed should not exceed what 2B shows, so a
month where 3B > 2B (beyond the tolerance) is flagged **Excess claimed**.
"""
from __future__ import annotations

import json

import numpy as np
import pandas as pd

from config import normalise_header
from modules import ReconError
from modules.cleaner import normalise_period, parse_amounts
from modules.matcher import ReconResult

HEADS = ("igst", "cgst", "sgst", "cess")
GSTR3B_COLUMNS = ["period", "igst", "cgst", "sgst", "cess", "net_igst", "net_cgst", "net_sgst", "net_cess", "source"]
_JSON_KEYS = {"igst": "iamt", "cgst": "camt", "sgst": "samt", "cess": "csamt"}
_ALIASES = {
    "period": ("month", "period", "returnperiod", "retperiod", "taxperiod", "fp"),
    "igst": ("igst", "integratedtax", "igstclaimed", "itcigst"),
    "cgst": ("cgst", "centraltax", "cgstclaimed", "itccgst"),
    "sgst": ("sgst", "stateuttax", "sgstutgst", "statetax", "sgstclaimed", "itcsgst"),
    "cess": ("cess", "cessclaimed", "itccess"),
}
TOLERANCE = 1.0  # rupees - differences below this are treated as equal


def empty_gstr3b() -> pd.DataFrame:
    return pd.DataFrame(columns=GSTR3B_COLUMNS)


def _num(value) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def parse_gstr3b_json(data: bytes | str | dict) -> pd.DataFrame:
    """Parse one GSTR-3B JSON into a one-row frame (period, 4A(5) ITC per head, net ITC 4C)."""
    if isinstance(data, dict):
        obj = data
    else:
        try:
            obj = json.loads(data.decode("utf-8-sig") if isinstance(data, bytes) else data)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ReconError(f"Invalid GSTR-3B JSON: {exc}") from exc
    root = obj.get("data", obj) if isinstance(obj, dict) and isinstance(obj.get("data"), dict) else obj
    if not isinstance(root, dict) or "itc_elg" not in root:
        raise ReconError("JSON not recognised as GSTR-3B (no 'itc_elg' / Table 4 section found).")
    period = normalise_period(pd.Series([root.get("ret_period") or root.get("rtnprd") or ""])).iloc[0]
    if not period:
        raise ReconError("GSTR-3B JSON has no valid 'ret_period'.")
    elg = root["itc_elg"] or {}
    oth = next((r for r in elg.get("itc_avl", []) or [] if str(r.get("ty", "")).upper() == "OTH"), {})
    net = elg.get("itc_net", {}) or {}
    row = {"period": period, "source": "GSTR-3B JSON"}
    for head, key in _JSON_KEYS.items():
        row[head] = _num(oth.get(key))
        row["net_" + head] = _num(net.get(key))
    return pd.DataFrame([row], columns=GSTR3B_COLUMNS)


def parse_gstr3b_table(frame: pd.DataFrame, source: str = "table") -> pd.DataFrame:
    """Parse a Month | IGST | CGST | SGST | Cess table (Excel / CSV / manual entry)."""
    norms = {c: normalise_header(c) for c in frame.columns if not str(c).startswith("__")}
    pick: dict[str, str] = {}
    for field, aliases in _ALIASES.items():
        col = next((c for a in aliases for c, n in norms.items() if n == a and c not in pick.values()), None)
        if col is None:
            col = next((c for a in aliases for c, n in norms.items() if n.startswith(a) and c not in pick.values()), None)
        if col is not None:
            pick[field] = col
    missing = [f for f in ("period", "igst", "cgst", "sgst") if f not in pick]
    if missing:
        raise ReconError("GSTR-3B table needs columns: Month, IGST, CGST, SGST (and optional Cess). "
                         f"Not found: {', '.join(missing)}")
    out = pd.DataFrame({"period": normalise_period(frame[pick["period"]])})
    for head in HEADS:
        out[head] = parse_amounts(frame[pick[head]]) if head in pick else 0.0
        out["net_" + head] = np.nan
    out["source"] = source
    bad = out["period"] == ""
    if bad.all():
        raise ReconError("No valid months found in the GSTR-3B table (use e.g. 05-2024, May-2024 or 052024).")
    return out.loc[~bad, GSTR3B_COLUMNS].reset_index(drop=True)


def combine_gstr3b(frames: list[pd.DataFrame]) -> pd.DataFrame:
    """Merge 3B figures from several files; a later file for the same month replaces an earlier one."""
    frames = [f for f in frames if f is not None and len(f)]
    if not frames:
        return empty_gstr3b()
    out = pd.concat(frames, ignore_index=True).drop_duplicates("period", keep="last")
    return out.sort_values("period").reset_index(drop=True)[GSTR3B_COLUMNS]


def _monthly(frame: pd.DataFrame, month_col: str, cols: dict[str, str], sign: pd.Series) -> pd.DataFrame:
    data = pd.DataFrame({"month": frame[month_col]})
    for head, src in cols.items():
        data[head] = frame[src].fillna(0.0).astype(float) * sign
    return data[data["month"] != ""].groupby("month")[list(cols)].sum()


def compare_gstr3b(result: ReconResult, gstr3b: pd.DataFrame | None, months: list[str] | None = None) -> pd.DataFrame:
    """Month-wise ITC: books vs GSTR-2B (available) vs GSTR-3B 4A(5), with differences.

    Returns one row per month plus a ``TOTAL`` row.  Columns (``*`` = igst/cgst/sgst/cess/total):
    ``books_*``, ``gstr2b_*``, ``gstr3b_*``, ``diff_3b_2b_*``, ``diff_3b_books``, ``diff_books_2b``,
    ``gstr2b_not_available``, ``cumulative_3b_minus_2b`` and ``status``.
    """
    if result.mode != "purchase":
        raise ReconError("The GSTR-3B comparison applies to purchase reconciliations (ITC).")
    recon = result.recon
    sign = recon["sign"].astype(float)
    b = recon["has_books"]
    p = recon["has_portal"]
    avail = p & (recon.get("itc_available_portal", pd.Series("Y", index=recon.index)) != "N")
    na = p & ~avail
    books = _monthly(recon[b], "month_books", {h: f"{h}_books" for h in HEADS}, sign[b])
    portal = _monthly(recon[avail], "period_portal", {h: f"{h}_portal" for h in HEADS}, sign[avail])
    not_avail = _monthly(recon[na], "period_portal", {"total": "total_tax_portal"}, sign[na])
    g3b = (gstr3b if gstr3b is not None else empty_gstr3b()).copy()
    g3b = g3b[g3b["period"].astype(str) != ""].set_index("period")[list(HEADS)].astype(float) \
        if len(g3b) else pd.DataFrame(columns=list(HEADS), dtype=float)

    all_months = sorted(set(books.index) | set(portal.index) | set(g3b.index))
    if months:
        all_months = [m for m in all_months if m in set(months)] or list(months)
    out = pd.DataFrame(index=pd.Index(all_months, name="month"))
    for prefix, src in (("books", books), ("gstr2b", portal), ("gstr3b", g3b)):
        src = src.reindex(out.index).fillna(0.0)
        for h in HEADS:
            out[f"{prefix}_{h}"] = src[h] if h in src else 0.0
        out[f"{prefix}_total"] = out[[f"{prefix}_{h}" for h in HEADS]].sum(axis=1)
    out["gstr2b_not_available"] = not_avail.reindex(out.index)["total"].fillna(0.0) if len(not_avail) else 0.0
    for h in (*HEADS, "total"):
        out[f"diff_3b_2b_{h}"] = out[f"gstr3b_{h}"] - out[f"gstr2b_{h}"]
    out["diff_3b_books"] = out["gstr3b_total"] - out["books_total"]
    out["diff_books_2b"] = out["books_total"] - out["gstr2b_total"]
    out["cumulative_3b_minus_2b"] = out["diff_3b_2b_total"].cumsum()
    has_3b = pd.Series(out.index.isin(g3b.index), index=out.index)
    out["status"] = np.select(
        [~has_3b, out["diff_3b_2b_total"] > TOLERANCE, out["diff_3b_2b_total"] < -TOLERANCE],
        ["3B not entered", "Excess claimed", "Short claimed"], "Matches 2B")
    out = out.reset_index()
    total = out.drop(columns=["month", "status", "cumulative_3b_minus_2b"]).sum()
    total_row = {"month": "TOTAL", **total.to_dict(),
                 "cumulative_3b_minus_2b": float(out["diff_3b_2b_total"].sum()),
                 "status": ("Excess claimed" if total["diff_3b_2b_total"] > TOLERANCE else
                            "Short claimed" if total["diff_3b_2b_total"] < -TOLERANCE else "Matches 2B")
                 if has_3b.any() else "3B not entered"}
    out = pd.concat([out, pd.DataFrame([total_row])], ignore_index=True)
    num = out.columns.difference(["month", "status"])
    out[num] = out[num].astype(float).round(2)
    return out
