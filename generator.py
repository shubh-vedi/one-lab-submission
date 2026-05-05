"""
generator.py — Seeded random data generator for the Payments Reconciliation Engine.

Generates PlatformTxn and BankTxn rows with configurable anomaly injection.
The generator is fully data-driven: anomaly types and their probabilities are
declared in a table (ANOMALY_TABLE); no hardcoded `if anomaly_type == "..."` branches.

Every generated row carries a _true_label so that scoring.py can evaluate
matcher accuracy.

Usage
-----
    from generator import GeneratorConfig, generate_data
    cfg = GeneratorConfig(seed=42, num_rows=500)
    platform_txns, bank_txns = generate_data(cfg)
"""

from __future__ import annotations

import datetime
import random
import uuid
from dataclasses import dataclass, field
from typing import Callable, Optional

from models import (
    ALL_LABELS,
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


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@dataclass
class AnomalyRate:
    """Probability config for a single anomaly type."""
    label: str
    probability: float          # fraction of rows affected (0.0 – 1.0)


@dataclass
class GeneratorConfig:
    seed: int = 42
    num_rows: int = 200
    period_start: datetime.date = field(
        default_factory=lambda: datetime.date.today() - datetime.timedelta(days=30)
    )
    period_end: datetime.date = field(
        default_factory=lambda: datetime.date.today() - datetime.timedelta(days=1)
    )
    settlement_window_days: int = 2
    psp_ref_missing_rate: float = 0.30   # fraction of platform rows without psp_ref

    # Anomaly injection table — data-driven, no hardcoded branches
    anomaly_rates: list[AnomalyRate] = field(default_factory=lambda: [
        AnomalyRate(label=LABEL_IN_TRANSIT,     probability=0.05),
        AnomalyRate(label=LABEL_DUPLICATE_BANK, probability=0.03),
        AnomalyRate(label=LABEL_ORPHAN_REFUND,  probability=0.04),
        AnomalyRate(label=LABEL_REVERSED,       probability=0.03),
        AnomalyRate(label=LABEL_MISSING_PSP,    probability=0.04),
        AnomalyRate(label=LABEL_TIMING_GAP,     probability=0.02),
        AnomalyRate(label=LABEL_UNCLASSIFIED,   probability=0.01),
    ])

    def __post_init__(self) -> None:
        if self.period_start >= self.period_end:
            raise ValueError("period_start must be before period_end")
        total = sum(ar.probability for ar in self.anomaly_rates)
        if total >= 1.0:
            raise ValueError(
                f"Sum of anomaly probabilities ({total:.2f}) must be < 1.0 "
                "(the remainder becomes the MATCHED rate)."
            )


# ---------------------------------------------------------------------------
# Anomaly handler table
# ---------------------------------------------------------------------------
# Each handler is a callable:
#   (rng, platform_txn, settlement_window_days) → (PlatformTxn | None, BankTxn | None)
#
# Handlers modify/omit rows to simulate the anomaly.  The handler returns:
#   - (modified_platform_txn, bank_txn) for most cases
#   - (platform_txn, None)              for platform-only anomalies
#   - (None, bank_txn)                  for bank-only anomalies

AnomalyHandler = Callable[
    ["random.Random", PlatformTxn, int],
    tuple[Optional[PlatformTxn], Optional[BankTxn]],
]


def _make_bank_from_platform(
    rng: random.Random, pt: PlatformTxn, days_offset: int
) -> BankTxn:
    """Helper: create a normal BankTxn for a PlatformTxn."""
    return BankTxn(
        bank_ref=f"B-{uuid.uuid4().hex[:8].upper()}",
        amount_paise=pt.amount_paise,
        bank_date=pt.platform_date + datetime.timedelta(days=days_offset),
        psp_ref=pt.psp_ref,
        description=f"Settlement {pt.psp_ref or pt.customer_id}",
        _true_label=LABEL_MATCHED,
    )


def _handle_matched(rng: random.Random, pt: PlatformTxn, window: int):
    """Normal match: bank arrives within the settlement window."""
    offset = rng.randint(0, window)
    bt = _make_bank_from_platform(rng, pt, offset)
    matched_pt = PlatformTxn(
        txn_id=pt.txn_id, customer_id=pt.customer_id,
        amount_paise=pt.amount_paise, platform_date=pt.platform_date,
        psp_ref=pt.psp_ref, txn_type=pt.txn_type,
        _true_label=LABEL_MATCHED,
    )
    return matched_pt, BankTxn(
        bank_ref=bt.bank_ref, amount_paise=bt.amount_paise,
        bank_date=bt.bank_date, psp_ref=bt.psp_ref,
        description=bt.description, _true_label=LABEL_MATCHED,
    )


def _handle_in_transit(rng: random.Random, pt: PlatformTxn, window: int):
    """Platform recorded; bank hasn't settled yet."""
    modified_pt = PlatformTxn(
        txn_id=pt.txn_id, customer_id=pt.customer_id,
        amount_paise=pt.amount_paise,
        platform_date=datetime.date.today(),   # very recent — within window
        psp_ref=pt.psp_ref, txn_type=pt.txn_type,
        _true_label=LABEL_IN_TRANSIT,
    )
    return modified_pt, None


def _handle_duplicate_bank(rng: random.Random, pt: PlatformTxn, window: int):
    """Return a MATCHED pair plus an extra duplicate BankTxn.
    The generator appends both; the duplicate is tagged with _true_label."""
    offset = rng.randint(0, window)
    normal_bt = _make_bank_from_platform(rng, pt, offset)
    # Mark platform as MATCHED
    matched_pt = PlatformTxn(
        txn_id=pt.txn_id, customer_id=pt.customer_id,
        amount_paise=pt.amount_paise, platform_date=pt.platform_date,
        psp_ref=pt.psp_ref, txn_type=pt.txn_type,
        _true_label=LABEL_MATCHED,
    )
    # Duplicate bank row — description contains "duplicate" so the classifier picks it up
    dup_bt = BankTxn(
        bank_ref=f"DUP-{uuid.uuid4().hex[:6].upper()}",
        amount_paise=pt.amount_paise,
        bank_date=normal_bt.bank_date,
        psp_ref=pt.psp_ref,
        description=f"duplicate entry {pt.psp_ref or pt.customer_id}",
        _true_label=LABEL_DUPLICATE_BANK,
    )
    return matched_pt, (normal_bt, dup_bt)   # tuple signals two bank rows


def _handle_orphan_refund(rng: random.Random, pt: PlatformTxn, window: int):
    """Platform REFUND with no corresponding bank credit."""
    orphan_pt = PlatformTxn(
        txn_id=pt.txn_id, customer_id=pt.customer_id,
        amount_paise=pt.amount_paise, platform_date=pt.platform_date,
        psp_ref=pt.psp_ref, txn_type="REFUND",
        _true_label=LABEL_ORPHAN_REFUND,
    )
    return orphan_pt, None


def _handle_reversed(rng: random.Random, pt: PlatformTxn, window: int):
    """Platform REVERSAL with no bank settlement."""
    rev_pt = PlatformTxn(
        txn_id=pt.txn_id, customer_id=pt.customer_id,
        amount_paise=pt.amount_paise, platform_date=pt.platform_date,
        psp_ref=pt.psp_ref, txn_type="REVERSAL",
        _true_label=LABEL_REVERSED,
    )
    return rev_pt, None


def _handle_missing_psp(rng: random.Random, pt: PlatformTxn, window: int):
    """Strip psp_ref; no bank match possible via TIER1."""
    no_psp_pt = PlatformTxn(
        txn_id=pt.txn_id, customer_id=pt.customer_id,
        amount_paise=pt.amount_paise, platform_date=pt.platform_date,
        psp_ref=None,  # missing!
        txn_type=pt.txn_type,
        _true_label=LABEL_MISSING_PSP,
    )
    return no_psp_pt, None


def _handle_timing_gap(rng: random.Random, pt: PlatformTxn, window: int):
    """Bank-only row dated far in the past — no platform row."""
    old_bt = BankTxn(
        bank_ref=f"GAP-{uuid.uuid4().hex[:6].upper()}",
        amount_paise=pt.amount_paise,
        bank_date=pt.platform_date - datetime.timedelta(days=window + rng.randint(3, 10)),
        psp_ref=None,
        description=f"Old settlement {pt.customer_id}",
        _true_label=LABEL_TIMING_GAP,
    )
    return None, old_bt   # no platform row for this anomaly type


def _handle_unclassified(rng: random.Random, pt: PlatformTxn, window: int):
    """Novel bank credit that matches no classifier rule."""
    mystery_bt = BankTxn(
        bank_ref=f"UNK-{uuid.uuid4().hex[:6].upper()}",
        amount_paise=rng.randint(10_000, 500_000),
        bank_date=datetime.date.today(),
        psp_ref=None,
        description="Mystery credit",
        _true_label=LABEL_UNCLASSIFIED,
    )
    return None, mystery_bt


# Map label → handler function (data-driven; order matches AnomalyRate table)
ANOMALY_HANDLER_MAP: dict[str, AnomalyHandler] = {
    LABEL_MATCHED:        _handle_matched,
    LABEL_IN_TRANSIT:     _handle_in_transit,
    LABEL_DUPLICATE_BANK: _handle_duplicate_bank,
    LABEL_ORPHAN_REFUND:  _handle_orphan_refund,
    LABEL_REVERSED:       _handle_reversed,
    LABEL_MISSING_PSP:    _handle_missing_psp,
    LABEL_TIMING_GAP:     _handle_timing_gap,
    LABEL_UNCLASSIFIED:   _handle_unclassified,
}


# ---------------------------------------------------------------------------
# Core generator
# ---------------------------------------------------------------------------

def _random_date(rng: random.Random, start: datetime.date, end: datetime.date) -> datetime.date:
    delta = (end - start).days
    return start + datetime.timedelta(days=rng.randint(0, max(0, delta - 1)))


def _pick_anomaly_label(rng: random.Random, cfg: GeneratorConfig) -> str:
    """Data-driven label selection based on probability config."""
    roll = rng.random()
    cumulative = 0.0
    for ar in cfg.anomaly_rates:
        cumulative += ar.probability
        if roll < cumulative:
            return ar.label
    return LABEL_MATCHED   # remainder probability → normal match


def generate_data(cfg: GeneratorConfig) -> tuple[list[PlatformTxn], list[BankTxn]]:
    """
    Generate synthetic platform and bank transactions.

    Returns
    -------
    (platform_txns, bank_txns) — two independent lists ready for the matcher.
    """
    rng = random.Random(cfg.seed)

    customer_pool = [f"CUST_{i:04d}" for i in range(max(10, cfg.num_rows // 10))]
    psp_counter = 0

    platform_txns: list[PlatformTxn] = []
    bank_txns: list[BankTxn] = []

    for i in range(cfg.num_rows):
        psp_counter += 1
        # Decide whether this row has a psp_ref
        has_psp = rng.random() >= cfg.psp_ref_missing_rate
        psp_ref = f"PSP-{psp_counter:06d}" if has_psp else None

        # Build a base platform transaction
        platform_date = _random_date(rng, cfg.period_start, cfg.period_end)
        base_pt = PlatformTxn(
            txn_id=f"P-{uuid.uuid4().hex[:8].upper()}",
            customer_id=rng.choice(customer_pool),
            amount_paise=rng.randint(1_000, 1_000_000),   # ₹10 – ₹10,000
            platform_date=platform_date,
            psp_ref=psp_ref,
            txn_type="PAYMENT",
            _true_label=LABEL_MATCHED,  # will be overwritten by handler
        )

        # Pick anomaly label and dispatch to the corresponding handler
        label = _pick_anomaly_label(rng, cfg)
        handler = ANOMALY_HANDLER_MAP.get(label, _handle_matched)
        result_pt, result_bt = handler(rng, base_pt, cfg.settlement_window_days)

        if result_pt is not None:
            platform_txns.append(result_pt)

        if result_bt is not None:
            # Handler may return a tuple of two bank rows (e.g., duplicate scenario)
            if isinstance(result_bt, tuple):
                bank_txns.extend(result_bt)
            else:
                bank_txns.append(result_bt)

    return platform_txns, bank_txns
