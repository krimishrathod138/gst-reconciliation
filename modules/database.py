"""
Persistence layer (SQLite via SQLAlchemy 2.0).

Tables
------
* ``recon_runs``        one row per reconciliation run: FY / month range, period label,
                        timestamp, user, settings, headline summary, GSTR-3B figures and the
                        result frames (Parquet-compressed blobs).
* ``review_decisions``  current accept / reject decision and remarks per run and match id.
* ``review_audit``      append-only trail: who changed which decision / remark, when, old -> new.
* ``column_mappings``   saved column-mapping profiles.
* ``users``             login accounts (password hashes only - see :mod:`modules.auth`).
* ``vendors``           vendor master: GSTIN, name, e-mail, phone, contact person.
* ``app_settings``      key/value settings (SMTP server etc.; never passwords).
* ``email_log``         every follow-up e-mail sent (or attempted) from the app.

Existing databases are migrated automatically: missing tables are created and
missing columns are added (``ALTER TABLE ... ADD COLUMN``), so history survives upgrades.
Frames are stored as Parquet (via pyarrow): compact, typed and - unlike pickle - safe to load.
"""
from __future__ import annotations

import io
import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd
from sqlalchemy import (
    Boolean, DateTime, ForeignKey, Integer, LargeBinary, String, Text, UniqueConstraint, create_engine, delete,
    inspect, select, text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship, sessionmaker

from config import DB_URL, ReconSettings
from modules import ReconError
from modules.matcher import ReconResult

DECISION_COLUMNS = ["match_id", "decision", "remarks", "updated_by", "updated_at"]


class Base(DeclarativeBase):
    """Declarative base for all tables."""


class ReconRun(Base):
    __tablename__ = "recon_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, index=True)
    created_by: Mapped[str | None] = mapped_column(String(50), default="")
    mode: Mapped[str] = mapped_column(String(20))
    portal_name: Mapped[str] = mapped_column(String(20))
    period_label: Mapped[str] = mapped_column(String(100), default="")
    fy: Mapped[str | None] = mapped_column(String(7), default="")
    from_month: Mapped[str | None] = mapped_column(String(7), default="")
    to_month: Mapped[str | None] = mapped_column(String(7), default="", index=True)
    company_name: Mapped[str] = mapped_column(String(200), default="")
    company_gstin: Mapped[str] = mapped_column(String(15), default="")
    books_files: Mapped[str] = mapped_column(Text, default="")
    portal_files: Mapped[str] = mapped_column(Text, default="")
    settings_json: Mapped[str] = mapped_column(Text, default="{}")
    summary_json: Mapped[str] = mapped_column(Text, default="{}")
    gstr3b_json: Mapped[str | None] = mapped_column(Text, default="")
    recon_blob: Mapped[bytes] = mapped_column(LargeBinary)
    books_blob: Mapped[bytes] = mapped_column(LargeBinary)
    portal_blob: Mapped[bytes] = mapped_column(LargeBinary)

    decisions: Mapped[list["ReviewDecision"]] = relationship(back_populates="run", cascade="all, delete-orphan")


class ReviewDecision(Base):
    __tablename__ = "review_decisions"
    __table_args__ = (UniqueConstraint("run_id", "match_id", name="uq_run_match"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("recon_runs.id", ondelete="CASCADE"), index=True)
    match_id: Mapped[str] = mapped_column(String(20))
    decision: Mapped[str] = mapped_column(String(20), default="Pending")
    remarks: Mapped[str] = mapped_column(Text, default="")
    updated_by: Mapped[str | None] = mapped_column(String(50), default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, onupdate=datetime.now)

    run: Mapped[ReconRun] = relationship(back_populates="decisions")


class ReviewAudit(Base):
    __tablename__ = "review_audit"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(Integer, index=True)
    match_id: Mapped[str] = mapped_column(String(20))
    action: Mapped[str] = mapped_column(String(20))
    old_decision: Mapped[str] = mapped_column(String(20), default="")
    new_decision: Mapped[str] = mapped_column(String(20), default="")
    old_remarks: Mapped[str] = mapped_column(Text, default="")
    new_remarks: Mapped[str] = mapped_column(Text, default="")
    username: Mapped[str] = mapped_column(String(50), default="")
    at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, index=True)


class ColumnMapping(Base):
    __tablename__ = "column_mappings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    dataset: Mapped[str] = mapped_column(String(20))  # books | portal
    mapping_json: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, onupdate=datetime.now)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(50), unique=True, index=True)
    full_name: Mapped[str] = mapped_column(String(100), default="")
    password_hash: Mapped[str] = mapped_column(String(200))
    role: Mapped[str] = mapped_column(String(10), default="user")  # admin | user
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    failed_attempts: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_login: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    created_by: Mapped[str] = mapped_column(String(50), default="")


