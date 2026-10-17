"""
Central configuration for the GST Reconciliation tool.

Everything that a user or developer may want to tune lives here:
canonical field definitions (with header aliases used for auto-mapping),
default matching tolerances, status labels, state codes and report formats.
"""
from __future__ import annotations

import os
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #
BASE_DIR: Path = Path(__file__).resolve().parent
DATA_DIR: Path = BASE_DIR / "data"
SAMPLE_DIR: Path = BASE_DIR / "sample_data"
TEMPLATE_DIR: Path = BASE_DIR / "templates"
DB_PATH: Path = DATA_DIR / "gst_recon.db"
DB_URL: str = os.environ.get("GST_RECON_DB_URL", f"sqlite:///{DB_PATH.as_posix()}")


def normalise_header(text: Any) -> str:
    """Reduce a column header to lowercase alphanumerics for alias comparison.

    >>> normalise_header("Taxable Value (₹)")
    'taxablevalue'
    """
    return re.sub(r"[^a-z0-9]", "", str(text).lower())


# --------------------------------------------------------------------------- #
# Canonical fields
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class FieldSpec:
    """Definition of one canonical input field."""

    key: str
    label: str
    required: bool = False
    numeric: bool = False
    aliases: tuple[str, ...] = ()
    help: str = ""

    @property
    def norm_aliases(self) -> tuple[str, ...]:
        """Aliases (plus label) reduced with :func:`normalise_header`."""
        return tuple(dict.fromkeys(normalise_header(a) for a in (self.label, *self.aliases)))


FIELD_SPECS: tuple[FieldSpec, ...] = (
    FieldSpec(
        "gstin", "Supplier GSTIN", required=True,
        aliases=("GSTIN of supplier", "GSTIN", "GSTIN/UIN", "Party GSTIN", "Supplier GSTIN/UIN",
                 "GSTIN No", "GSTIN Number", "CTIN", "Customer GSTIN", "Recipient GSTIN",
                 "GSTIN/UIN of Recipient", "Buyer GSTIN", "Vendor GSTIN"),
        help="Counter-party GSTIN (supplier for purchases, customer for sales).",
    ),
    FieldSpec(
        "party_name", "Supplier Name",
        aliases=("Trade/Legal name", "Trade/Legal name of the Supplier", "Trade Name", "Legal Name",
                 "Party Name", "Name of Supplier", "Vendor Name", "Customer Name", "Receiver Name",
                 "Party", "Particulars", "Ledger Name", "Name"),
    ),
    FieldSpec(
        "invoice_no", "Invoice No", required=True,
        aliases=("Invoice Number", "Inv No", "Invoice No.", "Bill No", "Bill Number", "Document No",
                 "Document Number", "Doc No", "Note Number", "Note No", "Credit/Debit Note No",
                 "Supplier Invoice No", "Supplier Invoice Number", "Invoice/Note No", "Reference No",
                 "Ref No", "Voucher Ref No"),
    ),
    FieldSpec(
        "invoice_date", "Invoice Date", required=True,
        aliases=("Inv Date", "Bill Date", "Document Date", "Doc Date", "Note Date", "Invoice/Note Date",
                 "Supplier Invoice Date", "Reference Date", "Date"),
    ),
    FieldSpec(
        "invoice_type", "Invoice Type",
        aliases=("Type", "Document Type", "Doc Type", "Voucher Type", "Transaction Type", "Section"),
        help="B2B / CDNR / Credit Note / Debit Note. Blank = B2B invoice.",
    ),
    FieldSpec(
        "note_type", "Note Type",
        aliases=("Note Type (Credit/Debit)", "Credit/Debit", "CDN Type", "Note Type C/D"),
        help="Optional: C / D (Credit / Debit note). Overrides Invoice Type when present.",
    ),
    FieldSpec(
        "taxable_value", "Taxable Value", required=True, numeric=True,
        aliases=("Taxable Amount", "Taxable Amt", "Taxable Val", "Assessable Value", "Taxable"),
    ),
    FieldSpec("igst", "IGST", numeric=True,
              aliases=("Integrated Tax", "IGST Amount", "IGST Amt", "Integrated Tax Amount", "IGST Value")),
    FieldSpec("cgst", "CGST", numeric=True,
              aliases=("Central Tax", "CGST Amount", "CGST Amt", "Central Tax Amount", "CGST Value")),
    FieldSpec("sgst", "SGST", numeric=True,
              aliases=("State/UT Tax", "SGST/UTGST", "State Tax", "UTGST", "SGST Amount", "SGST Amt",
                       "State/UT Tax Amount", "SGST Value", "SGST/UTGST Amount")),
    FieldSpec("cess", "Cess", numeric=True, aliases=("Cess Amount", "Cess Amt", "CESS Value")),
    FieldSpec(
        "invoice_value", "Total Invoice Value", numeric=True,
        aliases=("Invoice Value", "Total Value", "Note Value", "Invoice Amount", "Bill Amount",
                 "Total Amount", "Gross Amount", "Gross Total", "Document Value"),
        help="If not mapped it is computed as taxable value + taxes.",
    ),
    FieldSpec("place_of_supply", "Place of Supply",
              aliases=("POS", "Place Of Supply (Name of State/UT)", "Place of Supply (State)", "State")),
    FieldSpec(
        "return_period", "Return Period",
        aliases=("GSTR-1/IFF/GSTR-5 Period", "GSTR-1/IFF/GSTR-1A/5 Filing Period", "GSTR-1/5 Period",
                 "Filing Period", "Tax Period", "Period", "Month"),
        help="Optional: month in which the document appears in the return (multi-month recon).",
    ),
    FieldSpec("itc_available", "ITC Availability",
              aliases=("ITC Available", "Eligible for ITC", "ITC Eligible", "ITC Availability (Yes/No)")),
)

