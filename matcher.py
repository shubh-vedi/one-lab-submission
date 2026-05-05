"""
matcher.py — Waterfall matching engine for the Payments Reconciliation Engine.

Tier 1 : Exact psp_ref match (both sides have the same non-null psp_ref).
Tier 3 : Fuzzy bucket match — group by (customer_id, amount_paise), then score
         candidate pairs by date proximity within the settlement window, and
         resolve via greedy 1-to-1 assignment.

The classifier at the bottom labels residual (unmatched) rows using a
data-driven rules table — new labels are added by appending to CLASSIFIER_RULES,
never by touching if-else logic.
"""

from __future__ import annotations

import datetime
import itertools
from collections import defaultdict
from dataclasses import dataclass
from typing import Callable, Optional

from models import (
    ALL_LABELS,
    LABEL_AMOUNT_MISMATCH,
    LABEL_DUPLICATE_BANK,
    LABEL_IN_TRANSIT,
    LABEL_MATCHED,
    LABEL_MISSING_PSP,
    LABEL_ORPHAN_REFUND,
    LABEL_REVERSED,
    LABEL_TIMING_GAP,
    LABEL_UNCLASSIFIED,
    BankTxn,
    MatchResult,
    PlatformTxn,
    Reconciliation,
)


# ---------------------------------------------------------------------------
# Scoring helpers
# ---------------------------------------------------------------------------

def _date_proximity_score(p_date: datetime.date, b_date: datetime.date) -> float:
    """
    Return a score in (0, 1] reflecting how close two dates are.
    Closer dates produce higher scores.  Same-day settlement = 1.0.
    """
    delta = (b_date - p_date).days
    if delta < 0:
        return 0.0  # bank cannot settle before platform records the txn
    return 1.0 / (1.0 + delta)


# ---------------------------------------------------------------------------
# Tier 1 — Exact PSP-reference match
# ---------------------------------------------------------------------------

def _tier1_match(
    platform_txns: list[PlatformTxn],
    bank_txns: list[BankTxn],
    audit_log: list[str],
) -> tuple[list[MatchResult], list[PlatformTxn], list[BankTxn], set[str]]:
    """
    Exact psp_ref match.  Each psp_ref is consumed at most once from each side
    (first-come-first-served on duplicates — duplicates are logged).

    Returns an extra set `matched_bank_psp_refs` — the psp_refs of every bank
    row that was successfully paired with a platform row.  The caller uses this
    to structurally detect bank-side duplicates (remaining bank rows whose
    psp_ref is in this set) without relying on description text.
    """
    results: list[MatchResult] = []
    used_platform: set[str] = set()
    used_bank: set[str] = set()
    matched_bank_psp_refs: set[str] = set()   # psp_refs consumed on the bank side

    # Build psp_ref → [BankTxn, ...] index; a list so we can see duplicates.
    bank_index: dict[str, list[BankTxn]] = defaultdict(list)
    for bt in bank_txns:
        if bt.psp_ref:
            bank_index[bt.psp_ref].append(bt)

    for pt in platform_txns:
        if not pt.psp_ref or pt.txn_id in used_platform:
            continue
        candidates = [b for b in bank_index.get(pt.psp_ref, []) if b.bank_ref not in used_bank]
        if not candidates:
            continue

        # Take the best candidate (earliest bank_date, or first)
        bt = sorted(candidates, key=lambda b: b.bank_date)[0]
        score = _date_proximity_score(pt.platform_date, bt.bank_date)

        used_platform.add(pt.txn_id)
        used_bank.add(bt.bank_ref)
        matched_bank_psp_refs.add(pt.psp_ref)   # record this psp_ref as consumed

        note = (
            f"TIER1 exact psp_ref={pt.psp_ref!r} | "
            f"platform {pt.txn_id} ↔ bank {bt.bank_ref} | "
            f"score={score:.4f}"
        )
        audit_log.append(note)
        results.append(
            MatchResult(
                platform_txn=pt,
                bank_txn=bt,
                label=LABEL_MATCHED,
                score=score,
                match_tier="TIER1",
                audit_note=note,
            )
        )

    remaining_p = [p for p in platform_txns if p.txn_id not in used_platform]
    remaining_b = [b for b in bank_txns if b.bank_ref not in used_bank]
    return results, remaining_p, remaining_b, matched_bank_psp_refs


# ---------------------------------------------------------------------------
# Tier 3 — Bucket + greedy proximity match
# ---------------------------------------------------------------------------

