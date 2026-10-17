"""Session state, shared services and small widgets used by every page."""
from __future__ import annotations

import os
import traceback
from typing import Callable

import pandas as pd
import streamlit as st

from config import DB_URL, PARTY_LABELS, PORTAL_SHORT
from modules import ReconError
from modules.auth import AuthService, AuthUser
from modules.database import DECISION_COLUMNS, Database
from modules.gstr3b import compare_gstr3b
from modules.matcher import ReconResult
from modules.periods import add_pending_age, carry_forward_schedule, fy_months
from modules.reports import audit_sheet, carry_forward_sheet, gstr3b_sheet


# --------------------------------------------------------------------------- #
# Services
# --------------------------------------------------------------------------- #
@st.cache_resource
def _database(url: str) -> Database:
    return Database(url)


def get_db() -> Database:
    """Database for the configured URL (``GST_RECON_DB_URL`` is read at call time, so tests can isolate)."""
    return _database(os.environ.get("GST_RECON_DB_URL", DB_URL))


def get_auth() -> AuthService:
    return AuthService(get_db())


def empty_decisions() -> pd.DataFrame:
    return pd.DataFrame(columns=DECISION_COLUMNS)


def init_state() -> None:
    defaults = {"user": None, "result": None, "decisions": empty_decisions(), "run_id": None, "meta": {},
                "clean_reports": [], "samples": {"books": [], "portal": []}, "report_bytes": None, "pdf_bytes": None,
                "gstr3b": None, "smtp_password": "", "company_name": "", "company_gstin": "", "carry_info": ""}
    for key, value in defaults.items():
        st.session_state.setdefault(key, value)


def current_user() -> AuthUser | None:
    return st.session_state.get("user")


def username() -> str:
    user = current_user()
    return user.username if user else ""


def is_admin() -> bool:
    user = current_user()
    return bool(user and user.is_admin)


def guarded(page: Callable[[], None]) -> None:
    """Run a page, turning errors into friendly messages."""
    try:
        page()
    except ReconError as exc:
        st.error(str(exc))
    except Exception as exc:  # noqa: BLE001 - last line of defence for the UI
        st.error(f"Something went wrong: {exc}. Please check the input files; details below.")
        with st.expander("Technical details"):
            st.code(traceback.format_exc())


# --------------------------------------------------------------------------- #
# Current reconciliation
# --------------------------------------------------------------------------- #
def set_result(result: ReconResult | None, run_id: int | None, meta: dict, decisions: pd.DataFrame | None = None,
               clean_reports: list | None = None, gstr3b: pd.DataFrame | None = None) -> None:
    st.session_state.update(result=result, run_id=run_id, meta=meta or {},
                            decisions=decisions if decisions is not None else empty_decisions(),
                            clean_reports=clean_reports or [], gstr3b=gstr3b, report_bytes=None, pdf_bytes=None)


def base_result() -> ReconResult | None:
    return st.session_state["result"]


def run_months(res: ReconResult) -> list[str]:
    """Months covered by the loaded run (FY range if set, else months present in the data)."""
    meta = st.session_state["meta"]
    fy, start, end = meta.get("fy"), meta.get("from_month"), meta.get("to_month")
    if fy and start and end:
        return [m for m in fy_months(fy) if start <= m <= end]
    months = set(res.recon["month_books"]) | set(res.recon["period_portal"])
    months = sorted(m for m in months if isinstance(m, str) and len(m) == 7)
    if start and end:
        months = [m for m in months if start <= m <= end]
    return months


def view_result() -> ReconResult | None:
    """Current result with review decisions applied and months-pending computed."""
    base = base_result()
    if base is None:
        return None
    res = base.with_decisions(st.session_state["decisions"])
    months = run_months(res)
    if months:
        res.recon = add_pending_age(res.recon, months[-1])
    return res


def require_result() -> ReconResult | None:
    res = view_result()
    if res is None:
        st.info("No reconciliation loaded yet. Run one on the **Reconcile** page or load one from **History**.")
    return res


def short_name(res: ReconResult) -> str:
    return PORTAL_SHORT.get(res.portal_name, res.portal_name)


def tax_word(res: ReconResult) -> str:
    return "ITC" if res.mode == "purchase" else "Output tax"