FIELD_BY_KEY: dict[str, FieldSpec] = {f.key: f for f in FIELD_SPECS}
REQUIRED_FIELDS: tuple[str, ...] = tuple(f.key for f in FIELD_SPECS if f.required)
AMOUNT_FIELDS: tuple[str, ...] = ("taxable_value", "igst", "cgst", "sgst", "cess", "invoice_value")
TAX_FIELDS: tuple[str, ...] = ("igst", "cgst", "sgst", "cess")

# Labels that change with reconciliation mode (purchase vs sales).
PARTY_LABELS: dict[str, tuple[str, str]] = {
    "purchase": ("Supplier GSTIN", "Supplier Name"),
    "sales": ("Customer GSTIN", "Customer Name"),
}

# --------------------------------------------------------------------------- #
# Document types
# --------------------------------------------------------------------------- #
DOC_INVOICE = "INV"
DOC_CREDIT = "CN"
DOC_DEBIT = "DN"
DOC_LABELS: dict[str, str] = {DOC_INVOICE: "Invoice", DOC_CREDIT: "Credit Note", DOC_DEBIT: "Debit Note"}

# --------------------------------------------------------------------------- #
# Reconciliation modes / portal names
# --------------------------------------------------------------------------- #
MODES: dict[str, dict[str, str]] = {
    "Purchase Register vs GSTR-2B": {"mode": "purchase", "portal": "GSTR-2B", "short": "2B"},
    "Purchase Register vs GSTR-2A": {"mode": "purchase", "portal": "GSTR-2A", "short": "2A"},
    "Sales Register vs GSTR-1": {"mode": "sales", "portal": "GSTR-1", "short": "GSTR-1"},
}
PORTAL_SHORT: dict[str, str] = {v["portal"]: v["short"] for v in MODES.values()}

# --------------------------------------------------------------------------- #
# Match statuses
# --------------------------------------------------------------------------- #
STATUS_EXACT = "Exact Match"
STATUS_TOLERANCE = "Matched with Tolerance"
STATUS_MISMATCH = "Mismatch"
STATUS_PROBABLE = "Probable Match"
STATUS_MANUAL = "Manually Matched"
STATUS_MISSING_BOOKS = "Missing in Books"


