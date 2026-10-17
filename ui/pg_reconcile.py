"""Page: upload files, map columns, choose FY / months, carry forward and run."""
from __future__ import annotations

import hashlib
from datetime import date

import pandas as pd
import streamlit as st

from config import FIELD_SPECS, MODES, PARTY_LABELS, SAMPLE_DIR, TOLERANCE_MODES, ReconSettings
from modules import ReconError
from modules.cleaner import clean_dataset
from modules.loader import LoadedFile, load_file, missing_required, suggest_mapping, to_canonical
from modules.matcher import reconcile
from modules.periods import (
    add_carried, filter_books, filter_portal, fy_months, fy_of_month, month_label, pending_documents, range_label,
)
from ui.state import as_text, base_result, get_db, set_result, username

NOT_MAPPED = "— not mapped —"
AUTO_FY = "Auto - all months in the files"
SAMPLES = {
    ("purchase", "Excel register"): (["purchase_register.xlsx"], ["gstr2b_apr2024.json", "gstr2b_may2024.json"]),
    ("purchase", "Tally export"): (["tally_purchase_register.xlsx"], ["gstr2b_apr2024.json", "gstr2b_may2024.json"]),
    ("sales", "Excel register"): (["sales_register.xlsx"], ["gstr1_may2024.json"]),
    ("sales", "Tally export"): (["tally_sales_register.xlsx"], ["gstr1_may2024.json"]),
}


@st.cache_data(show_spinner=False, max_entries=32)
def cached_load(data: bytes, name: str, dataset: str, sheet: str | None, mode: str) -> LoadedFile:
    return load_file(data, name=name, dataset=dataset, sheet=sheet, mode=mode)


def gather_files(uploads: list, dataset: str) -> list[tuple[str, bytes]]:
    files = [(u.name, u.getvalue()) for u in uploads or []]
    return files + st.session_state["samples"][dataset]


def load_sample(mode: str, kind: str) -> None:
    books, portal = SAMPLES[(mode, kind)]
    st.session_state["samples"] = {"books": [(f, (SAMPLE_DIR / f).read_bytes()) for f in books],
                                   "portal": [(f, (SAMPLE_DIR / f).read_bytes()) for f in portal]}


def mapping_editor(loaded: LoadedFile, dataset: str, prefix: str, mode: str) -> dict[str, str | None]:
    """Column-mapping widgets for one tabular file; returns the chosen mapping."""
    db = get_db()
    columns = [c for c in loaded.frame.columns if not str(c).startswith("__")]
    options = [NOT_MAPPED] + columns
    suggestion = suggest_mapping(columns)
    keys = {spec.key: f"{prefix}_map_{spec.key}" for spec in FIELD_SPECS}
    for field_key, widget_key in keys.items():
        if widget_key not in st.session_state or st.session_state[widget_key] not in options:
            st.session_state[widget_key] = suggestion.get(field_key) or NOT_MAPPED

    def current() -> dict[str, str | None]:
        return {k: (None if st.session_state[w] == NOT_MAPPED else st.session_state[w]) for k, w in keys.items()}

    missing = missing_required(current())
    title = f"Column mapping — {loaded.name}" + (f" [{loaded.sheet}]" if loaded.sheet else "")
    with st.expander(title + (" ⚠️ action needed" if missing else " ✅"), expanded=bool(missing)):
        if loaded.meta.get("tally"):
            st.info("**Tally register export detected** - columns were converted automatically:\n\n- "
                    + "\n- ".join(loaded.notes), icon=":material/auto_fix_high:")
        profiles = db.list_mappings(dataset)
        if profiles:
            c1, c2 = st.columns([3, 1], vertical_alignment="bottom")
            chosen = c1.selectbox("Saved mapping profile", profiles, key=f"{prefix}_profile")

            def apply_profile() -> None:
                saved = db.load_mapping(chosen)
                for fk, wk in keys.items():
                    st.session_state[wk] = saved.get(fk) if saved.get(fk) in options else NOT_MAPPED

            c2.button("Apply profile", key=f"{prefix}_apply", on_click=apply_profile)
        gstin_label, name_label = PARTY_LABELS[mode]
        grid = st.columns(3)
        for i, spec in enumerate(FIELD_SPECS):
            label = {"gstin": gstin_label, "party_name": name_label}.get(spec.key, spec.label)
            grid[i % 3].selectbox(label + (" *" if spec.required else ""), options, key=keys[spec.key],
                                  help=spec.help or None)
        missing = missing_required(current())
        if missing:
            st.warning("Required fields not mapped: " + ", ".join(missing))
        c1, c2 = st.columns([3, 1], vertical_alignment="bottom")
        name = c1.text_input("Save this mapping as", key=f"{prefix}_save_name", placeholder="e.g. Tally purchase export")
        if c2.button("Save profile", key=f"{prefix}_save"):
            db.save_mapping(name, dataset, current())
            st.success(f"Mapping '{name.strip()}' saved.")
        st.caption("Preview (first 5 rows)")
        st.dataframe(as_text(loaded.frame.drop(columns="__source_row__", errors="ignore").head(5)), hide_index=True)
    return current()