def merge_decisions(new: pd.DataFrame) -> int:
    """Save edited decisions / remarks (with user + time in the audit trail) and refresh the session copy."""
    run_id = st.session_state["run_id"]
    new = new.copy()
    if run_id:
        changed = get_db().save_decisions(run_id, new, username())
        st.session_state["decisions"] = get_db().load_decisions(run_id)
    else:
        old = st.session_state["decisions"]
        new["updated_by"], new["updated_at"] = username(), pd.Timestamp.now()
        st.session_state["decisions"] = pd.concat([old[~old["match_id"].isin(new["match_id"])], new],
                                                  ignore_index=True)
        changed = len(new)
    st.session_state["report_bytes"] = None
    st.session_state["pdf_bytes"] = None
    return changed


def report_extras(res: ReconResult) -> dict:
    """Additional Excel sheets for the current run: GSTR-3B comparison, carry forward, audit trail."""
    extras: dict = {}
    months = run_months(res)
    if res.mode == "purchase":
        extras.update(gstr3b_sheet(compare_gstr3b(res, st.session_state.get("gstr3b"), months or None)))
    extras.update(carry_forward_sheet(carry_forward_schedule(res, months), short_name(res)))
    if st.session_state["run_id"]:
        extras.update(audit_sheet(get_db().load_audit(st.session_state["run_id"])))
    return extras


# --------------------------------------------------------------------------- #
# Widgets
# --------------------------------------------------------------------------- #
def money_config(df: pd.DataFrame, extra: dict | None = None) -> dict:
    """Column config: localized number format for money-like columns, dd-mm-yyyy for dates."""
    cfg: dict = {}
    for col in df.columns:
        if pd.api.types.is_float_dtype(df[col]):
            cfg[col] = st.column_config.NumberColumn(col, format="localized")
        elif pd.api.types.is_datetime64_any_dtype(df[col]):
            cfg[col] = st.column_config.DatetimeColumn(col, format="DD-MM-YYYY HH:mm") \
                if (df[col].dropna().dt.hour > 0).any() else st.column_config.DateColumn(col, format="DD-MM-YYYY")
    cfg.update(extra or {})
    return cfg


def show_table(df: pd.DataFrame, height: int | None = None, key: str | None = None) -> None:
    kwargs = {"height": height} if height else {}
    st.dataframe(df, hide_index=True, column_config=money_config(df), key=key, **kwargs)


def as_text(df: pd.DataFrame) -> pd.DataFrame:
    """Raw (uncleaned) frames can mix dates and text in a column; show them as text."""
    return df.astype(object).where(df.notna(), "").astype(str)


def csv_download(df: pd.DataFrame, label: str, file_name: str, key: str) -> None:
    st.download_button(label, df.to_csv(index=False).encode("utf-8-sig"), file_name=file_name, mime="text/csv",
                       key=key, icon=":material/download:")


def filter_bar(recon: pd.DataFrame, key: str, res: ReconResult, statuses: bool = True) -> pd.DataFrame:
    """One row of filters: supplier, GSTIN, status, month, financial year."""
    party = PARTY_LABELS[res.mode][1].split()[0]
    cols = st.columns(5 if statuses else 4)
    names = sorted(n for n in recon["party_name"].unique() if n)
    f_name = cols[0].multiselect(party, names, key=f"{key}_name", placeholder="All")
    f_gstin = cols[1].multiselect("GSTIN", sorted(recon["gstin"].unique()), key=f"{key}_gstin", placeholder="All")
    i = 2
    f_status: list = []
    if statuses:
        f_status = cols[2].multiselect("Status", sorted(recon["status"].unique()), key=f"{key}_status",
                                       placeholder="All")
        i = 3
    f_month = cols[i].multiselect("Month", sorted(m for m in recon["month"].unique() if m), key=f"{key}_month",
                                  placeholder="All")
    f_fy = cols[i + 1].multiselect("Financial year", sorted(f for f in recon["fy"].unique() if f), key=f"{key}_fy",
                                   placeholder="All")
    mask = pd.Series(True, index=recon.index)
    for values, col in ((f_name, "party_name"), (f_gstin, "gstin"), (f_status, "status"), (f_month, "month"),
                        (f_fy, "fy")):
        if values:
            mask &= recon[col].isin(values)
    return recon[mask]


def with_recon(res: ReconResult, recon: pd.DataFrame) -> ReconResult:
    """Same result with a filtered ``recon`` frame (for summaries of the filtered view)."""
    return ReconResult(recon, res.books, res.portal, res.settings, res.portal_name, res.mode, res.created_at)