class Vendor(Base):
    __tablename__ = "vendors"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    gstin: Mapped[str] = mapped_column(String(15), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(200), default="")
    email: Mapped[str] = mapped_column(String(500), default="")
    phone: Mapped[str] = mapped_column(String(50), default="")
    contact_person: Mapped[str] = mapped_column(String(100), default="")
    notes: Mapped[str] = mapped_column(Text, default="")
    updated_by: Mapped[str] = mapped_column(String(50), default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, onupdate=datetime.now)


class AppSetting(Base):
    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(50), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")
    updated_by: Mapped[str] = mapped_column(String(50), default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, onupdate=datetime.now)


class EmailLog(Base):
    __tablename__ = "email_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    gstin: Mapped[str] = mapped_column(String(15), default="")
    to_address: Mapped[str] = mapped_column(String(500), default="")
    subject: Mapped[str] = mapped_column(String(300), default="")
    status: Mapped[str] = mapped_column(String(10), default="")  # sent | failed
    error: Mapped[str] = mapped_column(Text, default="")
    sent_by: Mapped[str] = mapped_column(String(50), default="")
    sent_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, index=True)


# --------------------------------------------------------------------------- #
# Serialisation helpers
# --------------------------------------------------------------------------- #
def frame_to_bytes(df: pd.DataFrame) -> bytes:
    """Serialise a frame to Parquet bytes (object columns with mixed types are stringified)."""
    out = df.copy()
    for col in out.columns:
        if out[col].dtype == object:
            non_null = out[col].dropna()
            kinds = {type(v) for v in non_null.head(1000)}
            if len(kinds) > 1 or (kinds and not kinds <= {str, bool, float, int}):
                out[col] = out[col].map(lambda v: None if pd.isna(v) else str(v))
            elif kinds == {bool}:
                out[col] = out[col].astype(bool)
    buf = io.BytesIO()
    out.to_parquet(buf, index=False, compression="zstd")
    return buf.getvalue()


def bytes_to_frame(data: bytes) -> pd.DataFrame:
    """Inverse of :func:`frame_to_bytes`."""
    return pd.read_parquet(io.BytesIO(data))


def _clean(value: Any) -> str:
    return "" if value is None or (not isinstance(value, str) and pd.isna(value)) else str(value)