def status_missing_portal(portal_name: str) -> str:
    """Status label for documents in books but absent from the portal data."""
    return f"Missing in {portal_name}"


MATCHED_STATUSES: tuple[str, ...] = (STATUS_EXACT, STATUS_TOLERANCE, STATUS_MANUAL)

DECISION_PENDING = "Pending"
DECISION_ACCEPT = "Accepted"
DECISION_REJECT = "Rejected"
DECISIONS: tuple[str, ...] = (DECISION_PENDING, DECISION_ACCEPT, DECISION_REJECT)

# Dashboard chart colours (validated categorical slots 1-2; the entity keeps its colour on every chart).
CHART_BOOKS_COLOR = "#2a78d6"    # Books  - blue
CHART_PORTAL_COLOR = "#eb6834"   # Portal - orange
CHART_3B_COLOR = "#1baf7a"       # GSTR-3B - aqua (slot 3; slots 1-3 validated all-pairs)
CHART_SINGLE_COLOR = "#2a78d6"   # single-series charts
CHART_GRID_COLOR = "rgba(137,135,129,0.25)"


# --------------------------------------------------------------------------- #
# Matching settings
# --------------------------------------------------------------------------- #
TOLERANCE_MODES: dict[str, str] = {
    "absolute": "₹ only (default)",
    "both": "₹ AND % (strictest)",
    "percentage": "% only",
    "either": "₹ OR % (lenient on large invoices)",
}


@dataclass
class ReconSettings:
    """User-tunable reconciliation parameters.

    The default rule is a flat ±₹10 per amount field: a percentage rule is too
    lenient on large invoices (1% of ₹1 crore is ₹1 lakh).
    """

    abs_tolerance: float = 10.0          # rupees, per amount field
    pct_tolerance: float = 1.0           # percent of the larger value (used by the %, both and either modes)
    tolerance_mode: str = "absolute"     # key of TOLERANCE_MODES
    date_tolerance_days: int = 0         # allowed date gap for "Matched with Tolerance"
    fuzzy_threshold: float = 85.0        # rapidfuzz ratio (0-100) for Probable Match
    enable_fuzzy: bool = True
    fuzzy_block_size: int = 2_000        # rows per cdist block (memory guard)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "ReconSettings":
        data = data or {}
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**known)


def describe_tolerance(settings: ReconSettings) -> str:
    """Plain-language statement of the tolerance rule a run used, e.g. 'Within ±₹10 per amount field (₹ only)'."""
    rupees, pct = f"±₹{settings.abs_tolerance:g}", f"±{settings.pct_tolerance:g}%"
    rule = {
        "absolute": f"Within {rupees} per amount field",
        "percentage": f"Within {pct} per amount field",
        "both": f"Within {rupees} and {pct} per amount field",
        "either": f"Within {rupees} or {pct} per amount field",
    }.get(settings.tolerance_mode, f"{rupees} / {pct} ({settings.tolerance_mode})")
    label = {"absolute": "₹ only", "percentage": "% only", "both": "₹ AND %", "either": "₹ OR %"}.get(
        settings.tolerance_mode, settings.tolerance_mode)
    days = settings.date_tolerance_days
    date_part = "dates must match" if days == 0 else f"dates within {days} day(s)"
    return f"{rule} ({label}); {date_part}"


DEFAULT_SETTINGS = ReconSettings()

