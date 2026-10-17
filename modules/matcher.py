"""
Reconciliation engine.

Matching tiers (applied in this order, each document is used at most once):

1. **Exact Match** - same GSTIN + doc type + normalised invoice no, same date,
   taxable value and every tax head equal.
2. **Matched with Tolerance** - same key, amounts within the configured ₹ / %
   tolerance, date within ``date_tolerance_days`` and no tax-head switch.
3. **Mismatch** - same key but any other difference; per-field differences and
   a reason text are produced.
4. **Probable Match** - remaining documents of the same GSTIN + doc type whose
   invoice numbers are similar (rapidfuzz ratio >= threshold) *and* whose
   taxable value and total tax agree within tolerance.  Needs user review.
5. **Missing in <portal>** - only in books (ITC at risk / supplier follow-up).
6. **Missing in Books** - only on the portal.

Credit notes, debit notes and invoices are matched only against the same
document type; ``sign`` (-1 for credit notes) nets them in every summary.
Documents are matched across months (April invoice in May's 2B is still a
match and is flagged ``cross_period``).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

import numpy as np
import pandas as pd
from rapidfuzz import fuzz, process

from config import (
    DECISION_ACCEPT, DECISION_PENDING, DECISION_REJECT, DOC_CREDIT, MATCHED_STATUSES, STATUS_EXACT,
    STATUS_MANUAL, STATUS_MISMATCH, STATUS_MISSING_BOOKS, STATUS_PROBABLE, STATUS_TOLERANCE,
    ReconSettings, status_missing_portal,
)
from modules.cleaner import empty_clean_frame
from modules.formatting import format_inr

B, P = "_books", "_portal"
SIDE_COLS: tuple[str, ...] = (
    "_rid", "gstin", "gstin_error", "party_name", "invoice_no", "inv_norm", "invoice_date", "doc_type",
    "taxable_value", "igst", "cgst", "sgst", "cess", "total_tax", "invoice_value", "place_of_supply",
    "period", "month", "fy", "itc_available", "is_duplicate", "section", "source_file", "source_row",
    "carried_from",
)
TEXT_SIDE_COLS: tuple[str, ...] = (
    "gstin", "gstin_error", "party_name", "invoice_no", "inv_norm", "doc_type", "place_of_supply",
    "period", "month", "fy", "itc_available", "section", "source_file", "carried_from",
)
COMPARE_FIELDS: tuple[str, ...] = ("taxable_value", "igst", "cgst", "sgst", "cess")
DIFF_FIELDS: tuple[str, ...] = (*COMPARE_FIELDS, "total_tax", "invoice_value")
FIELD_LABELS: dict[str, str] = {
    "taxable_value": "Taxable value", "igst": "IGST", "cgst": "CGST", "sgst": "SGST", "cess": "Cess",
    "total_tax": "Total tax", "invoice_value": "Invoice value",
}
_EPS = 0.005  # amounts are rounded to paise; anything below half a paisa is equal


# --------------------------------------------------------------------------- #
# Tolerance helpers
# --------------------------------------------------------------------------- #
def within_tolerance(a: np.ndarray | pd.Series, b: np.ndarray | pd.Series, settings: ReconSettings) -> np.ndarray:
    """Element-wise test whether two amounts agree within the configured tolerance.

    Broadcasts, so it also works on ``(n, 1)`` vs ``(1, m)`` arrays for fuzzy matching.
    """
    a = np.asarray(a, dtype=float)
    b = np.asarray(b, dtype=float)
    diff = np.abs(a - b)
    abs_ok = diff <= settings.abs_tolerance + _EPS
    pct_ok = diff <= np.maximum(np.abs(a), np.abs(b)) * settings.pct_tolerance / 100.0 + _EPS
    mode = settings.tolerance_mode
    if mode == "absolute":
        return abs_ok
    if mode == "percentage":
        return pct_ok
    if mode == "both":
        return abs_ok & pct_ok
    return abs_ok | pct_ok


def _tax_head(igst: np.ndarray, cgst_sgst: np.ndarray) -> np.ndarray:
    """Classify the tax head used by a document: IGST, CGST+SGST, Mixed or Nil."""
    return np.select(
        [(igst > 0) & (cgst_sgst <= 0), (cgst_sgst > 0) & (igst <= 0), (igst > 0) & (cgst_sgst > 0)],
        ["IGST", "CGST+SGST", "Mixed"], "Nil",
    )


# --------------------------------------------------------------------------- #
# Result container
# --------------------------------------------------------------------------- #
@dataclass
class ReconResult:
    """Output of :func:`reconcile`.

    Attributes:
        recon: One row per matched pair or unmatched document (see :func:`reconcile`).
        books: Cleaned books frame.
        portal: Cleaned portal frame.
        settings: Settings used.
        portal_name: "GSTR-2B", "GSTR-2A" or "GSTR-1".
        mode: "purchase" or "sales".
    """

    recon: pd.DataFrame
    books: pd.DataFrame
    portal: pd.DataFrame
    settings: ReconSettings = field(default_factory=ReconSettings)
    portal_name: str = "GSTR-2B"
    mode: str = "purchase"
    created_at: datetime = field(default_factory=datetime.now)

    @property
    def missing_portal_status(self) -> str:
        return status_missing_portal(self.portal_name)

    def with_decisions(self, decisions: pd.DataFrame | None) -> "ReconResult":
        """Copy of this result with manual review decisions applied."""
        return ReconResult(apply_review(self.recon, decisions, self.portal_name), self.books, self.portal,
                           self.settings, self.portal_name, self.mode, self.created_at)

    def summary(self) -> dict[str, float | int]:
        out = summarize(self.recon, self.portal_name)
        out["invalid_gstin_books"] = int((self.books["gstin_error"] != "").sum())
        out["invalid_gstin_portal"] = int((self.portal["gstin_error"] != "").sum())
        out["duplicates_books"] = int(self.books["is_duplicate"].sum())
        out["duplicates_portal"] = int(self.portal["is_duplicate"].sum())
        return out


# --------------------------------------------------------------------------- #
# Frame assembly
# --------------------------------------------------------------------------- #
def _side(clean: pd.DataFrame, suffix: str) -> pd.DataFrame:
    """Select side columns and suffix them; adds the exact-match key."""
    if "carried_from" not in clean:  # frames saved by older versions
        clean = clean.assign(carried_from="")
    side = clean[list(SIDE_COLS)].copy()
    key = (clean["gstin"].astype(str) + "|" + clean["doc_type"].astype(str) + "|"
           + clean["inv_norm"].astype(str) + "|" + clean["dup_seq"].astype(str))
    side.columns = [c + suffix for c in SIDE_COLS]
    side["_key" + suffix] = key.to_numpy()
    return side


def _blank_side(n: int, suffix: str, template: pd.DataFrame) -> pd.DataFrame:
    """A side frame of *n* empty rows with the same columns as *template* (dates stay datetime)."""
    cols = {c: pd.Series([pd.NaT] * n, dtype="datetime64[ns]")
            if pd.api.types.is_datetime64_any_dtype(template[c]) else pd.Series([None] * n, dtype=object)
            for c in template.columns}
    return pd.DataFrame(cols)


def _finalize(frame: pd.DataFrame, portal_name: str) -> pd.DataFrame:
    """(Re)compute combined, difference and signed-ITC columns from the two sides.

    Idempotent: safe to call again after a side has been blanked (rejected match).
    """
    f = frame.reset_index(drop=True)
    has_b = f["_rid" + B].notna()
    has_p = f["_rid" + P].notna()
    f["has_books"], f["has_portal"] = has_b.to_numpy(), has_p.to_numpy()
    for c in TEXT_SIDE_COLS:
        for sfx in (B, P):
            if c + sfx not in f:  # results saved by older versions
                f[c + sfx] = ""
            f[c + sfx] = f[c + sfx].astype(object).where(f[c + sfx].notna(), "")
    for c in (*DIFF_FIELDS,):
        for sfx, has in ((B, has_b), (P, has_p)):
            f[c + sfx] = pd.to_numeric(f[c + sfx], errors="coerce").astype(float).where(has)
    for sfx in (B, P):
        f["invoice_date" + sfx] = pd.to_datetime(f["invoice_date" + sfx], errors="coerce").astype("datetime64[ns]")
        f["is_duplicate" + sfx] = f["is_duplicate" + sfx].astype(object).where(
            f["is_duplicate" + sfx].notna(), False).astype(bool)

    def pick(col: str) -> pd.Series:
        return f[col + B].where(f[col + B] != "", f[col + P])

    f["gstin"] = pick("gstin")
    f["party_name"] = pick("party_name")
    f["doc_type"] = pick("doc_type")
    f["month"] = pick("month")
    f["fy"] = pick("fy")
    f["invoice_date"] = f["invoice_date" + B].fillna(f["invoice_date" + P])
    f["sign"] = np.where(f["doc_type"] == DOC_CREDIT, -1, 1)

    both = has_b & has_p
    for c in DIFF_FIELDS:
        f["diff_" + c] = (f[c + B] - f[c + P]).round(2).where(both)
    f["date_diff_days"] = (f["invoice_date" + B] - f["invoice_date" + P]).dt.days.where(both)
    f["cross_period"] = (both & (f["period" + B] != f["period" + P])).to_numpy()

    f["itc_books"] = (f["sign"] * f["total_tax" + B].fillna(0.0)).round(2)
    f["itc_portal"] = (f["sign"] * f["total_tax" + P].fillna(0.0)).round(2)
    f["taxable_books"] = (f["sign"] * f["taxable_value" + B].fillna(0.0)).round(2)
    f["taxable_portal"] = (f["sign"] * f["taxable_value" + P].fillna(0.0)).round(2)
    return f


def _classify(f: pd.DataFrame, mask: pd.Series, settings: ReconSettings, portal_name: str) -> None:
    """Set ``status`` / ``reason`` in place for rows in *mask* (same-key pairs)."""
    if not mask.any():
        return
    sub = f[mask]
    exact_amt = np.ones(len(sub), dtype=bool)
    tol_amt = np.ones(len(sub), dtype=bool)
    reason = pd.Series("", index=sub.index, dtype=object)
    for c in COMPARE_FIELDS:
        bv, pv = sub[c + B].to_numpy(float), sub[c + P].to_numpy(float)
        diff = sub["diff_" + c]
        differs = (diff.abs() >= _EPS).to_numpy()
        exact_amt &= ~differs
        tol_amt &= within_tolerance(bv, pv, settings)
        if differs.any():
            text = FIELD_LABELS[c] + " diff " + diff.map(format_inr) + "; "
            reason = reason.where(~differs, reason + text)

    dd = sub["date_diff_days"]
    date_same = (dd == 0).to_numpy()
    date_tol = (dd.abs() <= settings.date_tolerance_days).to_numpy()
    date_text = pd.Series(np.where(dd.isna(), "Invoice date missing/invalid; ",
                                   "Date differs by " + dd.abs().fillna(0).astype(int).astype(str) + " day(s); "),
                          index=sub.index)
    reason = reason.where(date_same, reason + date_text)

    head_b = _tax_head(sub["igst" + B].to_numpy(float), (sub["cgst" + B] + sub["sgst" + B]).to_numpy(float))
    head_p = _tax_head(sub["igst" + P].to_numpy(float), (sub["cgst" + P] + sub["sgst" + P]).to_numpy(float))
    real_heads = np.isin(head_b, ["IGST", "CGST+SGST"]) & np.isin(head_p, ["IGST", "CGST+SGST"])
    head_mismatch = (head_b != head_p) & real_heads
    head_text = "Tax head: " + pd.Series(head_b, index=sub.index) + " in books vs " \
        + pd.Series(head_p, index=sub.index) + f" in {portal_name}; "
    reason = pd.Series(np.where(head_mismatch, head_text + reason, reason), index=sub.index)

    status = np.select([exact_amt & date_same, tol_amt & date_tol & ~head_mismatch],
                       [STATUS_EXACT, STATUS_TOLERANCE], STATUS_MISMATCH)
    reason = reason.str.rstrip("; ")
    reason = pd.Series(np.where(status == STATUS_TOLERANCE, "Within tolerance: " + reason, reason),
                       index=sub.index)
    f.loc[mask, "status"] = status
    f.loc[mask, "reason"] = reason.to_numpy()


# --------------------------------------------------------------------------- #
# Fuzzy matching
# --------------------------------------------------------------------------- #
def fuzzy_candidates(books_left: pd.DataFrame, portal_left: pd.DataFrame, settings: ReconSettings) -> pd.DataFrame:
    """One-to-one probable pairs among unmatched documents.

    Candidates must share GSTIN and doc type, have invoice-number similarity
    ``>= settings.fuzzy_threshold`` and taxable value + total tax within tolerance.
    Pairs are assigned greedily by score (ties broken by the smaller date gap).

    Returns:
        Frame with ``_rid_books``, ``_rid_portal``, ``fuzzy_score``.
    """
    empty = pd.DataFrame({"_rid" + B: pd.Series(dtype=float), "_rid" + P: pd.Series(dtype=float),
                          "fuzzy_score": pd.Series(dtype=float)})
    if not settings.enable_fuzzy or books_left.empty or portal_left.empty:
        return empty
    thr = float(settings.fuzzy_threshold)
    block = max(int(settings.fuzzy_block_size), 1)
    p_groups = {k: g for k, g in portal_left.groupby(["gstin" + P, "doc_type" + P], sort=False)}
    parts: list[pd.DataFrame] = []
    for key, gb in books_left.groupby(["gstin" + B, "doc_type" + B], sort=False):
        gp = p_groups.get(key)
        if gp is None:
            continue
        p_inv = gp["inv_norm" + P].tolist()
        p_rid = gp["_rid" + P].to_numpy()
        p_tax = gp["taxable_value" + P].to_numpy(float)[None, :]
        p_tt = gp["total_tax" + P].to_numpy(float)[None, :]
        p_dt = gp["invoice_date" + P].to_numpy("datetime64[D]")[None, :]
        for start in range(0, len(gb), block):
            blk = gb.iloc[start:start + block]
            scores = process.cdist(blk["inv_norm" + B].tolist(), p_inv, scorer=fuzz.ratio,
                                   score_cutoff=thr, workers=-1)
            ok = (scores >= thr)
            ok &= within_tolerance(blk["taxable_value" + B].to_numpy(float)[:, None], p_tax, settings)
            ok &= within_tolerance(blk["total_tax" + B].to_numpy(float)[:, None], p_tt, settings)
            i, j = np.nonzero(ok)
            if len(i) == 0:
                continue
            b_dt = blk["invoice_date" + B].to_numpy("datetime64[D]")[:, None]
            gap = np.abs((b_dt - p_dt).astype("float64"))[i, j]
            parts.append(pd.DataFrame({
                "_rid" + B: blk["_rid" + B].to_numpy()[i], "_rid" + P: p_rid[j],
                "fuzzy_score": scores[i, j].astype(float).round(1), "_gap": np.nan_to_num(gap, nan=1e9),
            }))
    if not parts:
        return empty
    cand = pd.concat(parts, ignore_index=True).sort_values(["fuzzy_score", "_gap"], ascending=[False, True])
    used_b: set = set()
    used_p: set = set()
    keep = np.zeros(len(cand), dtype=bool)
    for n, (rb, rp) in enumerate(zip(cand["_rid" + B].to_numpy(), cand["_rid" + P].to_numpy())):
        if rb not in used_b and rp not in used_p:
            used_b.add(rb)
            used_p.add(rp)
            keep[n] = True
    return cand[keep].drop(columns="_gap").reset_index(drop=True)


# --------------------------------------------------------------------------- #
# Main entry point
# --------------------------------------------------------------------------- #
RECON_LEAD_COLS: tuple[str, ...] = (
    "match_id", "status", "reason", "review_decision", "remarks", "gstin", "party_name", "doc_type",
    "invoice_date", "month", "fy", "fuzzy_score", "cross_period", "date_diff_days",
)


def reconcile(books: pd.DataFrame, portal: pd.DataFrame, settings: ReconSettings | None = None,
              portal_name: str = "GSTR-2B", mode: str = "purchase") -> ReconResult:
    """Reconcile cleaned books against cleaned portal data.

    Args:
        books: Output of :func:`modules.cleaner.clean_dataset` for the register.
        portal: Output of :func:`modules.cleaner.clean_dataset` for GSTR-2B/2A/1.
        settings: Tolerances and fuzzy options (defaults from :mod:`config`).
        portal_name: Label used in statuses ("Missing in GSTR-2B").
        mode: "purchase" or "sales" (labels only).

    Returns:
        :class:`ReconResult`; ``result.recon`` holds one row per pair / orphan with
        ``<field>_books``, ``<field>_portal``, ``diff_<field>`` columns, ``status`` and ``reason``.
    """
    settings = settings or ReconSettings()
    books = books if books is not None and len(books.columns) else empty_clean_frame()
    portal = portal if portal is not None and len(portal.columns) else empty_clean_frame()
    bs, ps = _side(books, B), _side(portal, P)

    # Tier 1-3: same key (GSTIN + doc type + normalised invoice + duplicate sequence)
    keyed = bs.merge(ps, left_on="_key" + B, right_on="_key" + P, how="inner")
    keyed["_tier"] = "key"
    left_b = bs[~bs["_rid" + B].isin(keyed["_rid" + B])]
    left_p = ps[~ps["_rid" + P].isin(keyed["_rid" + P])]

    # Tier 4: fuzzy invoice numbers
    fz = fuzzy_candidates(left_b, left_p, settings)
    fuzzy = fz.merge(left_b, on="_rid" + B).merge(left_p, on="_rid" + P) if len(fz) else fz
    fuzzy["_tier"] = "fuzzy"
    only_b = left_b[~left_b["_rid" + B].isin(fz["_rid" + B])]
    only_p = left_p[~left_p["_rid" + P].isin(fz["_rid" + P])]

    # Tier 5-6: orphans
    orphan_b = pd.concat([only_b.reset_index(drop=True), _blank_side(len(only_b), P, ps)], axis=1)
    orphan_b["_tier"] = "books_only"
    orphan_p = pd.concat([_blank_side(len(only_p), B, bs), only_p.reset_index(drop=True)], axis=1)
    orphan_p["_tier"] = "portal_only"

    frames = [x for x in (keyed, fuzzy, orphan_b, orphan_p) if len(x)]
    if frames:
        combined = pd.concat(frames, ignore_index=True)
    else:
        combined = pd.concat([bs.head(0), ps.head(0)], axis=1)
        combined["_tier"] = pd.Series(dtype=object)
    if "fuzzy_score" not in combined:
        combined["fuzzy_score"] = np.nan
    f = _finalize(combined, portal_name)

    f["status"] = ""
    f["reason"] = ""
    _classify(f, f["_tier"] == "key", settings, portal_name)
    is_fuzzy = f["_tier"] == "fuzzy"
    f.loc[is_fuzzy, "status"] = STATUS_PROBABLE
    f.loc[is_fuzzy, "reason"] = ("Similar invoice no (score " + f.loc[is_fuzzy, "fuzzy_score"].astype(str)
                                 + "), amounts agree - review")
    f.loc[f["_tier"] == "books_only", "status"] = status_missing_portal(portal_name)
    f.loc[f["_tier"] == "books_only", "reason"] = f"Not found in {portal_name} - follow up with supplier"
    f.loc[f["_tier"] == "portal_only", "status"] = STATUS_MISSING_BOOKS
    f.loc[f["_tier"] == "portal_only", "reason"] = "Not recorded in books"
    f["review_decision"] = np.where(is_fuzzy, DECISION_PENDING, "")
    f["remarks"] = ""

    # Stable ordering: GSTIN, then date, then books invoice
    f = f.sort_values(["gstin", "invoice_date", "invoice_no" + B, "invoice_no" + P], kind="stable",
                      na_position="last").reset_index(drop=True)
    f["match_id"] = ["R%06d" % (i + 1) for i in range(len(f))]
    f = f.drop(columns=["_tier", "_key" + B, "_key" + P])
    lead = list(RECON_LEAD_COLS)
    f = f[lead + [c for c in f.columns if c not in lead]]
    return ReconResult(f, books, portal, settings, portal_name, mode)


# --------------------------------------------------------------------------- #
# Manual review
# --------------------------------------------------------------------------- #
def apply_review(recon: pd.DataFrame, decisions: pd.DataFrame | None, portal_name: str) -> pd.DataFrame:
    """Apply accept / reject decisions (and remarks) to a base reconciliation.

    Args:
        recon: Base ``ReconResult.recon`` (as produced by :func:`reconcile`).
        decisions: Frame with ``match_id``, ``decision`` and ``remarks``.  Remarks
            may be given for any row; decisions only affect Probable Matches.

    Accepted probable matches become *Manually Matched*; rejected ones are split
    into a *Missing in <portal>* row (``<id>-B``) and a *Missing in Books* row (``<id>-P``).
    """
    if decisions is None or decisions.empty:
        return recon
    d = decisions.drop_duplicates("match_id", keep="last").set_index("match_id")
    f = recon.copy()
    dec = f["match_id"].map(d["decision"]) if "decision" in d else pd.Series(np.nan, index=f.index)
    rem = f["match_id"].map(d["remarks"]) if "remarks" in d else pd.Series(np.nan, index=f.index)
    rem = rem.where(rem.notna() & (rem.astype(str).str.strip() != ""))
    f["remarks"] = rem.fillna(f["remarks"]).astype(object)

    prob = f["status"] == STATUS_PROBABLE
    has_dec = prob & dec.isin([DECISION_PENDING, DECISION_ACCEPT, DECISION_REJECT])
    f.loc[has_dec, "review_decision"] = dec[has_dec]
    # Audit columns: who made the current decision and when (from the review_decisions table).
    by = f["match_id"].map(d["updated_by"]) if "updated_by" in d else pd.Series(np.nan, index=f.index)
    at = f["match_id"].map(d["updated_at"]) if "updated_at" in d else pd.Series(np.nan, index=f.index)
    f["reviewed_by"] = by.where(has_dec | by.notna(), "").fillna("").astype(object)
    f["reviewed_at"] = pd.to_datetime(at, errors="coerce")
    accept = prob & (dec == DECISION_ACCEPT)
    f.loc[accept, "status"] = STATUS_MANUAL
    f.loc[accept, "reason"] = "Probable match accepted by user"
    reject = prob & (dec == DECISION_REJECT)
    if not reject.any():
        return f

    rej = f[reject]
    books_part, portal_part = rej.copy(), rej.copy()
    for c in SIDE_COLS:
        portal_part[c + B] = None
        books_part[c + P] = None
    books_part["status"] = status_missing_portal(portal_name)
    books_part["match_id"] = rej["match_id"] + "-B"
    portal_part["status"] = STATUS_MISSING_BOOKS
    portal_part["match_id"] = rej["match_id"] + "-P"
    split = _finalize(pd.concat([books_part, portal_part], ignore_index=True), portal_name)
    split["reason"] = "Probable match rejected by user"
    split["_order"] = split["match_id"].str[:-2]
    keep = f[~reject].copy()
    keep["_order"] = keep["match_id"]
    out = pd.concat([keep, split[keep.columns]], ignore_index=True)
    return out.sort_values(["_order", "match_id"], kind="stable").drop(columns="_order").reset_index(drop=True)


# --------------------------------------------------------------------------- #
# Summaries
# --------------------------------------------------------------------------- #
def _risk_columns(recon: pd.DataFrame, portal_name: str) -> pd.DataFrame:
    """Per-row helper columns used by the summaries."""
    miss_p = recon["status"] == status_missing_portal(portal_name)
    mism = recon["status"] == STATUS_MISMATCH
    shortfall = (recon["itc_books"] - recon["itc_portal"]).clip(lower=0)
    return pd.DataFrame({
        "matched": recon["status"].isin(MATCHED_STATUSES),
        "miss_p": miss_p,
        "miss_b": recon["status"] == STATUS_MISSING_BOOKS,
        "mism": mism,
        "prob": recon["status"] == STATUS_PROBABLE,
        "risk": np.where(miss_p, recon["itc_books"].clip(lower=0), 0.0) + np.where(mism, shortfall, 0.0),
    }, index=recon.index)


def summarize(recon: pd.DataFrame, portal_name: str) -> dict[str, float | int]:
    """Headline metrics for the dashboard and the Summary sheet (ITC values are net of credit notes)."""
    r = _risk_columns(recon, portal_name)
    itc_p_na = recon["itc_portal"].where(recon.get("itc_available" + P, pd.Series("", index=recon.index)) == "N", 0)
    return {
        "docs_books": int(recon["has_books"].sum()),
        "docs_portal": int(recon["has_portal"].sum()),
        "itc_books": round(float(recon["itc_books"].sum()), 2),
        "itc_portal": round(float(recon["itc_portal"].sum()), 2),
        "itc_portal_not_available": round(float(itc_p_na.sum()), 2),
        "taxable_books": round(float(recon["taxable_books"].sum()), 2),
        "taxable_portal": round(float(recon["taxable_portal"].sum()), 2),
        "matched_count": int(r["matched"].sum()),
        "matched_itc": round(float(recon.loc[r["matched"], "itc_portal"].sum()), 2),
        "exact_count": int((recon["status"] == STATUS_EXACT).sum()),
        "tolerance_count": int((recon["status"] == STATUS_TOLERANCE).sum()),
        "manual_count": int((recon["status"] == STATUS_MANUAL).sum()),
        "mismatch_count": int(r["mism"].sum()),
        "mismatch_itc_diff": round(float((recon.loc[r["mism"], "itc_books"] - recon.loc[r["mism"], "itc_portal"]).sum()), 2),
        "probable_count": int(r["prob"].sum()),
        "missing_portal_count": int(r["miss_p"].sum()),
        "missing_portal_itc": round(float(recon.loc[r["miss_p"], "itc_books"].sum()), 2),
        "missing_books_count": int(r["miss_b"].sum()),
        "missing_books_itc": round(float(recon.loc[r["miss_b"], "itc_portal"].sum()), 2),
        "itc_at_risk": round(float(r["risk"].sum()), 2),
        "cross_period_count": int(recon["cross_period"].sum()),
    }


def status_table(recon: pd.DataFrame) -> pd.DataFrame:
    """Count and value (books vs portal) per status."""
    order = [STATUS_EXACT, STATUS_TOLERANCE, STATUS_MANUAL, STATUS_MISMATCH, STATUS_PROBABLE]
    g = recon.groupby("status", sort=False).agg(
        documents=("match_id", "size"), taxable_books=("taxable_books", "sum"), itc_books=("itc_books", "sum"),
        taxable_portal=("taxable_portal", "sum"), itc_portal=("itc_portal", "sum"))
    g["itc_difference"] = g["itc_books"] - g["itc_portal"]
    rank = {s: i for i, s in enumerate(order)}
    g = g.reset_index()
    g["_r"] = g["status"].map(rank).fillna(len(order))
    return g.sort_values(["_r", "status"]).drop(columns="_r").round(2).reset_index(drop=True)


def doc_type_table(recon: pd.DataFrame) -> pd.DataFrame:
    """Invoices / credit notes / debit notes separately, with net ITC (credit notes negative)."""
    g = recon.groupby("doc_type").agg(
        documents_books=("has_books", "sum"), documents_portal=("has_portal", "sum"),
        itc_books=("itc_books", "sum"), itc_portal=("itc_portal", "sum"))
    g.loc["NET"] = g.sum()
    g["itc_difference"] = g["itc_books"] - g["itc_portal"]
    return g.reset_index().round(2)


def supplier_summary(recon: pd.DataFrame, portal_name: str) -> pd.DataFrame:
    """Supplier-wise ITC position, sorted by pending ITC (highest first)."""
    if recon.empty:
        return pd.DataFrame(columns=["gstin", "party_name", "docs_books", "docs_portal", "itc_books", "itc_portal",
                                     "matched_itc", "mismatch_count", "probable_count", "missing_portal_count",
                                     "missing_portal_itc", "missing_books_count", "missing_books_itc",
                                     "pending_itc", "difference"])
    r = _risk_columns(recon, portal_name)
    work = pd.DataFrame({
        "gstin": recon["gstin"],
        "party_name": recon["party_name"].where(recon["party_name"] != ""),
        "docs_books": recon["has_books"].astype(int),
        "docs_portal": recon["has_portal"].astype(int),
        "itc_books": recon["itc_books"],
        "itc_portal": recon["itc_portal"],
        "matched_itc": recon["itc_portal"].where(r["matched"], 0.0),
        "mismatch_count": r["mism"].astype(int),
        "probable_count": r["prob"].astype(int),
        "missing_portal_count": r["miss_p"].astype(int),
        "missing_portal_itc": recon["itc_books"].where(r["miss_p"], 0.0),
        "missing_books_count": r["miss_b"].astype(int),
        "missing_books_itc": recon["itc_portal"].where(r["miss_b"], 0.0),
        "pending_itc": r["risk"],
    })
    agg = {c: "sum" for c in work.columns if c not in ("gstin", "party_name")}
    agg["party_name"] = "first"
    g = work.groupby("gstin", sort=False).agg(agg).reset_index()
    g["party_name"] = g["party_name"].fillna("")
    g["difference"] = g["itc_books"] - g["itc_portal"]
    cols = ["gstin", "party_name", *[c for c in work.columns if c not in ("gstin", "party_name")], "difference"]
    return g[cols].round(2).sort_values(["pending_itc", "difference"], ascending=False).reset_index(drop=True)