# --------------------------------------------------------------------------- #
# Repository
# --------------------------------------------------------------------------- #
class Database:
    """Repository around the SQLite database.

    Args:
        url: SQLAlchemy URL; defaults to ``config.DB_URL`` (``data/gst_recon.db``).
    """

    def __init__(self, url: str = DB_URL) -> None:
        if url.startswith("sqlite:///") and not url.endswith(":memory:"):
            Path(url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
        self.engine = create_engine(url)
        Base.metadata.create_all(self.engine)
        self._migrate()
        self._session = sessionmaker(self.engine, expire_on_commit=False)

    def _migrate(self) -> None:
        """Add columns introduced by newer versions to tables of an older database."""
        insp = inspect(self.engine)
        with self.engine.begin() as conn:
            for table in Base.metadata.sorted_tables:
                existing = {c["name"] for c in insp.get_columns(table.name)}
                for col in table.columns:
                    if col.name in existing:
                        continue
                    col_type = col.type.compile(dialect=self.engine.dialect)
                    default = "''" if isinstance(col.type, (String, Text)) else ("0" if isinstance(col.type, (Integer, Boolean)) else "NULL")
                    conn.execute(text(f'ALTER TABLE "{table.name}" ADD COLUMN "{col.name}" {col_type} DEFAULT {default}'))

    def session(self) -> Session:
        return self._session()

    # ------------------------------------------------------------------ runs
    def save_run(self, result: ReconResult, period_label: str = "", company_name: str = "",
                 company_gstin: str = "", books_files: list[str] | None = None,
                 portal_files: list[str] | None = None, fy: str = "", from_month: str = "",
                 to_month: str = "", created_by: str = "") -> int:
        """Persist a reconciliation run and return its id."""
        summary = result.summary()
        run = ReconRun(
            created_at=result.created_at, created_by=created_by, mode=result.mode, portal_name=result.portal_name,
            period_label=period_label, fy=fy, from_month=from_month, to_month=to_month, company_name=company_name,
            company_gstin=company_gstin, books_files=json.dumps(books_files or []),
            portal_files=json.dumps(portal_files or []), settings_json=json.dumps(result.settings.to_dict()),
            summary_json=json.dumps(summary), gstr3b_json="",
            recon_blob=frame_to_bytes(result.recon), books_blob=frame_to_bytes(result.books),
            portal_blob=frame_to_bytes(result.portal),
        )
        with self.session() as s, s.begin():
            s.add(run)
        return run.id

    def list_runs(self) -> pd.DataFrame:
        """All runs, newest first, with headline figures (no result blobs)."""
        stmt = select(ReconRun.id, ReconRun.created_at, ReconRun.created_by, ReconRun.mode, ReconRun.portal_name,
                      ReconRun.period_label, ReconRun.fy, ReconRun.from_month, ReconRun.to_month,
                      ReconRun.company_name, ReconRun.books_files, ReconRun.portal_files,
                      ReconRun.summary_json).order_by(ReconRun.created_at.desc(), ReconRun.id.desc())
        with self.session() as s:
            rows = s.execute(stmt).all()
        records = []
        for r in rows:
            summary = json.loads(r.summary_json or "{}")
            records.append({
                "run_id": r.id, "created_at": r.created_at, "created_by": r.created_by or "",
                "reconciliation": f"{r.mode.title()} vs {r.portal_name}", "period": r.period_label,
                "fy": r.fy or "", "from_month": r.from_month or "", "to_month": r.to_month or "",
                "company": r.company_name,
                "files": ", ".join(json.loads(r.books_files or "[]") + json.loads(r.portal_files or "[]")),
                "docs_books": summary.get("docs_books"), "docs_portal": summary.get("docs_portal"),
                "itc_books": summary.get("itc_books"), "itc_portal": summary.get("itc_portal"),
                "matched": summary.get("matched_count"), "mismatch": summary.get("mismatch_count"),
                "missing_portal": summary.get("missing_portal_count"),
                "missing_books": summary.get("missing_books_count"), "itc_at_risk": summary.get("itc_at_risk"),
            })
        return pd.DataFrame(records)

    def load_run(self, run_id: int) -> tuple[ReconResult, pd.DataFrame, dict[str, Any]]:
        """Load a run: ``(base_result, decisions, meta)``."""
        with self.session() as s:
            run = s.get(ReconRun, run_id)
            if run is None:
                raise ReconError(f"Run #{run_id} not found.")
            result = ReconResult(
                recon=bytes_to_frame(run.recon_blob), books=bytes_to_frame(run.books_blob),
                portal=bytes_to_frame(run.portal_blob), settings=ReconSettings.from_dict(json.loads(run.settings_json)),
                portal_name=run.portal_name, mode=run.mode, created_at=run.created_at,
            )
            meta = {"run_id": run.id, "period_label": run.period_label, "company_name": run.company_name,
                    "company_gstin": run.company_gstin, "created_at": run.created_at,
                    "created_by": run.created_by or "", "fy": run.fy or "", "from_month": run.from_month or "",
                    "to_month": run.to_month or "",
                    "books_files": json.loads(run.books_files or "[]"),
                    "portal_files": json.loads(run.portal_files or "[]")}
        return result, self.load_decisions(run_id), meta

    def find_previous_run(self, mode: str, portal_name: str, company_gstin: str, before_month: str) -> int | None:
        """Most recent run (same mode, portal and company GSTIN) whose period ended before *before_month*.

        Used to carry forward documents still missing on the portal.
        """
        stmt = (select(ReconRun.id).where(ReconRun.mode == mode, ReconRun.portal_name == portal_name,
                                          ReconRun.company_gstin == (company_gstin or ""),
                                          ReconRun.to_month != "", ReconRun.to_month < before_month)
                .order_by(ReconRun.to_month.desc(), ReconRun.created_at.desc(), ReconRun.id.desc()).limit(1))
        with self.session() as s:
            return s.scalars(stmt).first()

    def delete_run(self, run_id: int) -> None:
        with self.session() as s, s.begin():
            s.execute(delete(ReviewDecision).where(ReviewDecision.run_id == run_id))
            s.execute(delete(ReconRun).where(ReconRun.id == run_id))

    # ------------------------------------------------------------------ GSTR-3B figures per run
    def save_gstr3b(self, run_id: int, gstr3b: pd.DataFrame) -> None:
        """Store the GSTR-3B ITC figures (one row per return period) with a run."""
        with self.session() as s, s.begin():
            run = s.get(ReconRun, run_id)
            if run is None:
                raise ReconError(f"Run #{run_id} not found.")
            run.gstr3b_json = gstr3b.to_json(orient="records")

    def load_gstr3b(self, run_id: int) -> pd.DataFrame | None:
        with self.session() as s:
            run = s.get(ReconRun, run_id)
            raw = run.gstr3b_json if run is not None else ""
        if not raw:
            return None
        return pd.DataFrame(json.loads(raw))

    # ------------------------------------------------------------------ decisions & audit trail
    def save_decisions(self, run_id: int, decisions: pd.DataFrame, username: str = "") -> int:
        """Upsert decisions (``match_id``, ``decision``, ``remarks``) for a run.

        Every change is also appended to ``review_audit`` with *username* and a
        timestamp.  Returns the number of changed rows.
        """
        if decisions is None or decisions.empty:
            return 0
        changes = 0
        now = datetime.now()
        with self.session() as s, s.begin():
            existing = {d.match_id: d for d in s.scalars(select(ReviewDecision).where(ReviewDecision.run_id == run_id))}
            for rec in decisions.drop_duplicates("match_id", keep="last").itertuples(index=False):
                remarks = _clean(getattr(rec, "remarks", ""))
                decision = _clean(getattr(rec, "decision", ""))
                row = existing.get(rec.match_id)
                old_dec, old_rem = (row.decision, row.remarks) if row is not None else ("", "")
                if row is not None and not decision:
                    decision = old_dec  # remark-only update keeps the decision
                if (decision, remarks) == (old_dec, old_rem):
                    continue
                if row is None:
                    s.add(ReviewDecision(run_id=run_id, match_id=rec.match_id, decision=decision, remarks=remarks,
                                         updated_by=username, updated_at=now))
                else:
                    row.decision, row.remarks, row.updated_by, row.updated_at = decision, remarks, username, now
                action = decision if decision != old_dec and decision else "Remark"
                s.add(ReviewAudit(run_id=run_id, match_id=rec.match_id, action=action, old_decision=old_dec,
                                  new_decision=decision, old_remarks=old_rem, new_remarks=remarks,
                                  username=username, at=now))
                changes += 1
        return changes

    def load_decisions(self, run_id: int) -> pd.DataFrame:
        with self.session() as s:
            rows = s.scalars(select(ReviewDecision).where(ReviewDecision.run_id == run_id)).all()
        return pd.DataFrame([{"match_id": r.match_id, "decision": r.decision, "remarks": r.remarks,
                              "updated_by": r.updated_by or "", "updated_at": r.updated_at} for r in rows],
                            columns=DECISION_COLUMNS)

    def load_audit(self, run_id: int) -> pd.DataFrame:
        """Full review audit trail of a run, newest first."""
        with self.session() as s:
            rows = s.scalars(select(ReviewAudit).where(ReviewAudit.run_id == run_id)
                             .order_by(ReviewAudit.at.desc(), ReviewAudit.id.desc())).all()
        return pd.DataFrame([{"at": r.at, "username": r.username, "match_id": r.match_id, "action": r.action,
                              "old_decision": r.old_decision, "new_decision": r.new_decision,
                              "old_remarks": r.old_remarks, "new_remarks": r.new_remarks} for r in rows],
                            columns=["at", "username", "match_id", "action", "old_decision", "new_decision",
                                     "old_remarks", "new_remarks"])

    # ------------------------------------------------------------------ column mappings
    def save_mapping(self, name: str, dataset: str, mapping: dict[str, str | None]) -> None:
        """Create or overwrite a named mapping profile."""
        name = name.strip()
        if not name:
            raise ReconError("Please enter a name for the mapping profile.")
        with self.session() as s, s.begin():
            row = s.scalars(select(ColumnMapping).where(ColumnMapping.name == name)).first()
            payload = json.dumps({k: v for k, v in mapping.items() if v})
            if row is None:
                s.add(ColumnMapping(name=name, dataset=dataset, mapping_json=payload))
            else:
                row.dataset, row.mapping_json = dataset, payload

    def list_mappings(self, dataset: str | None = None) -> list[str]:
        stmt = select(ColumnMapping.name).order_by(ColumnMapping.name)
        if dataset:
            stmt = stmt.where(ColumnMapping.dataset == dataset)
        with self.session() as s:
            return list(s.scalars(stmt))

    def load_mapping(self, name: str) -> dict[str, str]:
        with self.session() as s:
            row = s.scalars(select(ColumnMapping).where(ColumnMapping.name == name)).first()
        if row is None:
            raise ReconError(f"Mapping profile '{name}' not found.")
        return json.loads(row.mapping_json)

    def delete_mapping(self, name: str) -> None:
        with self.session() as s, s.begin():
            s.execute(delete(ColumnMapping).where(ColumnMapping.name == name))

    # ------------------------------------------------------------------ vendors
    def list_vendors(self) -> pd.DataFrame:
        with self.session() as s:
            rows = s.scalars(select(Vendor).order_by(Vendor.name, Vendor.gstin)).all()
        return pd.DataFrame([{"gstin": v.gstin, "name": v.name, "email": v.email, "phone": v.phone,
                              "contact_person": v.contact_person, "notes": v.notes, "updated_by": v.updated_by,
                              "updated_at": v.updated_at} for v in rows],
                            columns=["gstin", "name", "email", "phone", "contact_person", "notes", "updated_by",
                                     "updated_at"])

    def get_vendor(self, gstin: str) -> dict[str, str] | None:
        with self.session() as s:
            v = s.scalars(select(Vendor).where(Vendor.gstin == gstin.strip().upper())).first()
        if v is None:
            return None
        return {"gstin": v.gstin, "name": v.name, "email": v.email, "phone": v.phone,
                "contact_person": v.contact_person, "notes": v.notes}

    def upsert_vendors(self, vendors: pd.DataFrame, username: str = "") -> tuple[int, int]:
        """Insert or update vendors keyed by GSTIN (already validated). Returns ``(added, updated)``."""
        added = updated = 0
        fields = ("name", "email", "phone", "contact_person", "notes")
        with self.session() as s, s.begin():
            existing = {v.gstin: v for v in s.scalars(select(Vendor))}
            for rec in vendors.to_dict("records"):
                gstin = _clean(rec.get("gstin")).strip().upper()
                if not gstin:
                    continue
                values = {f: _clean(rec.get(f)).strip() for f in fields if f in rec}
                row = existing.get(gstin)
                if row is None:
                    row = Vendor(gstin=gstin, updated_by=username, **values)
                    s.add(row)
                    existing[gstin] = row
                    added += 1
                elif any(getattr(row, f) != v for f, v in values.items()):
                    for f, v in values.items():
                        setattr(row, f, v)
                    row.updated_by = username
                    updated += 1
        return added, updated

    def delete_vendors(self, gstins: list[str]) -> int:
        with self.session() as s, s.begin():
            result = s.execute(delete(Vendor).where(Vendor.gstin.in_([g.strip().upper() for g in gstins])))
        return result.rowcount or 0

    # ------------------------------------------------------------------ settings
    def get_settings(self, prefix: str = "") -> dict[str, str]:
        with self.session() as s:
            rows = s.scalars(select(AppSetting).where(AppSetting.key.startswith(prefix))).all()
        return {r.key: r.value for r in rows}

    def set_settings(self, values: dict[str, Any], username: str = "") -> None:
        for key in values:
            if "password" in key.lower():
                raise ReconError("Passwords are never stored in the database.")
        with self.session() as s, s.begin():
            for key, value in values.items():
                row = s.get(AppSetting, key)
                if row is None:
                    s.add(AppSetting(key=key, value=_clean(value), updated_by=username))
                else:
                    row.value, row.updated_by = _clean(value), username

    # ------------------------------------------------------------------ e-mail log
    def log_email(self, run_id: int | None, gstin: str, to_address: str, subject: str, status: str,
                  error: str = "", sent_by: str = "") -> None:
        with self.session() as s, s.begin():
            s.add(EmailLog(run_id=run_id, gstin=gstin, to_address=to_address, subject=subject[:300],
                           status=status, error=error, sent_by=sent_by))

    def list_emails(self, run_id: int | None = None) -> pd.DataFrame:
        stmt = select(EmailLog).order_by(EmailLog.sent_at.desc(), EmailLog.id.desc())
        if run_id is not None:
            stmt = stmt.where(EmailLog.run_id == run_id)
        with self.session() as s:
            rows = s.scalars(stmt).all()
        return pd.DataFrame([{"sent_at": r.sent_at, "sent_by": r.sent_by, "gstin": r.gstin, "to": r.to_address,
                              "subject": r.subject, "status": r.status, "error": r.error, "run_id": r.run_id}
                             for r in rows],
                            columns=["sent_at", "sent_by", "gstin", "to", "subject", "status", "error", "run_id"])
