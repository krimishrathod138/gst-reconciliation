"""Unit tests for modules.matcher."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from config import (
    DECISION_ACCEPT, DECISION_PENDING, DECISION_REJECT, STATUS_EXACT, STATUS_MANUAL, STATUS_MISMATCH,
    STATUS_MISSING_BOOKS, STATUS_PROBABLE, STATUS_TOLERANCE, ReconSettings, describe_tolerance,
)
from conftest import GSTIN_KA, VALID_GSTIN, clean, doc
from modules.matcher import (
    doc_type_table, fuzzy_candidates, reconcile, status_table, supplier_summary, within_tolerance,
)

MISSING_2B = "Missing in GSTR-2B"


def run(books_rows, portal_rows, **settings):
    return reconcile(clean(books_rows), clean(portal_rows, "portal"), ReconSettings(**settings))


def statuses(result) -> list[str]:
    return result.recon["status"].tolist()


# ----------------------------------------------------------------- tolerance helper
class TestWithinTolerance:
    @pytest.mark.parametrize("mode, a, b, expected", [
        ("either", 1000, 1009, True),      # within ₹10
        ("either", 100000, 100500, True),  # 0.5% but ₹500
        ("either", 1000, 1020, False),     # ₹20 and 2%
        ("absolute", 100000, 100500, False),
        ("percentage", 100, 105, False),   # ₹5 but 5%
        ("percentage", 100000, 100900, True),
        ("both", 1000, 1009, True),        # ₹9 and 0.9%
        ("both", 100, 109, False),         # ₹9 but 9%
    ])
    def test_modes(self, mode, a, b, expected):
        s = ReconSettings(tolerance_mode=mode)
        assert bool(within_tolerance(a, b, s)) is expected

    def test_boundary_is_inclusive(self):
        assert within_tolerance(1000, 1010, ReconSettings(tolerance_mode="absolute"))

    @pytest.mark.parametrize("settings, expected", [
        (ReconSettings(), "Within ±₹10 per amount field (₹ only); dates must match"),
        (ReconSettings(tolerance_mode="either", date_tolerance_days=2),
         "Within ±₹10 or ±1% per amount field (₹ OR %); dates within 2 day(s)"),
        (ReconSettings(tolerance_mode="both", abs_tolerance=5, pct_tolerance=0.5),
         "Within ±₹5 and ±0.5% per amount field (₹ AND %); dates must match"),
        (ReconSettings(tolerance_mode="percentage"), "Within ±1% per amount field (% only); dates must match"),
    ])
    def test_describe_tolerance(self, settings, expected):
        assert describe_tolerance(settings) == expected

    def test_broadcasting(self):
        out = within_tolerance(np.array([[100.0], [200.0]]), np.array([[105.0, 250.0]]), ReconSettings())
        assert out.shape == (2, 2) and out.tolist() == [[True, False], [False, False]]


# ----------------------------------------------------------------- tiers
class TestTiers:
    def test_exact_match_after_invoice_normalisation(self):
        r = run([doc("INV/24-25/0001", 1000, 180)], [doc("inv-1", 1000, 180)])
        assert statuses(r) == [STATUS_EXACT]
        assert r.recon.loc[0, "invoice_no_books"] == "INV/24-25/0001"
        assert r.recon.loc[0, "invoice_no_portal"] == "inv-1"

    def test_tolerance_absolute(self):
        r = run([doc("A1", 1000, 180)], [doc("A1", 1008, 181.44)])
        assert statuses(r) == [STATUS_TOLERANCE]
        assert r.recon.loc[0, "diff_taxable_value"] == -8.0
        assert r.recon.loc[0, "reason"].startswith("Within tolerance")

    def test_default_rule_is_rupees_only(self):
        s = ReconSettings()
        assert (s.tolerance_mode, s.abs_tolerance) == ("absolute", 10.0)
        # ₹500 on ₹1 lakh is only 0.5%, but beyond ₹10 -> mismatch under the default rule
        r = run([doc("A1", 100000, 18000)], [doc("A1", 100500, 18090)])
        assert statuses(r) == [STATUS_MISMATCH]

    def test_default_rule_on_large_invoice(self):
        # 1% of ₹1 crore would have allowed ₹1 lakh; the ₹-only default allows ₹10
        crore = 1_00_00_000
        assert statuses(run([doc("A1", crore, 0)], [doc("A1", crore + 10, 0)])) == [STATUS_TOLERANCE]
        assert statuses(run([doc("A1", crore, 0)], [doc("A1", crore + 50_000, 0)])) == [STATUS_MISMATCH]

    def test_tolerance_percentage_when_either(self):
        r = run([doc("A1", 100000, 18000)], [doc("A1", 100500, 18090)], tolerance_mode="either")
        assert statuses(r) == [STATUS_TOLERANCE]

    def test_pct_setting_ignored_in_rupees_only_mode(self):
        r = run([doc("A1", 1000, 180)], [doc("A1", 1030, 180)], pct_tolerance=50)
        assert statuses(r) == [STATUS_MISMATCH]

    def test_custom_tolerance_values(self):
        rows_b, rows_p = [doc("A1", 1000, 180)], [doc("A1", 1030, 180)]
        assert statuses(run(rows_b, rows_p)) == [STATUS_MISMATCH]
        assert statuses(run(rows_b, rows_p, abs_tolerance=50)) == [STATUS_TOLERANCE]

    def test_mismatch_taxable_shows_difference(self):
        r = run([doc("A1", 50000, 9000)], [doc("A1", 60000, 10800)])
        rec = r.recon.iloc[0]
        assert rec["status"] == STATUS_MISMATCH
        assert rec["diff_taxable_value"] == -10000 and rec["diff_igst"] == -1800 and rec["diff_total_tax"] == -1800
        assert "Taxable value diff -₹10,000.00" in rec["reason"]

    def test_mismatch_date(self):
        r = run([doc("A1", 1000, 180, date="01-04-2024")], [doc("A1", 1000, 180, date="06-04-2024")])
        assert statuses(r) == [STATUS_MISMATCH]
        assert r.recon.loc[0, "date_diff_days"] == -5 and "5 day" in r.recon.loc[0, "reason"]

    def test_date_tolerance_setting(self):
        r = run([doc("A1", 1000, 180, date="01-04-2024")], [doc("A1", 1000, 180, date="03-04-2024")],
                date_tolerance_days=3)
        assert statuses(r) == [STATUS_TOLERANCE]

    def test_tax_head_mismatch(self):
        r = run([doc("A1", 1000, 180)], [doc("A1", 1000, 0, 90, 90)])
        rec = r.recon.iloc[0]
        assert rec["status"] == STATUS_MISMATCH
        assert rec["reason"].startswith("Tax head: IGST in books vs CGST+SGST in GSTR-2B")
        assert rec["diff_total_tax"] == 0

    def test_tax_head_switch_never_tolerance_even_if_totals_equal(self):
        r = run([doc("A1", 1000, 0, 90, 90)], [doc("A1", 1000, 180)], abs_tolerance=1000)
        assert statuses(r) == [STATUS_MISMATCH]

    def test_missing_both_sides(self):
        r = run([doc("B1", 100, 18)], [doc("P1", 999, 180)])
        assert sorted(statuses(r)) == sorted([MISSING_2B, STATUS_MISSING_BOOKS])

    def test_different_gstin_never_matches(self):
        r = run([doc("A1", 1000, 180)], [doc("A1", 1000, 180, gstin=GSTIN_KA)])
        assert sorted(statuses(r)) == sorted([MISSING_2B, STATUS_MISSING_BOOKS])

    def test_cross_period_match_flagged(self):
        r = run([doc("A1", 1000, 180, date="25-04-2024")], [doc("A1", 1000, 180, date="25-04-2024", period="052024")])
        assert statuses(r) == [STATUS_EXACT]
        assert bool(r.recon.loc[0, "cross_period"]) is True
        assert r.recon.loc[0, "period_books"] == "2024-04" and r.recon.loc[0, "period_portal"] == "2024-05"

    def test_empty_inputs(self):
        r = reconcile(clean([]), clean([]))
        assert r.recon.empty
        assert r.summary()["docs_books"] == 0
        r = run([doc("A1", 1, 0)], [])
        assert statuses(r) == [MISSING_2B]
        r = run([], [doc("A1", 1, 0)])
        assert statuses(r) == [STATUS_MISSING_BOOKS]


# ----------------------------------------------------------------- fuzzy
class TestFuzzy:
    def test_probable_match_on_typo(self):
        r = run([doc("KEL/56789", 33000, 5940)], [doc("KEL/56798", 33000, 5940)])
        assert statuses(r) == [STATUS_PROBABLE]
        assert r.recon.loc[0, "fuzzy_score"] >= 85
        assert r.recon.loc[0, "review_decision"] == DECISION_PENDING

    def test_no_probable_when_amounts_differ(self):
        r = run([doc("KEL/56789", 33000, 5940)], [doc("KEL/56798", 43000, 7740)])
        assert STATUS_PROBABLE not in statuses(r)

    def test_no_probable_below_threshold(self):
        r = run([doc("AB12", 1000, 180)], [doc("XY98", 1000, 180)])
        assert STATUS_PROBABLE not in statuses(r)

    def test_fuzzy_can_be_disabled(self):
        r = run([doc("KEL/56789", 33000, 5940)], [doc("KEL/56798", 33000, 5940)], enable_fuzzy=False)
        assert STATUS_PROBABLE not in statuses(r)

    def test_one_to_one_assignment_prefers_best_score(self):
        books = [doc("ABCDE12345", 1000, 180)]
        portal = [doc("ABCDE12354", 1000, 180), doc("ABCDE1234", 1000, 180)]
        r = run(books, portal)
        prob = r.recon[r.recon["status"] == STATUS_PROBABLE]
        assert len(prob) == 1 and prob.iloc[0]["invoice_no_portal"] == "ABCDE1234"  # 94.7 beats 90
        assert (r.recon["status"] == STATUS_MISSING_BOOKS).sum() == 1

    def test_fuzzy_blocks_give_same_result(self):
        books = [doc(f"INVX{i:05d}", 1000 + i, 0) for i in range(30)]
        portal = [doc(f"INVX{i:05d}Z", 1000 + i, 0) for i in range(30)]
        a = run(books, portal)
        b = run(books, portal, fuzzy_block_size=7)
        assert statuses(a) == statuses(b)
        assert statuses(a).count(STATUS_PROBABLE) == 30

    def test_fuzzy_candidates_empty(self):
        out = fuzzy_candidates(pd.DataFrame(), pd.DataFrame(), ReconSettings())
        assert out.empty


# ----------------------------------------------------------------- notes & duplicates
class TestNotesAndDuplicates:
    def test_credit_note_not_matched_with_invoice_of_same_number(self):
        r = run([doc("1001", 1000, 180, typ="Credit Note")], [doc("1001", 1000, 180)])
        assert sorted(statuses(r)) == sorted([MISSING_2B, STATUS_MISSING_BOOKS])

    def test_credit_note_matches_cdnr(self):
        r = run([doc("CN/01", 500, 90, typ="Credit Note")], [doc("CN-1", 500, 90, typ="CDNR", note_type="C")])
        assert statuses(r) == [STATUS_EXACT]

    def test_credit_notes_net_itc(self):
        books = [doc("I1", 10000, 1800), doc("C1", 1000, 180, typ="Credit Note"), doc("D1", 500, 90, typ="Debit Note")]
        r = run(books, books)
        s = r.summary()
        assert s["itc_books"] == 1800 - 180 + 90 == s["itc_portal"]
        table = doc_type_table(r.recon).set_index("doc_type")
        assert table.loc["CN", "itc_books"] == -180 and table.loc["NET", "itc_books"] == 1710

    def test_duplicate_in_books_matches_once(self):
        r = run([doc("A1", 100, 18), doc("A-1", 100, 18)], [doc("A1", 100, 18)])
        assert sorted(statuses(r)) == sorted([STATUS_EXACT, MISSING_2B])
        missing = r.recon[r.recon["status"] == MISSING_2B].iloc[0]
        assert bool(missing["is_duplicate_books"]) is True

    def test_duplicates_on_both_sides_pair_in_order(self):
        r = run([doc("A1", 100, 18), doc("A1", 100, 18)], [doc("A1", 100, 18), doc("A1", 100, 18)])
        assert statuses(r) == [STATUS_EXACT, STATUS_EXACT]


# ----------------------------------------------------------------- review decisions
class TestReview:
    @pytest.fixture
    def probable(self):
        return run([doc("KEL/56789", 33000, 5940), doc("X1", 10, 0)], [doc("KEL/56798", 33000, 5940)])

    def _decide(self, result, decision, remark="checked"):
        mid = result.recon.loc[result.recon["status"] == STATUS_PROBABLE, "match_id"].iloc[0]
        return mid, result.with_decisions(pd.DataFrame({"match_id": [mid], "decision": [decision], "remarks": [remark]}))

    def test_accept(self, probable):
        mid, out = self._decide(probable, DECISION_ACCEPT)
        rec = out.recon.set_index("match_id").loc[mid]
        assert rec["status"] == STATUS_MANUAL and rec["remarks"] == "checked" and rec["review_decision"] == DECISION_ACCEPT
        assert out.summary()["matched_count"] == 1

    def test_reject_splits_into_two_orphans(self, probable):
        mid, out = self._decide(probable, DECISION_REJECT, "different invoice")
        rows = out.recon[out.recon["match_id"].str.startswith(mid)].set_index("match_id")
        assert rows.loc[f"{mid}-B", "status"] == MISSING_2B
        assert rows.loc[f"{mid}-P", "status"] == STATUS_MISSING_BOOKS
        assert rows.loc[f"{mid}-B", "invoice_no_portal"] == "" and rows.loc[f"{mid}-P", "invoice_no_books"] == ""
        assert rows.loc[f"{mid}-B", "itc_books"] == 5940 and rows.loc[f"{mid}-B", "itc_portal"] == 0
        assert (rows["remarks"] == "different invoice").all()
        assert len(out.recon) == len(probable.recon) + 1

    def test_base_result_unchanged(self, probable):
        self._decide(probable, DECISION_REJECT)
        assert STATUS_PROBABLE in statuses(probable)

    def test_pending_and_remarks_only(self, probable):
        other = probable.recon.loc[probable.recon["status"] != STATUS_PROBABLE, "match_id"].iloc[0]
        dec = pd.DataFrame({"match_id": [other], "decision": [DECISION_ACCEPT], "remarks": ["note"]})
        out = probable.with_decisions(dec).recon.set_index("match_id")
        assert out.loc[other, "status"] == MISSING_2B      # decisions only affect probable matches
        assert out.loc[other, "remarks"] == "note"

    def test_no_decisions(self, probable):
        assert probable.with_decisions(None).recon.equals(probable.recon)


# ----------------------------------------------------------------- summaries
class TestSummaries:
    @pytest.fixture
    def result(self):
        books = [doc("E1", 1000, 180), doc("M1", 1000, 180), doc("B1", 500, 90), doc("CN1", 200, 36, typ="Credit Note")]
        portal = [doc("E1", 1000, 180), doc("M1", 800, 144), doc("P1", 300, 54, gstin=GSTIN_KA, name="KA Ltd")]
        return run(books, portal)

    def test_itc_at_risk(self, result):
        s = result.summary()
        # missing invoice B1 (90) + mismatch shortfall (180-144=36); missing credit note is not a risk
        assert s["itc_at_risk"] == 126
        assert s["missing_portal_count"] == 2 and s["missing_portal_itc"] == 90 - 36
        assert s["missing_books_count"] == 1 and s["missing_books_itc"] == 54
        assert s["itc_books"] == 180 + 180 + 90 - 36 and s["itc_portal"] == 180 + 144 + 54

    def test_supplier_summary(self, result):
        sup = supplier_summary(result.recon, result.portal_name).set_index("gstin")
        assert sup.loc[VALID_GSTIN, "pending_itc"] == 126
        assert sup.loc[VALID_GSTIN, "missing_portal_count"] == 2
        assert sup.loc[GSTIN_KA, "missing_books_count"] == 1 and sup.loc[GSTIN_KA, "party_name"] == "KA Ltd"
        assert sup.index[0] == VALID_GSTIN  # sorted by pending ITC

    def test_status_table(self, result):
        t = status_table(result.recon).set_index("status")
        assert t.loc[STATUS_EXACT, "documents"] == 1
        assert t["documents"].sum() == len(result.recon)

    def test_match_ids_unique(self, result):
        assert result.recon["match_id"].is_unique


# ----------------------------------------------------------------- scale
def test_one_lakh_rows_performance():
    import time
    from modules.cleaner import clean_dataset
    rng = np.random.default_rng(7)
    n = 100_000
    gstins = [VALID_GSTIN, GSTIN_KA]
    frame = pd.DataFrame({
        "gstin": [gstins[i % 2] for i in range(n)],
        "invoice_no": [f"INV/24-25/{i:06d}" for i in range(n)],
        "invoice_date": ["15-04-2024"] * n,
        "taxable_value": rng.integers(1_000, 1_00_000, n).astype(float),
    })
    frame["igst"] = (frame["taxable_value"] * 0.18).round(2)
    portal = frame.sample(frac=0.95, random_state=1).copy()
    portal.loc[portal.index[:2000], "taxable_value"] += 5          # tolerance
    portal.loc[portal.index[2000:3000], "taxable_value"] += 5000   # mismatch
    t = time.perf_counter()
    books_c, _ = clean_dataset(frame)
    portal_c, _ = clean_dataset(portal, "portal")
    r = reconcile(books_c, portal_c)
    elapsed = time.perf_counter() - t
    counts = r.recon["status"].value_counts()
    assert counts[STATUS_TOLERANCE] == 2000 and counts[STATUS_MISMATCH] == 1000
    assert counts[MISSING_2B] == 5000
    assert elapsed < 90, f"too slow: {elapsed:.1f}s"