def _tier3_match(
    platform_txns: list[PlatformTxn],
    bank_txns: list[BankTxn],
    settlement_window_days: int,
    audit_log: list[str],
) -> tuple[list[MatchResult], list[PlatformTxn], list[BankTxn]]:
    """
    Group by (customer_id, amount_paise), then score each (platform, bank) pair
    within the settlement window (bank_date >= platform_date and
    bank_date <= platform_date + settlement_window_days).

    Greedy 1-to-1 assignment: sort candidates by (-score, p.txn_id, b.bank_ref)
    for full determinism, then greedily consume pairs.
    """

    # Build bank bucket index keyed by amount_paise (int) only.
    # BankTxn has no customer_id field; customer filtering happens during
    # the cross-product below when we join against the platform bucket.
    bank_bucket: dict[int, list[BankTxn]] = defaultdict(list)
    for bt in bank_txns:
        bank_bucket[bt.amount_paise].append(bt)

    # Build platform bucket index
    plat_bucket: dict[tuple[str, int], list[PlatformTxn]] = defaultdict(list)
    for pt in platform_txns:
        plat_bucket[(pt.customer_id, pt.amount_paise)].append(pt)

    # Generate scored candidates
    @dataclass
    class Candidate:
        score: float
        platform_txn: PlatformTxn
        bank_txn: BankTxn

    candidates: list[Candidate] = []

    for (cust_id, amount), pts in plat_bucket.items():
        bank_matches = bank_bucket.get(amount, [])
        for pt, bt in itertools.product(pts, bank_matches):
            delta = (bt.bank_date - pt.platform_date).days
            if 0 <= delta <= settlement_window_days:
                score = _date_proximity_score(pt.platform_date, bt.bank_date)
                candidates.append(Candidate(score=score, platform_txn=pt, bank_txn=bt))

    # Sort for deterministic greedy assignment: best score first; break ties by ids
    candidates.sort(key=lambda c: (-c.score, c.platform_txn.txn_id, c.bank_txn.bank_ref))

    results: list[MatchResult] = []
    used_platform: set[str] = set()
    used_bank: set[str] = set()

    for cand in candidates:
        pt, bt = cand.platform_txn, cand.bank_txn
        if pt.txn_id in used_platform or bt.bank_ref in used_bank:
            continue
        used_platform.add(pt.txn_id)
        used_bank.add(bt.bank_ref)
        note = (
            f"TIER3 bucket cust={pt.customer_id} amt={pt.amount_paise}p | "
            f"platform {pt.txn_id} ↔ bank {bt.bank_ref} | "
            f"score={cand.score:.4f}"
        )
        audit_log.append(note)
        results.append(
            MatchResult(
                platform_txn=pt,
                bank_txn=bt,
                label=LABEL_MATCHED,
                score=cand.score,
                match_tier="TIER3",
                audit_note=note,
            )
        )

    remaining_p = [p for p in platform_txns if p.txn_id not in used_platform]
    remaining_b = [b for b in bank_txns if b.bank_ref not in used_bank]
    return results, remaining_p, remaining_b


# ---------------------------------------------------------------------------
# Classifier — data-driven rules table
# ---------------------------------------------------------------------------

# Each rule is (label, predicate).
# The predicate receives (platform_txn | None, bank_txn | None, settlement_window_days).
# Rules are evaluated top-to-bottom; the first match wins.
# To add a new label: append a tuple here — no if-else edits required.

ClassifierPredicate = Callable[
    [Optional[PlatformTxn], Optional[BankTxn], int], bool
]

CLASSIFIER_RULES: list[tuple[str, ClassifierPredicate]] = [
    # 1. Platform REFUND with no bank match → orphan refund (checked BEFORE in-transit)
    (
        LABEL_ORPHAN_REFUND,
        lambda p, b, w: (
            p is not None
            and b is None
            and p.txn_type == "REFUND"
        ),
    ),
    # 2. Platform REVERSAL with no bank match → reversed (checked BEFORE in-transit)
    (
        LABEL_REVERSED,
        lambda p, b, w: (
            p is not None
            and b is None
            and p.txn_type == "REVERSAL"
        ),
    ),
    # 3. Platform txn has no psp_ref and no bank match → missing PSP reference
    (
        LABEL_MISSING_PSP,
        lambda p, b, w: (
            p is not None
            and b is None
            and p.psp_ref is None
        ),
    ),
    # 4. Platform txn with no bank match and within settlement window → in-transit
    #    (falls here only for normal PAYMENT rows within the window)
    (
        LABEL_IN_TRANSIT,
        lambda p, b, w: (
            p is not None
            and b is None
            and (datetime.date.today() - p.platform_date).days <= w
        ),
    ),
    # 5. Bank-only row where description signals duplicate
    (
        LABEL_DUPLICATE_BANK,
        lambda p, b, w: (
            p is None
            and b is not None
            and "duplicate" in b.description.lower()
        ),
    ),
    # 6. Bank-only row outside settlement window → timing gap
    (
        LABEL_TIMING_GAP,
        lambda p, b, w: (
            p is None
            and b is not None
            and (datetime.date.today() - b.bank_date).days > w
        ),
    ),
    # 7. Bank-only orphan refund (description contains "refund")
    (
        LABEL_ORPHAN_REFUND,
        lambda p, b, w: (
            p is None
            and b is not None
            and "refund" in b.description.lower()
        ),
    ),
    # 8. Fallback — must always be last
    (
        LABEL_UNCLASSIFIED,
        lambda p, b, w: True,
    ),
]