# --------------------------------------------------------------------------- #
# Cleaning rules
# --------------------------------------------------------------------------- #
# Tried in order; first successful parse wins. Day-first formats come first (Indian convention).
DATE_FORMATS: tuple[str, ...] = (
    "%d-%m-%Y", "%d/%m/%Y", "%d.%m.%Y", "%d-%m-%y", "%d/%m/%y", "%d.%m.%y",
    "%Y-%m-%d", "%Y/%m/%d", "%Y-%m-%d %H:%M:%S", "%d-%m-%Y %H:%M:%S", "%d/%m/%Y %H:%M:%S",
    "%d-%b-%Y", "%d-%b-%y", "%d %b %Y", "%d %B %Y", "%d-%B-%Y", "%b %d, %Y", "%B %d, %Y", "%Y%m%d",
)
PERIOD_FORMATS: tuple[str, ...] = (
    "%b'%y", "%b-%Y", "%b %Y", "%b-%y", "%B-%Y", "%B %Y", "%B'%y", "%b%Y", "%B, %Y", "%b, %Y",
)
FY_YEAR_RANGE: tuple[int, int] = (2000, 2050)
DISPLAY_DATE_FORMAT = "%d-%m-%Y"

# --------------------------------------------------------------------------- #
# GSTIN / state codes
# --------------------------------------------------------------------------- #
STATE_CODES: dict[str, str] = {
    "01": "Jammu and Kashmir", "02": "Himachal Pradesh", "03": "Punjab", "04": "Chandigarh",
    "05": "Uttarakhand", "06": "Haryana", "07": "Delhi", "08": "Rajasthan", "09": "Uttar Pradesh",
    "10": "Bihar", "11": "Sikkim", "12": "Arunachal Pradesh", "13": "Nagaland", "14": "Manipur",
    "15": "Mizoram", "16": "Tripura", "17": "Meghalaya", "18": "Assam", "19": "West Bengal",
    "20": "Jharkhand", "21": "Odisha", "22": "Chhattisgarh", "23": "Madhya Pradesh", "24": "Gujarat",
    "25": "Daman and Diu", "26": "Dadra and Nagar Haveli and Daman and Diu", "27": "Maharashtra",
    "28": "Andhra Pradesh (Old)", "29": "Karnataka", "30": "Goa", "31": "Lakshadweep", "32": "Kerala",
    "33": "Tamil Nadu", "34": "Puducherry", "35": "Andaman and Nicobar Islands", "36": "Telangana",
    "37": "Andhra Pradesh", "38": "Ladakh", "97": "Other Territory", "99": "Centre Jurisdiction",
}
VALID_STATE_CODES: frozenset[str] = frozenset(STATE_CODES)

# --------------------------------------------------------------------------- #
# Report formatting
# --------------------------------------------------------------------------- #
# Indian digit grouping (lakh / crore) with rupee symbol.  Excel allows only two
# conditions per format, so the report picks one of three formats per cell
# (verified by rendering in Excel):
#   INR_FORMAT      values >= 0 below ₹100 crore
#   INR_FORMAT_NEG  negatives above -₹100 crore.  Excel shows the minus sign by
#                   itself only in the unconditional 3rd section, so the two
#                   conditional sections carry an explicit "-".
#   INR_FORMAT_*    ₹100 crore and above, either sign (single section: Excel adds "-")
INR_FORMAT = (
    '[>=10000000]"₹"##\\,##\\,##\\,##0.00;'
    '[>=100000]"₹"##\\,##\\,##0.00;'
    '"₹"##,##0.00'
)
INR_FORMAT_NEG = (
    '[<=-10000000]-"₹"##\\,##\\,##\\,##0.00;'
    '[<=-100000]-"₹"##\\,##\\,##0.00;'
    '"₹"##,##0.00'
)
INR_FORMAT_ARAB = '"₹"##\\,##\\,##\\,##\\,##0.00'          # 100 crore .. < 10,000 crore
INR_FORMAT_KHARAB = '"₹"##\\,##\\,##\\,##\\,##\\,##0.00'   # 10,000 crore and above
EXCEL_DATE_FORMAT = "dd-mm-yyyy"
HEADER_FILL = "1F4E78"
HEADER_FONT_COLOR = "FFFFFF"
DIFF_HEADER_FILL = "C00000"
DIFF_CELL_FILL = "FFC7CE"
DIFF_CELL_FONT = "9C0006"
TOLERANCE_CELL_FILL = "FFEB9C"
MAX_COL_WIDTH = 45

# Streamlit
APP_TITLE = "GST Reconciliation"
