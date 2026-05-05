"""
scoring.py — Compares matcher output against _true_label ground truth.

Produces a confusion matrix and per-label precision / recall / F1.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Optional

from models import ALL_LABELS, MatchResult


# ---------------------------------------------------------------------------
# Extract true labels
# ---------------------------------------------------------------------------

def _true_label_of(mr: MatchResult) -> Optional[str]:
    """
    Return the ground-truth label for a MatchResult.

    For a MATCHED pair we check the platform side (primary record).
    For unmatched rows we use whichever side is present.
    """
    if mr.platform_txn is not None:
        return mr.platform_txn._true_label
    if mr.bank_txn is not None:
        return mr.bank_txn._true_label
    return None


# ---------------------------------------------------------------------------
# Confusion matrix builder
# ---------------------------------------------------------------------------

def build_confusion_matrix(
    match_results: list[MatchResult],
) -> dict[str, dict[str, int]]:
    """
    Build a confusion matrix:
        matrix[true_label][predicted_label] = count

    Labels are drawn from ALL_LABELS in models.py.
    """
    matrix: dict[str, dict[str, int]] = {
        t: {p: 0 for p in ALL_LABELS} for t in ALL_LABELS
    }
    for mr in match_results:
        true_lbl = _true_label_of(mr) or "UNCLASSIFIED"
        pred_lbl = mr.label
        if true_lbl not in matrix:
            matrix[true_lbl] = {p: 0 for p in ALL_LABELS}
        matrix[true_lbl][pred_lbl] = matrix[true_lbl].get(pred_lbl, 0) + 1
    return matrix


# ---------------------------------------------------------------------------
# Per-label metrics
# ---------------------------------------------------------------------------

def compute_metrics(
    matrix: dict[str, dict[str, int]],
) -> dict[str, dict[str, float]]:
    """
    Compute precision, recall, and F1 for every label.

    Returns
    -------
    dict[label, {"precision": float, "recall": float, "f1": float, "support": int}]
    """
    labels = list(matrix.keys())
    metrics: dict[str, dict[str, float]] = {}

    for lbl in labels:
        tp = matrix[lbl].get(lbl, 0)
        # False Positives: other true-labels predicted as lbl
        fp = sum(matrix[t].get(lbl, 0) for t in labels if t != lbl)
        # False Negatives: lbl true-labels predicted as something else
        fn = sum(matrix[lbl].get(p, 0) for p in labels if p != lbl)
        support = sum(matrix[lbl].values())

        precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
        recall = tp / (tp + fn) if (tp + fn) > 0 else 0.0
        f1 = (
            2 * precision * recall / (precision + recall)
            if (precision + recall) > 0
            else 0.0
        )
        metrics[lbl] = {
            "precision": precision,
            "recall": recall,
            "f1": f1,
            "support": support,
        }
    return metrics


# ---------------------------------------------------------------------------
# Pretty-print helpers
# ---------------------------------------------------------------------------

def print_confusion_matrix(matrix: dict[str, dict[str, int]]) -> None:
    """Print the confusion matrix to stdout."""
    labels = list(matrix.keys())
    col_w = 16

    # Header
    header = f"{'True \\ Predicted':<{col_w}}" + "".join(f"{l:<{col_w}}" for l in labels)
    print(header)
    print("─" * len(header))

    for true_lbl in labels:
        row_vals = "".join(f"{matrix[true_lbl].get(p, 0):<{col_w}}" for p in labels)
        print(f"{true_lbl:<{col_w}}{row_vals}")


def print_metrics(metrics: dict[str, dict[str, float]]) -> None:
    """Print per-label precision / recall / F1."""
    print(f"\n{'Label':<20} {'Precision':>10} {'Recall':>10} {'F1':>10} {'Support':>10}")
    print("─" * 54)
    for lbl, m in metrics.items():
        if m["support"] == 0:
            continue
        print(
            f"{lbl:<20} {m['precision']:>10.3f} {m['recall']:>10.3f} "
            f"{m['f1']:>10.3f} {int(m['support']):>10}"
        )


def score_and_print(match_results: list[MatchResult]) -> dict:
    """
    Full scoring pipeline: build matrix, compute metrics, print both.

    Returns a dict with "matrix" and "metrics" keys for programmatic access.
    """
    matrix = build_confusion_matrix(match_results)
    metrics = compute_metrics(matrix)

    print("\n=== Confusion Matrix (rows=True, cols=Predicted) ===")
    print_confusion_matrix(matrix)
    print("\n=== Per-Label Metrics ===")
    print_metrics(metrics)

    # Macro-average F1 (labels with support > 0 only)
    active = [m for m in metrics.values() if m["support"] > 0]
    macro_f1 = sum(m["f1"] for m in active) / len(active) if active else 0.0
    print(f"\nMacro-average F1 (active labels): {macro_f1:.3f}")

    return {"matrix": matrix, "metrics": metrics, "macro_f1": macro_f1}