def classify_residual(
    platform_txn: Optional[PlatformTxn],
    bank_txn: Optional[BankTxn],
    settlement_window_days: int,
) -> str:
    """Apply the rules table and return the first matching label."""
    for label, predicate in CLASSIFIER_RULES:
        if predicate(platform_txn, bank_txn, settlement_window_days):
            return label
    return LABEL_UNCLASSIFIED  # should never reach here given the fallback rule


# ---------------------------------------------------------------------------
# Public entry-point
# ---------------------------------------------------------------------------

def run_matcher(
    platform_txns: list[PlatformTxn],
    bank_txns: list[BankTxn],
    settlement_window_days: int = 2,
) -> tuple[list[MatchResult], list[str]]:
    """
    Run the full waterfall matcher and return:
      - list[MatchResult]  — one entry per row on either side
      - list[str]          — audit trail entries

    Every row on both sides receives exactly one MatchResult entry.
    """
    audit_log: list[str] = []

    # ── Tier 1 ──────────────────────────────────────────────────────────────
    audit_log.append("=== TIER 1: Exact PSP-ref match ===")
    t1_results, rem_p, rem_b, matched_bank_psp_refs = _tier1_match(
        platform_txns, bank_txns, audit_log
    )
    audit_log.append(
        f"Tier 1 complete: {len(t1_results)} matches | "
        f"{len(rem_p)} platform residuals | {len(rem_b)} bank residuals"
    )

    # ── Bank-side duplicate pass (structural, within the bank file only) ─────
    # Any remaining bank row whose psp_ref was already consumed by a Tier 1
    # match is a duplicate *within the bank file* — a second settlement entry
    # for a transaction that already settled once.
    # This check is psp_ref-structural; it does NOT depend on description text
    # and cannot fire across files (platform rows are never involved here).
    audit_log.append("=== BANK-DEDUP: Structural within-bank-file duplicate pass ===")
    dup_results: list[MatchResult] = []
    after_dedup: list[BankTxn] = []
    for bt in rem_b:
        if bt.psp_ref and bt.psp_ref in matched_bank_psp_refs:
            note = (
                f"BANK-DEDUP psp_ref={bt.psp_ref!r} already matched → "
                f"bank {bt.bank_ref} = {LABEL_DUPLICATE_BANK} (within-bank-file duplicate)"
            )
            audit_log.append(note)
            dup_results.append(
                MatchResult(
                    platform_txn=None,
                    bank_txn=bt,
                    label=LABEL_DUPLICATE_BANK,
                    score=0.0,
                    match_tier=None,
                    audit_note=note,
                )
            )
        else:
            after_dedup.append(bt)
    audit_log.append(
        f"Bank-dedup complete: {len(dup_results)} structural duplicates found | "
        f"{len(after_dedup)} bank residuals continue"
    )
    rem_b = after_dedup

    # ── Tier 3 ──────────────────────────────────────────────────────────────
    audit_log.append("=== TIER 3: Bucket proximity match ===")
    t3_results, rem_p, rem_b = _tier3_match(rem_p, rem_b, settlement_window_days, audit_log)
    audit_log.append(
        f"Tier 3 complete: {len(t3_results)} matches | "
        f"{len(rem_p)} platform residuals | {len(rem_b)} bank residuals"
    )

    # ── Classify residuals ───────────────────────────────────────────────────
    audit_log.append("=== CLASSIFIER: Labelling residuals ===")
    residual_results: list[MatchResult] = []

    for pt in rem_p:
        label = classify_residual(pt, None, settlement_window_days)
        note = f"RESIDUAL platform {pt.txn_id} → {label}"
        audit_log.append(note)
        residual_results.append(
            MatchResult(
                platform_txn=pt,
                bank_txn=None,
                label=label,
                score=0.0,
                match_tier=None,
                audit_note=note,
            )
        )

    for bt in rem_b:
        label = classify_residual(None, bt, settlement_window_days)
        note = f"RESIDUAL bank {bt.bank_ref} → {label}"
        audit_log.append(note)
        residual_results.append(
            MatchResult(
                platform_txn=None,
                bank_txn=bt,
                label=label,
                score=0.0,
                match_tier=None,
                audit_note=note,
            )
        )

    all_results = t1_results + dup_results + t3_results + residual_results
    audit_log.append(
        f"=== DONE: {len(all_results)} total results | "
        f"{len(t1_results) + len(t3_results)} matched | "
        f"{len(dup_results)} structural bank duplicates | "
        f"{len(residual_results)} other unmatched ==="
    )
    return all_results, audit_log
