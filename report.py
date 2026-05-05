"""
report.py — Produces the reconciliation statement from a list of MatchResults.

Formula:
    expected_bank = platform_total
                  + in_transit
                  − duplicates
                  ± rounding
                  − orphan_refunds

All arithmetic is done in integer paise (no floats).
"""

from __future__ import annotations

import datetime
import uuid
from typing import Optional

from models import (
    LABEL_DUPLICATE_BANK,
    LABEL_IN_TRANSIT,
    LABEL_MATCHED,
    LABEL_ORPHAN_REFUND,
    BankTxn,
    MatchResult,
    PlatformTxn,
    Reconciliation,
    inr_str,
)


def build_reconciliation(
    match_results: list[MatchResult],
    settlement_window_days: int = 2,
    run_id: Optional[str] = None,
) -> Reconciliation:
    """
    Aggregate MatchResults into a Reconciliation statement.

    Parameters
    ----------
    match_results        : Full list from run_matcher()
    settlement_window_days : T+N window used during matching
    run_id               : Optional identifier for this run; auto-generated if None
    """
    run_id = run_id or str(uuid.uuid4())[:8].upper()

    # ── Totals ──────────────────────────────────────────────────────────────
    platform_total_paise = 0
    actual_bank_paise = 0
    in_transit_paise = 0
    duplicate_paise = 0
    rounding_paise = 0        # reserved for future use; kept at 0 in v1
    orphan_refund_paise = 0

    matched_count = 0
    unmatched_platform_count = 0
    unmatched_bank_count = 0

    seen_platform_ids: set[str] = set()
    seen_bank_refs: set[str] = set()

    for mr in match_results:
        # Platform side contribution
        if mr.platform_txn and mr.platform_txn.txn_id not in seen_platform_ids:
            seen_platform_ids.add(mr.platform_txn.txn_id)
            platform_total_paise += mr.platform_txn.amount_paise

        # Bank side contribution
        if mr.bank_txn and mr.bank_txn.bank_ref not in seen_bank_refs:
            seen_bank_refs.add(mr.bank_txn.bank_ref)
            # Duplicates are excluded from the "actual bank" total we compare against
            if mr.label != LABEL_DUPLICATE_BANK:
                actual_bank_paise += mr.bank_txn.amount_paise

        # Label-specific aggregations
        if mr.label == LABEL_MATCHED:
            matched_count += 1
        elif mr.label == LABEL_IN_TRANSIT:
            in_transit_paise += mr.platform_txn.amount_paise if mr.platform_txn else 0
            unmatched_platform_count += 1
        elif mr.label == LABEL_DUPLICATE_BANK:
            duplicate_paise += mr.bank_txn.amount_paise if mr.bank_txn else 0
            unmatched_bank_count += 1
        elif mr.label == LABEL_ORPHAN_REFUND:
            if mr.platform_txn:
                orphan_refund_paise += mr.platform_txn.amount_paise
                unmatched_platform_count += 1
            else:
                orphan_refund_paise += mr.bank_txn.amount_paise if mr.bank_txn else 0
                unmatched_bank_count += 1
        elif mr.platform_txn is not None and mr.bank_txn is None:
            unmatched_platform_count += 1
        elif mr.platform_txn is None and mr.bank_txn is not None:
            unmatched_bank_count += 1

    # ── Statement formula ───────────────────────────────────────────────────
    expected_bank_paise = (
        platform_total_paise
        + in_transit_paise
        - duplicate_paise
        + rounding_paise       # signed; negative = debit adjustment
        - orphan_refund_paise
    )
    residual_paise = actual_bank_paise - expected_bank_paise

    total_platform_rows = len(seen_platform_ids)
    total_bank_rows = len(seen_bank_refs)

    return Reconciliation(
        run_id=run_id,
        generated_at=datetime.datetime.now(),
        settlement_window_days=settlement_window_days,
        platform_total_paise=platform_total_paise,
        in_transit_paise=in_transit_paise,
        duplicate_paise=duplicate_paise,
        rounding_paise=rounding_paise,
        orphan_refund_paise=orphan_refund_paise,
        expected_bank_paise=expected_bank_paise,
        actual_bank_paise=actual_bank_paise,
        residual_paise=residual_paise,
        total_platform_rows=total_platform_rows,
        total_bank_rows=total_bank_rows,
        matched_count=matched_count,
        unmatched_platform_count=unmatched_platform_count,
        unmatched_bank_count=unmatched_bank_count,
        match_results=match_results,
    )


def print_statement(recon: Reconciliation) -> None:
    """Print the reconciliation statement to stdout (useful for CLI / tests)."""
    for line in recon.summary_lines():
        print(line)
