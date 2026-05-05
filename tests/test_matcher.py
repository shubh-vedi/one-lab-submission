"""
tests/test_matcher.py — Hand-crafted 10-row scenario covering all anomaly types.

Rows:
  P1  ←→ B1   : MATCHED via TIER1 (exact psp_ref)
  P2  ←→ B2   : MATCHED via TIER3 (same cust+amt, within window)
  P3  (only)  : IN_TRANSIT (within settlement window, no bank entry)
  P4  (only)  : ORPHAN_REFUND (txn_type=REFUND, no bank entry)
  P5  (only)  : REVERSED (txn_type=REVERSAL, no bank entry)
  P6  (only)  : MISSING_PSP (no psp_ref, no bank match)
  P7  ←→ B3   : MATCHED via TIER1, but B4 is a DUPLICATE_BANK of the same ref
  B4  (only)  : DUPLICATE_BANK (description contains "duplicate")
  B5  (only)  : TIMING_GAP (bank-only, older than settlement window)
  B6  (only)  : UNCLASSIFIED fallback (novel bank-only row)

Run:  pytest tests/test_matcher.py -v
"""

import datetime
import sys
import os

# Ensure the project root is on the path when running from repo root or tests/
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from models import (
    LABEL_DUPLICATE_BANK,
    LABEL_IN_TRANSIT,
    LABEL_MATCHED,
    LABEL_MISSING_PSP,
    LABEL_ORPHAN_REFUND,
    LABEL_REVERSED,
    LABEL_TIMING_GAP,
    LABEL_UNCLASSIFIED,
    BankTxn,
    PlatformTxn,
)
from matcher import run_matcher

TODAY = datetime.date.today()
YESTERDAY = TODAY - datetime.timedelta(days=1)
WINDOW = 2  # settlement_window_days


# ---------------------------------------------------------------------------
# Fixture — hand-crafted dataset
# ---------------------------------------------------------------------------

