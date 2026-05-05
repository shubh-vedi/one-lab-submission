"""
models.py — Frozen dataclasses for the Payments Reconciliation Engine.

All monetary amounts are stored as integer paise (1 INR = 100 paise).
Decimal / formatted strings are produced only at I/O boundaries.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def paise_to_inr(paise: int) -> Decimal:
    """Convert integer paise to a two-decimal-place Decimal (display only)."""
    return Decimal(paise) / Decimal(100)


def inr_str(paise: int) -> str:
    """Format paise as an INR currency string for display."""
    return f"₹{paise_to_inr(paise):,.2f}"


# ---------------------------------------------------------------------------
# Core domain models
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PlatformTxn:
    """A transaction recorded on the platform side."""

    txn_id: str                        # Platform-side unique identifier
    customer_id: str                   # Anonymised customer reference
    amount_paise: int                  # Amount in integer paise (never float)
    platform_date: datetime.date       # Date the transaction was recorded
    psp_ref: Optional[str]             # Payment-service-provider reference (may be None)
    txn_type: str                      # "PAYMENT" | "REFUND" | "REVERSAL"

    # Ground-truth label injected by the generator; not used in matching logic.
    # Prefixed with underscore to signal it is internal / non-business.
    _true_label: str = field(default="MATCHED", compare=False, hash=False)

    def __post_init__(self) -> None:
        if self.amount_paise < 0:
            raise ValueError(f"amount_paise must be non-negative, got {self.amount_paise}")
        if self.txn_type not in {"PAYMENT", "REFUND", "REVERSAL"}:
            raise ValueError(f"Unknown txn_type: {self.txn_type!r}")


@dataclass(frozen=True)
class BankTxn:
    """A transaction line in the bank settlement file."""

    bank_ref: str                      # Bank-side unique identifier
    amount_paise: int                  # Amount in integer paise (never float)
    bank_date: datetime.date           # Settlement date on the bank side
    psp_ref: Optional[str]             # PSP reference echoed by the bank (may be None)
    description: str                   # Free-text description from the bank

    # Ground-truth label (same convention as PlatformTxn._true_label).
    _true_label: str = field(default="MATCHED", compare=False, hash=False)

    def __post_init__(self) -> None:
        if self.amount_paise < 0:
            raise ValueError(f"amount_paise must be non-negative, got {self.amount_paise}")


@dataclass(frozen=True)
class MatchResult:
    """Outcome of pairing one PlatformTxn to one BankTxn (or neither)."""

    platform_txn: Optional[PlatformTxn]
    bank_txn: Optional[BankTxn]
    label: str                          # Classification label (see LABEL_* constants)
    score: float                        # Confidence / proximity score (higher = better)
    match_tier: Optional[str]           # "TIER1" | "TIER3" | None (unmatched)
    audit_note: str                     # Human-readable explanation of this decision

    def __post_init__(self) -> None:
        if self.platform_txn is None and self.bank_txn is None:
            raise ValueError("MatchResult must have at least one side non-None.")


@dataclass
class Reconciliation:
    """Aggregated reconciliation statement for a given run."""

    run_id: str
    generated_at: datetime.datetime
    settlement_window_days: int

    # Monetary totals (all in integer paise)
    platform_total_paise: int
    in_transit_paise: int          # Matched platform txns not yet settled
    duplicate_paise: int           # Duplicate bank entries removed
    rounding_paise: int            # Net rounding adjustments (signed)
    orphan_refund_paise: int       # Refund rows with no matching platform credit

    expected_bank_paise: int       # = platform_total + in_transit − duplicates ± rounding − orphan_refunds
    actual_bank_paise: int         # Sum of all bank rows
    residual_paise: int            # = actual_bank − expected_bank (ideally 0)

    # Counts
    total_platform_rows: int
    total_bank_rows: int
    matched_count: int
    unmatched_platform_count: int
    unmatched_bank_count: int

    # Full list of individual results for drill-down
    match_results: list[MatchResult] = field(default_factory=list)

    def summary_lines(self) -> list[str]:
        """Return formatted reconciliation statement lines (display only)."""
        lines = [
            f"Reconciliation Run : {self.run_id}",
            f"Generated At       : {self.generated_at.isoformat(timespec='seconds')}",
            f"Settlement Window  : T+{self.settlement_window_days}",
            "─" * 52,
            f"Platform Total     :  {inr_str(self.platform_total_paise):>18}",
            f"+ In-Transit       :  {inr_str(self.in_transit_paise):>18}",
            f"− Duplicates       :  {inr_str(self.duplicate_paise):>18}",
            f"± Rounding         :  {inr_str(abs(self.rounding_paise)):>18}"
            + (" (debit)" if self.rounding_paise < 0 else " (credit)"),
            f"− Orphan Refunds   :  {inr_str(self.orphan_refund_paise):>18}",
            "─" * 52,
            f"= Expected Bank    :  {inr_str(self.expected_bank_paise):>18}",
            f"  Actual Bank      :  {inr_str(self.actual_bank_paise):>18}",
            f"  Residual         :  {inr_str(abs(self.residual_paise)):>18}"
            + (" ✓ BALANCED" if self.residual_paise == 0 else " ✗ OUT OF BALANCE"),
            "─" * 52,
            f"Platform Rows      : {self.total_platform_rows:>5}",
            f"Bank Rows          : {self.total_bank_rows:>5}",
            f"Matched Pairs      : {self.matched_count:>5}",
            f"Unmatched Platform : {self.unmatched_platform_count:>5}",
            f"Unmatched Bank     : {self.unmatched_bank_count:>5}",
        ]
        return lines


# ---------------------------------------------------------------------------
# Label constants — single source of truth used by matcher & classifier
# ---------------------------------------------------------------------------

LABEL_MATCHED = "MATCHED"
LABEL_IN_TRANSIT = "IN_TRANSIT"
LABEL_DUPLICATE_BANK = "DUPLICATE_BANK"
LABEL_ORPHAN_REFUND = "ORPHAN_REFUND"
LABEL_AMOUNT_MISMATCH = "AMOUNT_MISMATCH"
LABEL_MISSING_PSP = "MISSING_PSP"
LABEL_REVERSED = "REVERSED"
LABEL_TIMING_GAP = "TIMING_GAP"
LABEL_UNCLASSIFIED = "UNCLASSIFIED"

ALL_LABELS = [
    LABEL_MATCHED,
    LABEL_IN_TRANSIT,
    LABEL_DUPLICATE_BANK,
    LABEL_ORPHAN_REFUND,
    LABEL_AMOUNT_MISMATCH,
    LABEL_MISSING_PSP,
    LABEL_REVERSED,
    LABEL_TIMING_GAP,
    LABEL_UNCLASSIFIED,
]
