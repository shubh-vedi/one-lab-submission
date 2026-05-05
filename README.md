# 🏦 Payments Reconciliation Engine

> **INR-only · Integer Paise · T+N Settlement Window · Waterfall Matching · Streamlit UI**

A production-grade payments reconciliation engine that matches platform transactions against bank settlement files, classifies every row with an auditable label, and produces a formal reconciliation statement — all without a single float in the matching logic.

---

## 📋 Table of Contents

- [Overview](#overview)
- [Architecture](#architecture)
- [Module Reference](#module-reference)
- [Matching Pipeline](#matching-pipeline)
- [Label Taxonomy](#label-taxonomy)
- [Getting Started](#getting-started)
- [Running Tests](#running-tests)
- [Streamlit UI](#streamlit-ui)
- [Design Constraints](#design-constraints)
- [Extending the System](#extending-the-system)

---

## Overview

The engine reconciles two data sources:

| Source | Description |
|--------|-------------|
| **Platform file** | Transactions recorded on the payment platform (`PlatformTxn`) |
| **Bank file** | Per-transaction settlement lines from the bank (`BankTxn`) |

Every row on both sides receives exactly **one label**. The reconciliation statement derives:

```
expected_bank = platform_total + in_transit − duplicates ± rounding − orphan_refunds
residual      = actual_bank − expected_bank          ← 0 = BALANCED
```

---

## Architecture

```
┌──────────────┐     ┌───────────────┐     ┌──────────────┐
│  generator   │────▶│    matcher    │────▶│    report    │
│  (seeded RNG)│     │  Tier1→Dedup  │     │  (statement) │
└──────────────┘     │  →Tier3→Class │     └──────────────┘
                     └───────┬───────┘
                             │ audit_log
                     ┌───────▼───────┐     ┌──────────────┐
                     │    scoring    │────▶│    app.py    │
                     │ (confusion    │     │  (Streamlit) │
                     │   matrix)     │     └──────────────┘
                     └───────────────┘
```

---

## Module Reference

| File | Responsibility |
|------|---------------|
| `models.py` | Frozen dataclasses: `PlatformTxn`, `BankTxn`, `MatchResult`, `Reconciliation`. Label constants. Integer-paise helpers. |
| `matcher.py` | Waterfall matcher (Tier 1 + Bank-Dedup + Tier 3) + data-driven classifier |
| `report.py` | Builds the `Reconciliation` statement from `MatchResult` list |
| `generator.py` | Seeded synthetic data generator with data-driven anomaly injection |
| `scoring.py` | Confusion matrix + per-label precision / recall / F1 |
| `app.py` | Streamlit UI — three tabs with `st.cache_data` on generator and matcher |
| `tests/test_matcher.py` | 13 hand-crafted pytest tests covering all anomaly types |
| `conftest.py` | Adds project root to `sys.path` for pytest |

---

## Matching Pipeline

```
Platform Txns ──┐
                ▼
         ┌─────────────┐
         │   TIER 1    │  Exact psp_ref match
         │  (O(n) hash)│  → consumes matched_bank_psp_refs
         └──────┬──────┘
                │ residuals
                ▼
         ┌─────────────┐
         │  BANK-DEDUP │  Structural within-bank-file duplicate detection
         │             │  any remaining bank row whose psp_ref ∈ matched set
         │             │  → DUPLICATE_BANK  (no description-text dependency)
         └──────┬──────┘
                │ non-duplicates
                ▼
         ┌─────────────┐
         │   TIER 3    │  Bucket by (customer_id, amount_paise)
         │             │  Score by 1/(1+Δdays), greedy 1-to-1 assignment
         │             │  sorted (-score, p.txn_id, b.bank_ref) for determinism
         └──────┬──────┘
                │ residuals
                ▼
         ┌─────────────┐
         │ CLASSIFIER  │  Data-driven rules table (append to add a label)
         │             │  ORPHAN_REFUND → REVERSED → MISSING_PSP
         │             │  → IN_TRANSIT → DUPLICATE_BANK → TIMING_GAP
         │             │  → ORPHAN_REFUND(bank) → UNCLASSIFIED
         └─────────────┘
```

### Key design: Bank-side duplicate detection is structural

Duplicates are identified by checking whether a remaining bank row's `psp_ref` was **already consumed by a Tier 1 match** — not by inspecting free-text description fields. This means:

- ✅ Fires for any bank-file duplicate regardless of description wording  
- ✅ Only fires on `platform_txn=None` rows — never crosses file boundaries  
- ✅ No magic-word dependency

---

## Label Taxonomy

| Label | Emoji | Meaning |
|-------|-------|---------|
| `MATCHED` | ✅ | Paired via Tier 1 or Tier 3 |
| `IN_TRANSIT` | 🕐 | Platform recorded; bank not yet settled (within T+N window) |
| `DUPLICATE_BANK` | ⚠️ | Second bank entry for a psp_ref already settled (within-bank-file) |
| `ORPHAN_REFUND` | 💜 | Platform REFUND or bank refund credit with no counterpart |
| `REVERSED` | ↩️ | Platform REVERSAL with no bank settlement |
| `MISSING_PSP` | ❓ | Platform row has no psp_ref; Tier 1 cannot reach it |
| `TIMING_GAP` | ⏳ | Bank-only row dated beyond the settlement window |
| `UNCLASSIFIED` | 🔲 | Fallback — novel row matching no classifier rule |

---

## Getting Started

### Prerequisites

- Python 3.10+
- pip

### Install

```bash
git clone https://github.com/shubh-vedi/one-lab-submission.git
cd one-lab-submission
pip install -r requirements.txt
```

### Run the Streamlit app

```bash
streamlit run app.py
```

Open [http://localhost:8501](http://localhost:8501) in your browser.

---

## Running Tests

```bash
pytest tests/test_matcher.py -v
```

Expected output:

```
13 passed in 0.02s
```

The test suite covers a hand-crafted 10-row scenario (7 platform + 6 bank rows) with every anomaly type:

| Test | Scenario |
|------|---------|
| `test_tier1_exact_match_p1_b1` | TIER1 via exact psp_ref |
| `test_tier3_fuzzy_match_p2_b2` | TIER3 via bucket proximity |
| `test_in_transit_p3` | Platform row within settlement window |
| `test_orphan_refund_p4` | Platform REFUND, no bank match |
| `test_reversed_p5` | Platform REVERSAL, no bank match |
| `test_missing_psp_p6` | Platform row with no psp_ref |
| `test_tier1_p7_b3_matched` | TIER1 match despite duplicate bank row |
| `test_duplicate_bank_b4` | Structural bank-side duplicate |
| `test_timing_gap_b5` | Old bank-only row |
| `test_unclassified_b6` | Mystery bank credit |
| `test_audit_trail_non_empty` | Audit log populated |
| `test_no_row_receives_multiple_results` | No double-assignment |
| `test_total_result_count` | Every row covered exactly once |

---

## Streamlit UI

### Tab 1 — 📊 Data

- Side-by-side platform and bank transaction tables with emoji-labelled `true_label` column  
- Label distribution bar charts for both sides  
- Sidebar controls: seed, row count, T+N window, PSP-ref missing rate, per-anomaly injection rates

### Tab 2 — 🔗 Matching & Audit Trail

- Filterable match-results table (by tier and label)  
- Searchable scrollable audit log showing every decision  
- Tier breakdown bar chart

### Tab 3 — 📋 Report & Scoring

- BALANCED / OUT OF BALANCE banner  
- Full reconciliation statement (monospace)  
- Confusion matrix (rows = true label, columns = predicted label)  
- Per-label precision / recall / F1 table  
- Macro-average F1 metric  
- Label distribution in results

---

## Design Constraints

| Constraint | How enforced |
|------------|-------------|
| **No floats in matching logic** | All amounts are `int` (paise). Dict keys, comparisons, and arithmetic are integer-only. The only `float` values are the date-proximity score (sort key only) and display-layer conversions via `paise_to_inr()`. |
| **Every row receives a label** | Classifier fallback rule `(LABEL_UNCLASSIFIED, lambda *_: True)` is always last in the rules table. |
| **Every match decision logged** | `audit_log: list[str]` is populated at every tier and classifier decision and passed through to the UI. |
| **Duplicates are within-bank-file only** | `DUPLICATE_BANK` can only be assigned to rows where `platform_txn is None`. The structural dedup pass checks `psp_ref ∈ matched_bank_psp_refs` — it cannot fire on a row already matched to a platform row. |
| **Data-driven classifier** | New labels are added by appending one tuple to `CLASSIFIER_RULES`. No if-else edits. |
| **Data-driven generator** | Anomaly dispatch uses `ANOMALY_HANDLER_MAP[label]`. No `if anomaly_type == ...` branches. |
| **Deterministic Tier 3** | Greedy assignment sorted by `(-score, p.txn_id, b.bank_ref)` ensures identical output for identical input regardless of insertion order. |

---

## Extending the System

### Add a new label

1. Add constant to `models.py` → `ALL_LABELS`  
2. Append rule to `CLASSIFIER_RULES` in `matcher.py`  
3. Append handler to `ANOMALY_HANDLER_MAP` in `generator.py`  
4. Add emoji to `LABEL_EMOJI_MAP` in `app.py`

### Add Tier 2 (fuzzy PSP matching)

Insert a `_tier2_match()` call between the Bank-Dedup pass and Tier 3 in `run_matcher()`.

### Export results as CSV

```python
st.download_button("Download CSV", results_to_df(match_results).to_csv(), "results.csv")
```

---

## Project Structure

```
onelab-submission/
├── models.py           # Domain dataclasses + label constants
├── matcher.py          # Waterfall matcher + data-driven classifier
├── report.py           # Reconciliation statement builder
├── generator.py        # Seeded synthetic data generator
├── scoring.py          # Confusion matrix + F1 metrics
├── app.py              # Streamlit UI (3 tabs)
├── conftest.py         # pytest path setup
├── requirements.txt    # Dependencies
└── tests/
    └── test_matcher.py # 13 hand-crafted scenario tests
```

---

## Dependencies

```
streamlit>=1.33.0
pandas>=2.1.0
pytest>=8.0.0
```

No matplotlib, no numpy, no scipy required.

---

*Built for the OneLab submission — Payments Reconciliation Engine challenge.*