@pytest.fixture()
def hand_crafted_data():
    platform_txns = [
        # P1 — will match B1 via TIER1 exact psp_ref
        PlatformTxn(
            txn_id="P1",
            customer_id="CUST_A",
            amount_paise=100_000,
            platform_date=YESTERDAY,
            psp_ref="PSP_EXACT_1",
            txn_type="PAYMENT",
            _true_label=LABEL_MATCHED,
        ),
        # P2 — will match B2 via TIER3 (no psp_ref on either side)
        PlatformTxn(
            txn_id="P2",
            customer_id="CUST_B",
            amount_paise=50_000,
            platform_date=YESTERDAY,
            psp_ref=None,
            txn_type="PAYMENT",
            _true_label=LABEL_MATCHED,
        ),
        # P3 — IN_TRANSIT: no bank row, within window
        PlatformTxn(
            txn_id="P3",
            customer_id="CUST_C",
            amount_paise=75_000,
            platform_date=TODAY,
            psp_ref="PSP_TRANSIT",
            txn_type="PAYMENT",
            _true_label=LABEL_IN_TRANSIT,
        ),
        # P4 — ORPHAN_REFUND: REFUND type, no bank counterpart
        PlatformTxn(
            txn_id="P4",
            customer_id="CUST_D",
            amount_paise=25_000,
            platform_date=YESTERDAY,
            psp_ref="PSP_REFUND",
            txn_type="REFUND",
            _true_label=LABEL_ORPHAN_REFUND,
        ),
        # P5 — REVERSED: REVERSAL type, no bank counterpart
        PlatformTxn(
            txn_id="P5",
            customer_id="CUST_E",
            amount_paise=30_000,
            platform_date=YESTERDAY,
            psp_ref="PSP_REV",
            txn_type="REVERSAL",
            _true_label=LABEL_REVERSED,
        ),
        # P6 — MISSING_PSP: no psp_ref and no matching bank row
        PlatformTxn(
            txn_id="P6",
            customer_id="CUST_F",
            amount_paise=99_999,  # unique amount → no TIER3 match
            platform_date=YESTERDAY,
            psp_ref=None,
            txn_type="PAYMENT",
            _true_label=LABEL_MISSING_PSP,
        ),
        # P7 — will match B3 via TIER1; B4 is a bank-side duplicate
        PlatformTxn(
            txn_id="P7",
            customer_id="CUST_G",
            amount_paise=200_000,
            platform_date=YESTERDAY,
            psp_ref="PSP_DUP_ORIGINAL",
            txn_type="PAYMENT",
            _true_label=LABEL_MATCHED,
        ),
    ]

    bank_txns = [
        # B1 — matches P1 via TIER1
        BankTxn(
            bank_ref="B1",
            amount_paise=100_000,
            bank_date=TODAY,
            psp_ref="PSP_EXACT_1",
            description="Settlement PSP_EXACT_1",
            _true_label=LABEL_MATCHED,
        ),
        # B2 — matches P2 via TIER3 (same amount, within window, no psp_ref)
        BankTxn(
            bank_ref="B2",
            amount_paise=50_000,
            bank_date=TODAY,
            psp_ref=None,
            description="Settlement CUST_B 500.00",
            _true_label=LABEL_MATCHED,
        ),
        # B3 — matches P7 via TIER1
        BankTxn(
            bank_ref="B3",
            amount_paise=200_000,
            bank_date=TODAY,
            psp_ref="PSP_DUP_ORIGINAL",
            description="Settlement PSP_DUP_ORIGINAL",
            _true_label=LABEL_MATCHED,
        ),
        # B4 — DUPLICATE_BANK: second bank row for same psp_ref (P7 already matched B3)
        BankTxn(
            bank_ref="B4",
            amount_paise=200_000,
            bank_date=TODAY,
            psp_ref="PSP_DUP_ORIGINAL",
            description="duplicate entry PSP_DUP_ORIGINAL",
            _true_label=LABEL_DUPLICATE_BANK,
        ),
        # B5 — TIMING_GAP: bank-only, date is older than settlement window
        BankTxn(
            bank_ref="B5",
            amount_paise=12_300,
            bank_date=TODAY - datetime.timedelta(days=WINDOW + 5),
            psp_ref=None,
            description="Old settlement entry",
            _true_label=LABEL_TIMING_GAP,
        ),
        # B6 — UNCLASSIFIED: novel bank-only row that matches no classifier rule above fallback
        BankTxn(
            bank_ref="B6",
            amount_paise=55_555,
            bank_date=TODAY,
            psp_ref=None,
            description="Mystery credit",
            _true_label=LABEL_UNCLASSIFIED,
        ),
    ]

    return platform_txns, bank_txns


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _result_map(results):
    """Build a dict: (platform_id or None, bank_id or None) → MatchResult."""
    return {
        (
            r.platform_txn.txn_id if r.platform_txn else None,
            r.bank_txn.bank_ref if r.bank_txn else None,
        ): r
        for r in results
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestMatcherHandCrafted:
    def test_total_result_count(self, hand_crafted_data):
        """Every platform and bank row must appear in exactly one MatchResult."""
        p_txns, b_txns = hand_crafted_data
        results, _ = run_matcher(p_txns, b_txns, settlement_window_days=WINDOW)
        # Each platform row appears once; each bank row appears once.
        p_ids_in_results = [r.platform_txn.txn_id for r in results if r.platform_txn]
        b_refs_in_results = [r.bank_txn.bank_ref for r in results if r.bank_txn]
        assert sorted(p_ids_in_results) == sorted([p.txn_id for p in p_txns])
        assert sorted(b_refs_in_results) == sorted([b.bank_ref for b in b_txns])

    def test_tier1_exact_match_p1_b1(self, hand_crafted_data):
        p_txns, b_txns = hand_crafted_data
        results, _ = run_matcher(p_txns, b_txns, settlement_window_days=WINDOW)
        rmap = _result_map(results)
        r = rmap[("P1", "B1")]
        assert r.label == LABEL_MATCHED
        assert r.match_tier == "TIER1"

    def test_tier3_fuzzy_match_p2_b2(self, hand_crafted_data):
        p_txns, b_txns = hand_crafted_data
        results, _ = run_matcher(p_txns, b_txns, settlement_window_days=WINDOW)
        rmap = _result_map(results)
        r = rmap[("P2", "B2")]
        assert r.label == LABEL_MATCHED
        assert r.match_tier == "TIER3"

    def test_in_transit_p3(self, hand_crafted_data):
        p_txns, b_txns = hand_crafted_data
        results, _ = run_matcher(p_txns, b_txns, settlement_window_days=WINDOW)
        rmap = _result_map(results)
        r = rmap[("P3", None)]
        assert r.label == LABEL_IN_TRANSIT

    def test_orphan_refund_p4(self, hand_crafted_data):
        p_txns, b_txns = hand_crafted_data
        results, _ = run_matcher(p_txns, b_txns, settlement_window_days=WINDOW)
        rmap = _result_map(results)
        r = rmap[("P4", None)]
        assert r.label == LABEL_ORPHAN_REFUND

    def test_reversed_p5(self, hand_crafted_data):
        p_txns, b_txns = hand_crafted_data
        results, _ = run_matcher(p_txns, b_txns, settlement_window_days=WINDOW)
        rmap = _result_map(results)
        r = rmap[("P5", None)]
        assert r.label == LABEL_REVERSED

    def test_missing_psp_p6(self, hand_crafted_data):
        p_txns, b_txns = hand_crafted_data
        results, _ = run_matcher(p_txns, b_txns, settlement_window_days=WINDOW)
        rmap = _result_map(results)
        r = rmap[("P6", None)]
        assert r.label == LABEL_MISSING_PSP

    def test_tier1_p7_b3_matched(self, hand_crafted_data):
        p_txns, b_txns = hand_crafted_data
        results, _ = run_matcher(p_txns, b_txns, settlement_window_days=WINDOW)
        rmap = _result_map(results)
        r = rmap[("P7", "B3")]
        assert r.label == LABEL_MATCHED
        assert r.match_tier == "TIER1"

    def test_duplicate_bank_b4(self, hand_crafted_data):
        p_txns, b_txns = hand_crafted_data
        results, _ = run_matcher(p_txns, b_txns, settlement_window_days=WINDOW)
        rmap = _result_map(results)
        r = rmap[(None, "B4")]
        assert r.label == LABEL_DUPLICATE_BANK

    def test_timing_gap_b5(self, hand_crafted_data):
        p_txns, b_txns = hand_crafted_data
        results, _ = run_matcher(p_txns, b_txns, settlement_window_days=WINDOW)
        rmap = _result_map(results)
        r = rmap[(None, "B5")]
        assert r.label == LABEL_TIMING_GAP

    def test_unclassified_b6(self, hand_crafted_data):
        p_txns, b_txns = hand_crafted_data
        results, _ = run_matcher(p_txns, b_txns, settlement_window_days=WINDOW)
        rmap = _result_map(results)
        r = rmap[(None, "B6")]
        assert r.label == LABEL_UNCLASSIFIED

    def test_audit_trail_non_empty(self, hand_crafted_data):
        p_txns, b_txns = hand_crafted_data
        _, audit_log = run_matcher(p_txns, b_txns, settlement_window_days=WINDOW)
        assert len(audit_log) > 0

    def test_no_row_receives_multiple_results(self, hand_crafted_data):
        """Guard against double-assignment: each row must appear in at most one result."""
        p_txns, b_txns = hand_crafted_data
        results, _ = run_matcher(p_txns, b_txns, settlement_window_days=WINDOW)
        p_counts: dict[str, int] = {}
        b_counts: dict[str, int] = {}
        for r in results:
            if r.platform_txn:
                p_counts[r.platform_txn.txn_id] = p_counts.get(r.platform_txn.txn_id, 0) + 1
            if r.bank_txn:
                b_counts[r.bank_txn.bank_ref] = b_counts.get(r.bank_txn.bank_ref, 0) + 1
        for tid, cnt in p_counts.items():
            assert cnt == 1, f"Platform txn {tid!r} appears {cnt} times"
        for bref, cnt in b_counts.items():
            assert cnt == 1, f"Bank txn {bref!r} appears {cnt} times"
