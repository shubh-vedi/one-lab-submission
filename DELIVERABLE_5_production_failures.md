# Production Failure Analysis

Three things this reconciliation engine would get wrong at production scale.

---

## 1. Tier 3 creates silent false matches on recurring amounts

The bucket key for fuzzy matching is `(customer_id, amount_paise)`. In production, a single customer paying a ₹999 monthly subscription generates multiple platform rows and multiple bank rows with identical keys inside the same settlement window. The greedy date-proximity sort picks *some* pairing, but the choice is arbitrary — November's bank row can be matched to October's platform row. Because both sides of the ledger are consumed the residual stays zero, so the mis-attribution is invisible in the statement and only surfaces during a manual dispute months later.

---

## 2. The settlement window counts calendar days, not banking days

`timedelta(days=N)` treats Saturdays, Sundays, and public holidays as valid settlement days. Banks do not settle on non-working days. A transaction recorded on Friday is correctly labelled `IN_TRANSIT` on Friday and Saturday, but by Monday morning it has aged past T+2 and is reclassified as `UNCLASSIFIED` or `TIMING_GAP` — a legitimate pending transaction turns into a false exception. Every long weekend produces a wave of spurious alerts that operations teams must manually clear before the real anomalies can be seen.

---

## 3. Duplicate detection is blind to psp_ref-free bank duplicates

The structural bank-dedup pass catches duplicates only when a `psp_ref` was already consumed by a Tier 1 match. Older core-banking systems frequently emit duplicate settlement lines with no `psp_ref` and identical boilerplate descriptions (e.g. `"NEFT CR"`). Both rows pass the dedup pass, both enter Tier 3, one matches the platform row, and the second lands as `UNCLASSIFIED`. The result: `actual_bank_paise` is silently inflated by the duplicate amount, the reconciliation shows an unexplained credit residual, and the `duplicate_paise` counter in the statement reads zero — giving a false signal that the bank file is clean.
