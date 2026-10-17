# GST Reconciliation

This tool reconciles your **Purchase Register** (Tally / ERP / Excel) with **GSTR-2B** or **GSTR-2A**, and
your **Sales Register** with **GSTR-1**. It compares ITC claimed in **GSTR-3B** with 2B and your books. It
follows missing invoices month by month until they appear, and helps you chase suppliers by e-mail.

It runs locally and is reachable only from this computer (localhost). The UI is Streamlit, and history
is stored in SQLite.

---

## 1. Setup

Requires **Python 3.11+**.

```powershell
cd gst_reconciliation
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

## 2. Start the app

* Double-click **`start_app.bat`**, or the **GST Reconciliation** shortcut on the Desktop. It starts the
  app from this folder and opens <http://localhost:8510> in your browser. Close its window to stop
  the app.
* Or, from inside `gst_reconciliation/`, run `.venv\Scripts\python.exe -m streamlit run app.py`.

**First start.** You are asked to create the **administrator account**; there is no default
password. After that, everyone signs in with their own account, and admins add users on the
**Admin** page.

**Localhost only.** `.streamlit/config.toml` makes the server listen on `127.0.0.1`, so other
computers cannot open the app. It is read from the folder the app starts in, which `start_app.bat`
takes care of.

To try the app, go to **Reconcile → Sample data: Excel register or Tally export → Load sample data →
Run reconciliation**.

## 3. Tests

```powershell
.venv\Scripts\python.exe -m pytest -v
```

There are **224 tests**:

| File | Covers |
|---|---|
| `test_cleaner.py` | GSTIN checksum, invoice / date / amount normalisation, duplicates, 1-lakh-row speed |
| `test_matcher.py` | every matching tier, tolerance rules, fuzzy matching, CN/DN netting, review decisions, summaries |
| `test_loader_reports.py` | loaders (Excel, CSV, portal Excel, JSON), Excel report, Indian ₹ formats, database, migration, audit trail |
| `test_periods.py` | FY / month range, carry-forward across runs, month-wise schedule |
| `test_tally.py` | Tally register import, purchase and sales |
| `test_gstr3b.py` | GSTR-3B parsing and the 3B vs 2B vs books comparison |
| `test_auth.py` | login, lockout, roles, password rules |
| `test_vendors_mailer.py` | vendor master, and SMTP sending against a real local test mail server |
| `test_pdf_reports.py` | PDF content, read back from the generated files |
| `test_app.py` | the running app: setup and login, every page, bulk accept with audit, 3B entry, reports, carry-forward between two runs, sending an e-mail |

---

## 4. Features

### Inputs
* **Registers.** Excel or CSV. Title rows above the table are detected automatically.
* **Tally exports.** A *Purchase Register / Sales Register → Export → Excel* file is recognised and
  converted automatically:
  * **Invoice number.** The supplier invoice number is used; if it is missing, the voucher number.
  * **Tax.** Ledger columns containing IGST / CGST / SGST / Cess are summed.
  * **Taxable value.** Gross Total − taxes − round off, so freight and packing ledgers are included.
  * **Voucher types.** In a purchase register, a Tally Debit Note (purchase return) is the supplier's
    credit note.
  * **Other rows.** `Dr` / `Cr` amounts, cancelled vouchers and the Grand Total row are handled.
* **GSTR-2B / 2A / GSTR-1.** The official JSON, the portal Excel (B2B, B2B-CDNR and amendment
  sheets), or any flat file. Several months can be loaded at once. Sections with no invoice-level
  counterpart in the books (ISD, IMPG, IMPGSEZ, ECO …) are listed as "not reconciled".
* **Column mapping.** Columns are auto-suggested; you can adjust them and save them as profiles.

### Cleaning and matching
The six tiers, in order:
1. Exact Match
2. Matched with Tolerance
3. Mismatch, with the difference per field and a reason
4. Probable Match (similar invoice number)
5. Missing in 2B
6. Missing in Books

Credit and debit notes are matched separately and netted.

**Tolerance rules.** The default is **₹ only (±₹10)**. The alternatives are ₹ AND %, % only, and
₹ OR % (lenient: 1% of ₹1 crore is ₹1 lakh). The rule used is shown on the Dashboard, in the Excel
Summary and in the PDFs.

### Financial year, months and carry forward
* On **Reconcile → Period**, pick a **financial year** and a **month range**. Books are selected by
  invoice month; portal documents by return period.
* **Carry forward (on by default).** Books documents still *Missing in 2B* at the end of the previous
  saved run are brought into the next run, matched against the new 2B, and carried again until they
  appear. "Previous" means the same company GSTIN, and a run ending before the new range. Each carried
  document shows where it came from (*Carried Forward From*) and its *Months Pending*.
* **Month-wise & Carry Forward page.** For each month: opening pending, new in books, reflected in 2B,
  and closing pending, as counts and ITC, with charts.

### GSTR-3B comparison (purchase)
You can enter the 3B figures three ways:
* upload the GSTR-3B **JSON** (one per month)
* upload an Excel/CSV table (Month, IGST, CGST, SGST, Cess)
* type them in

For each month, the page compares:
* **Books ITC**, by invoice month
* **2B ITC available**, by return period, excluding "ITC not available"
* **3B Table 4A(5) "All other ITC"**, the part of 3B that 2B's B2B and CDNR sections feed

It shows the differences per tax head and a cumulative 3B − 2B. Months where 3B exceeds 2B are
flagged **Excess claimed** (Sec. 16(2)(aa) / Rule 36(4)). The 3B figures are saved with the run and
added as a sheet in the Excel report.

### Review and audit trail
Accept or reject probable matches (bulk-accept by score) and add remarks to any document. Every
change is recorded with the **user, the time, and the old → new value**. The trail is shown on the
Review page and in the Excel sheet *Review Audit Trail*; the *Probable Matches* sheet shows
*Reviewed By / At*.

### Vendor master and follow-up e-mails
* **Vendor master.** GSTIN (validated), name, e-mails, phone, contact person, notes. Edit it in a
  table, import it from Excel/CSV, or fill it from the current reconciliation.
* **Supplier Follow-up.** A drafted e-mail per supplier listing the missing and mismatched invoices,
  addressed from the vendor master. You can:
  * **send it** through your mail server, with a CSV of the pending invoices attached
  * **copy the text**
  * **open it in your mail program**
  * download it as .txt
  * **send to all** suppliers who have an e-mail address, after confirming

  Every sent or failed e-mail is logged.
* **Mail server.** An admin configures it on **Admin → E-mail server** (host, port, STARTTLS / SSL,
  from address) and can send a test e-mail. **The SMTP password is never stored.** Set the environment
  variable `GST_RECON_SMTP_PASSWORD` before starting the app, or type the password when sending (it is
  kept only for that session).

### Reports
* **Excel.** The sheets are:
  * Summary
  * Matched, Matched with Tolerance, and Mismatched (differences in red)
  * Probable Matches
  * Missing in 2B and Missing in Books
  * Supplier-wise Summary
  * Duplicates and Invalid GSTINs
  * GSTR-3B vs 2B vs Books
  * Month-wise Carry Forward
  * Review Audit Trail

  Every sheet has frozen headers, filters and auto widths. The **Indian ₹ lakh/crore format** is
  correct for every sign and size; this was checked by opening the file in Excel. Remarks are always
  stored as text, never as formulas.
* **PDF.** The **Summary** (A4 portrait) and the **Supplier-wise** report (A4 landscape; optionally
  pending suppliers only), with the ₹ symbol, a repeated table header and page numbers.
* **Filtered reports.** An optional filter (supplier, GSTIN, status, month, FY) limits what goes into
  both.

### Other pages
* **Dashboard.** Filters, key figures, charts and the detailed table.
* **Data Quality.** Invalid GSTINs, duplicates, and excluded rows.
* **History.** Load earlier runs, with their decisions and 3B figures, filtered by FY. Only admins can
  delete runs.
* **Admin.** Users (add, reset password, role, activate or deactivate), the e-mail server, and the
  e-mail log.

### Security
* **Passwords.** Stored as salted PBKDF2-SHA256 hashes. They need 8+ characters with letters and
  digits, and 5 failed attempts lock the account for 15 minutes.
* **Admins.** The last active admin cannot be removed.
* **Network.** The app is reachable only from localhost.
* **Signing in again.** You stay signed in until you sign out or close or refresh the browser tab.
  After a refresh you sign in again.

---

## 5. Performance (1 lakh invoices)

| Step | Time* |
|---|---|
| Cleaning + matching | ~5 s |
| Excel report (≈92,000 matched rows, 10+ sheets, 15 MB) | ~21 s (was 72 s before the writer was replaced) |

\*Measured on the development laptop while its CPU was 40–70% busy with other programs; an idle
machine is faster.

The report's data rows are rendered column-wise with Arrow string operations and streamed into an
XlsxWriter workbook. XlsxWriter also writes the styles, widths, filters and highlighting.

## 6. Sample data (`python sample_data/generate_sample_data.py`)

| File | Purpose |
|---|---|
| `purchase_register.xlsx` / `tally_purchase_register.xlsx` | 53 purchase documents: plain register / the same as a Tally export |
| `gstr2b_apr2024.json`, `gstr2b_may2024.json`, `gstr2b_portal_format.xlsx` | GSTR-2B in official JSON (two months) and portal Excel |
| `gstr3b_apr2024.json`, `gstr3b_may2024.json` | GSTR-3B: April over-claims by ₹5,000, May under-claims by ₹1,200 |
| `sales_register.xlsx` / `tally_sales_register.xlsx`, `gstr1_may2024.json` | sales vs GSTR-1 |
| `templates/*.xlsx` | blank input templates |

With the default rule the purchase sample gives:

| Status | Count |
|---|---|
| Exact | 29 |
| Within tolerance | 5 |
| Mismatch | 7 |
| Probable | 4 |
| Missing in 2B | 8 |
| Missing in books | 5 |

ITC at risk is **₹39,360**. The Tally version gives identical results.

`REW/T600` differs by about 0.6% (₹1,500 / ₹270). It is a Mismatch under ₹ only and within tolerance
under the % rules.

## 7. Project layout

```
gst_reconciliation/
  app.py                  entry point: login + page router
  start_app.bat           starts the app from this folder and opens the browser
  .streamlit/config.toml  server settings (127.0.0.1 only)
  config.py               field specs, aliases, tolerances, statuses, formats
  modules/
    loader.py   cleaner.py   matcher.py   reports.py   database.py   formatting.py
    periods.py  (FY, month range, carry forward)      tally.py     (Tally register import)
    gstr3b.py   (3B vs 2B vs books)                   auth.py      (users, login)
    vendors.py  (vendor master)                       mailer.py    (SMTP, mailto)
    pdf_reports.py (PDF export)
  ui/           one module per page + state.py, charts.py, login.py
  sample_data/  templates/  tests/
  data/         gst_recon.db (created at runtime; upgraded automatically)
```

## 8. Configuration

* `config.py`: `ReconSettings` defaults, header aliases, Excel formats.
* `GST_RECON_DB_URL`: use another database, e.g. `sqlite:///D:/gst/recon.db`.
* `GST_RECON_SMTP_PASSWORD`: the SMTP password, if you don't want to type it in the app.

## 9. Limitations

* **Portal layouts.** The GSTR-2B / 2A / 1 / 3B readers follow the published portal layouts and are
  tested against layout-faithful sample files, not against real downloads. If a real file from your
  account is not read correctly, send a copy (with amounts changed) and the parser can be adjusted.
* **Tally.** Tested with the standard register export layout. Heavily customised exports may need the
  column mapping adjusted, and the mapping can be saved as a profile.
* **E-mail.** Sending is tested against a local test mail server. With your real provider (Office 365,
  Gmail, etc.) you may need an app password or SMTP AUTH enabled.
