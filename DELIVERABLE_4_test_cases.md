# Test Cases

**File:** `tests/test_matcher.py`  
**Run:** `pytest tests/test_matcher.py -v`  
**Result:** 13 passed in 0.02s

---

## Hand-crafted Scenario

The test dataset is built from scratch — 7 platform rows and 6 bank rows — engineered so every anomaly type appears exactly once. No random generation; every row's expected label is known before the matcher runs.

```
Platform rows:   P1 P2 P3 P4 P5 P6 P7
Bank rows:       B1 B2 B3 B4 B5 B6
```

| Row(s) | Scenario injected | Expected label |
|--------|------------------|----------------|
| P1 ↔ B1 | Same `psp_ref="PSP_EXACT_1"` on both sides | `MATCHED` via TIER1 |
| P2 ↔ B2 | Same `customer_id + amount_paise`, no `psp_ref` | `MATCHED` via TIER3 |
| P3 | Platform date = today, no bank entry | `IN_TRANSIT` |
| P4 | `txn_type="REFUND"`, no bank entry | `ORPHAN_REFUND` |
| P5 | `txn_type="REVERSAL"`, no bank entry | `REVERSED` |
| P6 | `psp_ref=None`, unique amount (no TIER3 candidate) | `MISSING_PSP` |
| P7 ↔ B3 | `psp_ref="PSP_DUP_ORIGINAL"` (B3 matches; B4 is extra) | `MATCHED` via TIER1 |
| B4 | Second bank row for same `psp_ref`, description="duplicate…" | `DUPLICATE_BANK` |
| B5 | Bank-only row dated `window + 5` days in the past | `TIMING_GAP` |
| B6 | Bank-only, unique amount, description="Mystery credit" | `UNCLASSIFIED` |

---

## Test List

| # | Test name | What it asserts |
|---|-----------|----------------|
| 1 | `test_total_result_count` | Every platform + bank row ID appears in exactly one `MatchResult` |
| 2 | `test_tier1_exact_match_p1_b1` | P1↔B1 → `label=MATCHED`, `match_tier="TIER1"` |
| 3 | `test_tier3_fuzzy_match_p2_b2` | P2↔B2 → `label=MATCHED`, `match_tier="TIER3"` |
| 4 | `test_in_transit_p3` | (P3, None) → `label=IN_TRANSIT` |
| 5 | `test_orphan_refund_p4` | (P4, None) → `label=ORPHAN_REFUND` |
| 6 | `test_reversed_p5` | (P5, None) → `label=REVERSED` |
| 7 | `test_missing_psp_p6` | (P6, None) → `label=MISSING_PSP` |
| 8 | `test_tier1_p7_b3_matched` | P7↔B3 → `label=MATCHED`, `match_tier="TIER1"` (despite B4 existing) |
| 9 | `test_duplicate_bank_b4` | (None, B4) → `label=DUPLICATE_BANK` |
| 10 | `test_timing_gap_b5` | (None, B5) → `label=TIMING_GAP` |
| 11 | `test_unclassified_b6` | (None, B6) → `label=UNCLASSIFIED` |
| 12 | `test_audit_trail_non_empty` | `audit_log` list is non-empty after `run_matcher()` |
| 13 | `test_no_row_receives_multiple_results` | No `txn_id` or `bank_ref` appears in more than one result |

---

## Key Design Decisions Tested

**Rule ordering (tests 4–7):** The classifier rules table must check `ORPHAN_REFUND` (REFUND type), `REVERSED` (REVERSAL type), and `MISSING_PSP` (no psp_ref) *before* the `IN_TRANSIT` date-window catch-all — otherwise all three collapse into `IN_TRANSIT`. Tests 5, 6, 7 would fail if rule order is wrong.

**No double-assignment (test 13):** Each row on both sides must appear in exactly one `MatchResult`. Greedy Tier 3 assignment uses `used_platform` and `used_bank` sets to enforce this.

**Structural duplicate detection (test 9):** B4 has the same `psp_ref` as B3. After Tier 1 consumes B3, the bank-dedup pass checks `psp_ref ∈ matched_bank_psp_refs` and labels B4 as `DUPLICATE_BANK` — not by inspecting description text, but structurally.
