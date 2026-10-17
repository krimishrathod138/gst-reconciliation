"""Page: vendor master - supplier name, GSTIN, e-mail, phone, contact person."""
from __future__ import annotations

import streamlit as st

from modules.loader import load_file
from modules.vendors import VENDOR_FIELDS, map_vendor_columns, validate_vendors, vendors_from_result
from ui.state import base_result, csv_download, get_db, show_table, username

LABELS = {"gstin": "GSTIN", "name": "Name", "email": "E-mail(s)", "phone": "Phone", "contact_person": "Contact person",
          "notes": "Notes"}


def render() -> None:
    db = get_db()
    st.header("Vendor master")
    st.caption("Used for supplier follow-up e-mails. Several e-mail addresses can be separated by commas. "
               "GSTINs are validated (checksum).")
    current = db.list_vendors()
    edit = current[list(VENDOR_FIELDS)].rename(columns=LABELS)
    edited = st.data_editor(edit, hide_index=True, num_rows="dynamic", key="vendor_editor",
                            column_config={"GSTIN": st.column_config.TextColumn("GSTIN", max_chars=15, required=True),
                                           "E-mail(s)": st.column_config.TextColumn("E-mail(s)", width="large")})
    c1, c2, c3 = st.columns(3)
    if c1.button("Save changes", type="primary", icon=":material/save:"):
        rows = edited.rename(columns={v: k for k, v in LABELS.items()})
        valid, rejected = validate_vendors(rows)
        removed = sorted(set(current["gstin"]) - set(valid["gstin"]) - set(rejected["gstin"]))
        added, updated = db.upsert_vendors(valid, username())
        deleted = db.delete_vendors(removed) if removed else 0
        if len(rejected):
            st.error(f"{len(rejected)} row(s) not saved:")
            show_table(rejected.rename(columns={**LABELS, "error": "Problem"}), key="vendor_rejected")
        st.success(f"Saved: {added} added, {updated} updated, {deleted} removed.")
    res = base_result()
    if c2.button("Add suppliers from current reconciliation", disabled=res is None,
                 help="Adds GSTIN + name for every valid GSTIN in the loaded run; existing vendors are not changed."):
        found = vendors_from_result(res.recon)
        new = found[~found["gstin"].isin(current["gstin"])]
        added, _ = db.upsert_vendors(new, username())
        st.success(f"{added} new vendor(s) added - fill in their e-mail addresses above.")
        st.rerun()
    with c3:
        csv_download(current.rename(columns=LABELS), "Download vendor list (CSV)", "vendor_master.csv", "dl_vendors")

    st.subheader("Import from Excel / CSV")
    st.caption("Columns such as GSTIN, Supplier Name, Email, Mobile, Contact Person are recognised automatically.")
    up = st.file_uploader("Vendor list", type=["xlsx", "xls", "csv"], key="vendor_upload")
    if up is not None:
        raw = load_file(up.getvalue(), name=up.name, dataset="vendors").frame.drop(columns="__source_row__",
                                                                                  errors="ignore")
        valid, rejected = validate_vendors(map_vendor_columns(raw))
        st.write(f"{len(valid)} valid row(s), {len(rejected)} with problems.")
        show_table(valid.rename(columns=LABELS), height=240, key="vendor_import_valid")
        if len(rejected):
            show_table(rejected.rename(columns={**LABELS, "error": "Problem"}), height=200, key="vendor_import_bad")
        if st.button(f"Import {len(valid)} vendor(s)", disabled=valid.empty):
            added, updated = db.upsert_vendors(valid, username())
            st.success(f"Imported: {added} added, {updated} updated.")
