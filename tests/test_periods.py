"""Tests for financial-year / month-range handling and carry forward (modules.periods)."""
from __future__ import annotations

import pandas as pd
import pytest

from conftest import clean, doc
from modules.database import Database
from modules.matcher import reconcile
from modules.periods import (
    add_carried, add_pending_age, available_fys, carry_forward_schedule, filter_books, filter_portal, fy_months,
    fy_of_month, month_label, pending_documents, range_label,
)


def test_fy_helpers():
    months = fy_months("2024-25")
    assert months[0] == "2024-04" and months[-1] == "2025-03" and len(months) == 12
    assert fy_of_month("2025-03") == "2024-25" and fy_of_month("2025-04") == "2025-26"
    assert month_label("2024-05") == "May 2024" and month_label("") == ""
    assert range_label("2024-04", "2024-06") == "Apr 2024 - Jun 2024"
    assert range_label("2024-05", "2024-05") == "May 2024"


def test_available_fys_newest_first():
    books = clean([doc("A", 1, date="10-03-2024"), doc("B", 1, date="10-04-2024")])
    assert available_fys(books) == ["2024-25", "2023-24"]


def test_filters_use_invoice_month_for_books_and_return_period_for_portal():
    books = clean([doc("A", 1, date="10-04-2024"), doc("B", 1, date="10-05-2024"), doc("C", 1, date="10-06-2024")])
    portal = clean([doc("A", 1, date="10-04-2024", period="052024"), doc("B", 1, date="10-05-2024")], "portal")
    fb = filter_books(books, "2024-05", "2024-06")
    fp = filter_portal(portal, "2024-05", "2024-05")
    assert fb["invoice_no"].tolist() == ["B", "C"] and fb["_rid"].tolist() == [0, 1]
    assert fp["invoice_no"].tolist() == ["A", "B"]      # A was invoiced in April but reported in May


@pytest.fixture
def april_run():
    """April: A matched, B and C missing in 2B."""
    books = clean([doc("A", 1000, 180, date="05-04-2024"), doc("B", 2000, 360, date="12-04-2024"),
                   doc("C", 500, 90, date="20-04-2024")])
    portal = clean([doc("A", 1000, 180, date="05-04-2024", period="042024")], "portal")
    return reconcile(books, portal)


def test_pending_documents_carry_label(april_run):
    carried = pending_documents(april_run, "Apr 2024")
    assert sorted(carried["invoice_no"]) == ["B", "C"]
    assert set(carried["carried_from"]) == {"Apr 2024"}


def test_carry_forward_until_documents_appear(april_run):
    # May run: books have a new invoice D; B appears in May 2B, C still missing.
    carried = pending_documents(april_run, "Apr 2024")
    may_books = clean([doc("D", 800, 144, date="03-05-2024")])
    books, n = add_carried(may_books, carried)
    assert n == 2 and books["_rid"].is_unique
    portal = clean([doc("B", 2000, 360, date="12-04-2024", period="052024"),
                    doc("D", 800, 144, date="03-05-2024", period="052024")], "portal")
    may = reconcile(books, portal)
    rec = may.recon.set_index("invoice_no_books")
    assert rec.loc["B", "status"] == "Exact Match" and rec.loc["B", "carried_from_books"] == "Apr 2024"
    assert rec.loc["C", "status"] == "Missing in GSTR-2B" and rec.loc["C", "carried_from_books"] == "Apr 2024"
    assert rec.loc["D", "carried_from_books"] == ""
    # June: C is carried again and keeps its original label
    june = pending_documents(may, "May 2024")
    assert june["invoice_no"].tolist() == ["C"] and june["carried_from"].tolist() == ["Apr 2024"]


def test_carried_duplicate_of_new_entry_is_dropped(april_run):
    carried = pending_documents(april_run, "Apr 2024")
    books, n = add_carried(clean([doc("B", 2000, 360, date="12-04-2024")]), carried)
    assert n == 1 and sorted(books["invoice_no"]) == ["B", "C"]
    assert not books["is_duplicate"].any()


def test_rejected_probable_is_carried(april_run):
    books = clean([doc("KEL/56789", 3300, 594, date="05-04-2024")])
    portal = clean([doc("KEL/56798", 3300, 594, date="05-04-2024")], "portal")
    r = reconcile(books, portal)
    mid = r.recon["match_id"].iloc[0]
    view = r.with_decisions(pd.DataFrame({"match_id": [mid], "decision": ["Rejected"], "remarks": [""]}))
    assert pending_documents(view, "Apr 2024")["invoice_no"].tolist() == ["KEL/56789"]


def test_schedule_moves_documents_month_by_month():
    books = clean([doc("A", 1000, 180, date="05-04-2024"), doc("B", 2000, 360, date="12-04-2024"),
                   doc("C", 500, 90, date="20-04-2024"), doc("D", 800, 144, date="03-05-2024")])
    portal = clean([doc("A", 1000, 180, date="05-04-2024", period="042024"),
                    doc("B", 2000, 360, date="12-04-2024", period="052024"),
                    doc("D", 800, 144, date="03-05-2024", period="062024")], "portal")
    r = reconcile(books, portal)
    sched = carry_forward_schedule(r, ["2024-04", "2024-05", "2024-06"]).set_index("month")
    assert sched.loc["2024-04"].tolist() == [0, 0, 3, 630, 1, 180, 2, 450]
    assert sched.loc["2024-05"].tolist() == [2, 450, 1, 144, 1, 360, 2, 234]
    assert sched.loc["2024-06"].tolist() == [2, 234, 0, 0, 1, 144, 1, 90]
    # closing = opening + added - reflected, every month
    s = sched
    assert ((s["opening_count"] + s["added_count"] - s["reflected_count"]) == s["closing_count"]).all()
    aged = add_pending_age(r.recon, "2024-06").set_index("invoice_no_books")["months_pending"]
    assert aged.to_dict() == {"A": 0, "B": 1, "C": 2, "D": 1}


def test_schedule_counts_carried_as_opening(april_run):
    carried = pending_documents(april_run, "Apr 2024")
    books, _ = add_carried(clean([doc("D", 800, 144, date="03-05-2024")]), carried)
    portal = clean([doc("B", 2000, 360, date="12-04-2024", period="052024")], "portal")
    sched = carry_forward_schedule(reconcile(books, portal), ["2024-05"]).iloc[0]
    assert (sched["opening_count"], sched["added_count"], sched["reflected_count"], sched["closing_count"]) == (2, 1, 1, 2)


def test_find_previous_run(tmp_path, april_run):
    db = Database(f"sqlite:///{(tmp_path / 'p.db').as_posix()}")
    apr = db.save_run(april_run, "Apr 2024", company_gstin="27AABCD1234E1Z8", fy="2024-25",
                      from_month="2024-04", to_month="2024-04")
    db.save_run(april_run, "Other co", company_gstin="29AABCK4321M1ZK", fy="2024-25", from_month="2024-04",
                to_month="2024-04")
    assert db.find_previous_run("purchase", "GSTR-2B", "27AABCD1234E1Z8", "2024-05") == apr
    assert db.find_previous_run("purchase", "GSTR-2B", "27AABCD1234E1Z8", "2024-04") is None
    assert db.find_previous_run("sales", "GSTR-1", "27AABCD1234E1Z8", "2024-05") is None
    loaded, _, meta = db.load_run(apr)
    assert meta["to_month"] == "2024-04"
    assert sorted(pending_documents(loaded, meta["period_label"])["invoice_no"]) == ["B", "C"]