def file_block(files: list[tuple[str, bytes]], dataset: str, mode: str) -> list[pd.DataFrame | None]:
    """Load, show and map every file of one dataset; returns canonical frames (None = mapping incomplete)."""
    frames: list[pd.DataFrame | None] = []
    for idx, (name, data) in enumerate(files):
        prefix = f"{dataset}_{idx}_{hashlib.md5(data).hexdigest()[:8]}"
        loaded = cached_load(data, name, dataset, None, mode)
        if loaded.kind == "table" and len(loaded.sheets) > 1:
            sheet = st.selectbox(f"Sheet in {name}", loaded.sheets, key=f"{prefix}_sheet")
            if sheet != loaded.sheet:
                loaded = cached_load(data, name, dataset, sheet, mode)
        if loaded.canonical:
            st.success(f"**{name}** — " + "; ".join(loaded.notes), icon=":material/task_alt:")
            frames.append(to_canonical(loaded))
        else:
            mapping = mapping_editor(loaded, dataset, prefix, mode)
            frames.append(None if missing_required(mapping) else to_canonical(loaded, mapping))
    return frames


def settings_panel() -> ReconSettings:
    with st.expander("Matching settings", expanded=False):
        c1, c2, c3 = st.columns(3)
        mode = c1.selectbox("Tolerance rule", list(TOLERANCE_MODES), format_func=TOLERANCE_MODES.get,
                            help="₹ only is the default: a % rule is lenient on large invoices "
                                 "(1% of ₹1 crore is ₹1 lakh).")
        abs_tol = c2.number_input("Amount tolerance (₹)", 0.0, 100000.0, 10.0, 1.0, disabled=mode == "percentage",
                                  help="Per amount field (taxable value, each tax head).")
        pct_tol = c3.number_input("Percentage tolerance (%)", 0.0, 100.0, 1.0, 0.1, disabled=mode == "absolute",
                                  help="Used only by the '% only', '₹ AND %' and '₹ OR %' rules.")
        c4, c5, c6 = st.columns(3)
        date_tol = c4.number_input("Date tolerance (days)", 0, 365, 0, 1,
                                   help="Allowed date gap for 'Matched with Tolerance'. 0 = dates must match.")
        fuzzy_on = c5.toggle("Fuzzy invoice-number matching", value=True)
        threshold = c6.slider("Fuzzy similarity threshold", 60, 100, 85, disabled=not fuzzy_on)
    return ReconSettings(abs_tolerance=abs_tol, pct_tolerance=pct_tol, tolerance_mode=mode,
                         date_tolerance_days=int(date_tol), fuzzy_threshold=float(threshold), enable_fuzzy=fuzzy_on)


def fy_choices() -> list[str]:
    today = date.today()
    current = today.year if today.month >= 4 else today.year - 1
    return [AUTO_FY] + [f"{y}-{(y + 1) % 100:02d}" for y in range(current + 1, current - 9, -1)]


def period_panel(mode: str, portal_name: str) -> tuple[str | None, str | None, str | None, bool]:
    """FY + month range + carry-forward option.  Returns ``(fy, from_month, to_month, carry)``."""
    c1, c2 = st.columns([1, 2])
    fy = c1.selectbox("Financial year", fy_choices(), key="rec_fy",
                      help="Books are selected by invoice month, portal documents by return period.")
    start = end = None
    if fy != AUTO_FY:
        months = fy_months(fy)
        start, end = c2.select_slider("Months", options=months, value=(months[0], months[-1]),
                                      format_func=month_label, key=f"rec_months_{fy}")
    else:
        c2.caption("All invoice months in the books and all return periods in the portal files are included.")
    carry = st.checkbox(f"Carry forward documents still missing in {portal_name} from the previous run",
                        value=True, key="rec_carry",
                        help="Pulls in books documents that were 'Missing in the portal' at the end of the latest "
                             "saved run (same company GSTIN, ending before this period) and keeps carrying them "
                             "until they appear.")
    if carry and start:
        prev = get_db().find_previous_run(mode, portal_name, st.session_state["company_gstin"], start)
        if prev:
            runs = get_db().list_runs().set_index("run_id")
            st.caption(f"Will carry forward from run #{prev} · {runs.loc[prev, 'period'] or '-'}")
        else:
            st.caption("No earlier saved run found for this company GSTIN - nothing to carry forward.")
    return (None if fy == AUTO_FY else fy), start, end, carry


def run_reconciliation(books_frames, portal_frames, settings, mode, portal_name, fy, start, end, carry,
                       period_label, files_b, files_p) -> None:
    db = get_db()
    with st.status("Reconciling…", expanded=True) as status:
        st.write("Cleaning books…")
        books, rep_b = clean_dataset(pd.concat(books_frames, ignore_index=True), "books")
        st.write(f"Cleaning {portal_name}…")
        portal, rep_p = clean_dataset(pd.concat(portal_frames, ignore_index=True), "portal")
        if books.empty and portal.empty:
            raise ReconError("No usable rows found in either dataset after cleaning.")
        if start and end:
            books, portal = filter_books(books, start, end), filter_portal(portal, start, end)
            if books.empty and portal.empty:
                raise ReconError(f"No documents fall in {range_label(start, end)}. Check the financial year / months.")
        else:
            months = sorted(set(m for m in books["month"] if m) | set(m for m in portal["period"] if m))
            if months:
                start, end = months[0], months[-1]
                fy = fy_of_month(start)
        carry_info = ""
        if carry and start:
            prev_id = db.find_previous_run(mode, portal_name, st.session_state["company_gstin"], start)
            if prev_id:
                prev, prev_dec, prev_meta = db.load_run(prev_id)
                pending = pending_documents(prev.with_decisions(prev_dec), prev_meta["period_label"] or f"Run #{prev_id}")
                books, n = add_carried(books, pending)
                carry_info = f"{n} document(s) carried forward from run #{prev_id} ({prev_meta['period_label']})"
                st.write(carry_info)
        st.write("Matching…")
        result = reconcile(books, portal, settings, portal_name=portal_name, mode=mode)
        label = period_label.strip() or (range_label(start, end) if start else "")
        run_id = db.save_run(result, label, st.session_state["company_name"], st.session_state["company_gstin"],
                             files_b, files_p, fy=fy or "", from_month=start or "", to_month=end or "",
                             created_by=username())
        set_result(result, run_id, {"period_label": label, "fy": fy or "", "from_month": start or "",
                                    "to_month": end or "", "company_name": st.session_state["company_name"]},
                   clean_reports=[rep_b, rep_p])
        if carry_info:
            found = result.recon[(result.recon["carried_from_books"] != "") & result.recon["has_portal"]]
            carry_info += f"; {len(found)} of them now appear in {portal_name}."
        st.session_state["carry_info"] = carry_info
        status.update(label=f"Done - run #{run_id} saved", state="complete", expanded=False)


def render() -> None:
    st.header("Reconcile")
    mode_label = st.selectbox("Reconciliation type", list(MODES), key="mode_label")
    cfg = MODES[mode_label]
    mode, portal_name = cfg["mode"], cfg["portal"]
    books_word = "Purchase Register" if mode == "purchase" else "Sales Register"

    c1, c2, c3 = st.columns([1, 1, 2], vertical_alignment="bottom")
    kind = c1.selectbox("Sample data", ["Excel register", "Tally export"], key="sample_kind")
    c2.button("Load sample data", on_click=load_sample, args=(mode, kind), icon=":material/science:",
              help="Dummy files from sample_data/ that cover every match category.")
    if any(st.session_state["samples"].values()):
        names = [n for n, _ in st.session_state["samples"]["books"] + st.session_state["samples"]["portal"]]
        c3.caption("Sample files loaded: " + ", ".join(names))
        if c3.button("Clear sample files", type="tertiary"):
            st.session_state["samples"] = {"books": [], "portal": []}
            st.rerun()

    left, right = st.columns(2)
    with left:
        st.subheader(f"1 · {books_word}")
        up_books = st.file_uploader(f"{books_word} - Excel / CSV / Tally export", type=["xlsx", "xls", "xlsm", "csv"],
                                    accept_multiple_files=True, key="up_books")
        files_b = gather_files(up_books, "books")
        books_frames = file_block(files_b, "books", mode)
    with right:
        st.subheader(f"2 · {portal_name}")
        up_portal = st.file_uploader(f"{portal_name} (portal JSON / Excel / CSV) - several months allowed",
                                     type=["json", "xlsx", "xls", "xlsm", "csv"], accept_multiple_files=True,
                                     key="up_portal")
        files_p = gather_files(up_portal, "portal")
        portal_frames = file_block(files_p, "portal", mode)

    st.subheader("3 · Period")
    fy, start, end, carry = period_panel(mode, portal_name)
    st.subheader("4 · Settings & run")
    settings = settings_panel()
    period_label = st.text_input("Period label (optional)", placeholder="Auto from the months selected")

    ready = bool(books_frames) and bool(portal_frames) and all(f is not None for f in books_frames + portal_frames)
    if not ready:
        st.caption("Upload both datasets and complete the column mapping to enable reconciliation.")
    if st.button("Run reconciliation", type="primary", disabled=not ready, icon=":material/play_arrow:"):
        run_reconciliation(books_frames, portal_frames, settings, mode, portal_name, fy, start, end, carry,
                           period_label, [n for n, _ in files_b], [n for n, _ in files_p])

    res = base_result()
    if res is not None and st.session_state["clean_reports"]:
        rep_b, rep_p = st.session_state["clean_reports"]
        c1, c2 = st.columns(2)
        c1.info("**Books:** " + " · ".join(rep_b.messages()))
        c2.info(f"**{res.portal_name}:** " + " · ".join(rep_p.messages()))
        if st.session_state.get("carry_info"):
            st.info("**Carry forward:** " + st.session_state["carry_info"], icon=":material/redo:")
        counts = res.recon["status"].value_counts()
        st.success("Reconciliation complete: " + " · ".join(f"{k}: {v:,}" for k, v in counts.items())
                   + ". Open the **Dashboard** for details.")
