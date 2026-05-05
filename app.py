"""
app.py — Streamlit UI for the Payments Reconciliation Engine.

Three tabs:
  1. Data        — Configure and preview generated data
  2. Matching    — Run the waterfall matcher and inspect the audit trail
  3. Report      — Reconciliation statement + scoring confusion matrix
"""

from __future__ import annotations

import datetime
from typing import Optional

import pandas as pd
import streamlit as st

from generator import AnomalyRate, GeneratorConfig, generate_data
from matcher import run_matcher
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
    MatchResult,
    PlatformTxn,
    inr_str,
    paise_to_inr,
)
from report import build_reconciliation
from scoring import build_confusion_matrix, compute_metrics

# ──────────────────────────────────────────────────────────────────────────────
# Page config
# ──────────────────────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Payments Reconciliation Engine",
    page_icon="🏦",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ──────────────────────────────────────────────────────────────────────────────
# Custom CSS
# ──────────────────────────────────────────────────────────────────────────────
st.markdown(
    """
<style>
    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap');

    html, body, [class*="css"] {
        font-family: 'Inter', sans-serif;
    }

    /* Dark gradient background */
    .stApp {
        background: linear-gradient(135deg, #0f0c29, #302b63, #24243e);
        color: #e0e0e0;
    }

    /* Sidebar */
    [data-testid="stSidebar"] {
        background: rgba(255,255,255,0.04);
        border-right: 1px solid rgba(255,255,255,0.08);
    }

    /* Cards / metric boxes */
    .metric-card {
        background: rgba(255,255,255,0.06);
        border: 1px solid rgba(255,255,255,0.1);
        border-radius: 14px;
        padding: 18px 22px;
        text-align: center;
        transition: transform 0.2s ease, box-shadow 0.2s ease;
    }
    .metric-card:hover {
        transform: translateY(-3px);
        box-shadow: 0 8px 24px rgba(0,0,0,0.4);
    }
    .metric-value {
        font-size: 1.8rem;
        font-weight: 700;
        color: #a78bfa;
    }
    .metric-label {
        font-size: 0.78rem;
        color: #9ca3af;
        margin-top: 4px;
        letter-spacing: 0.04em;
        text-transform: uppercase;
    }

    /* Section headers */
    .section-header {
        font-size: 1.1rem;
        font-weight: 600;
        color: #c4b5fd;
        border-left: 3px solid #7c3aed;
        padding-left: 10px;
        margin: 22px 0 12px 0;
    }

    /* Statement block */
    .statement-block {
        background: rgba(0,0,0,0.35);
        border: 1px solid rgba(124,58,237,0.3);
        border-radius: 12px;
        padding: 20px 24px;
        font-family: 'Courier New', monospace;
        font-size: 0.88rem;
        line-height: 1.9;
        color: #e2e8f0;
    }

    /* Badge pills */
    .badge {
        display: inline-block;
        padding: 2px 10px;
        border-radius: 999px;
        font-size: 0.72rem;
        font-weight: 600;
        letter-spacing: 0.04em;
    }
    .badge-matched   { background:#14532d; color:#86efac; }
    .badge-transit   { background:#1e3a5f; color:#93c5fd; }
    .badge-dup       { background:#451a03; color:#fcd34d; }
    .badge-refund    { background:#4c1d95; color:#ddd6fe; }
    .badge-reversed  { background:#312e81; color:#a5b4fc; }
    .badge-missing   { background:#422006; color:#fbbf24; }
    .badge-timing    { background:#1c1917; color:#a8a29e; }
    .badge-unclass   { background:#1f2937; color:#d1d5db; }

    /* Audit trail log */
    .audit-entry {
        font-family: 'Courier New', monospace;
        font-size: 0.80rem;
        padding: 4px 8px;
        border-left: 2px solid #6d28d9;
        margin-bottom: 3px;
        color: #d1d5db;
    }
    .audit-entry-section {
        color: #a78bfa;
        font-weight: 600;
        border-left-color: #a78bfa;
    }

    /* Tabs */
    .stTabs [data-baseweb="tab-list"] {
        gap: 6px;
        background: transparent;
    }
    .stTabs [data-baseweb="tab"] {
        background: rgba(255,255,255,0.04);
        border-radius: 8px 8px 0 0;
        border: 1px solid rgba(255,255,255,0.08);
        color: #9ca3af;
        font-weight: 500;
    }
    .stTabs [aria-selected="true"] {
        background: rgba(124,58,237,0.25) !important;
        color: #c4b5fd !important;
        border-color: rgba(124,58,237,0.5) !important;
    }

    /* DataFrame */
    [data-testid="stDataFrame"] {
        border-radius: 10px;
        overflow: hidden;
    }

    /* Scrollable audit box */
    .audit-scroll {
        max-height: 420px;
        overflow-y: auto;
        background: rgba(0,0,0,0.3);
        border-radius: 10px;
        padding: 14px;
        border: 1px solid rgba(255,255,255,0.07);
    }

    /* Balanced / unbalanced banner */
    .banner-ok  { background:#14532d; border-radius:10px; padding:14px 20px;
                  color:#86efac; font-weight:700; font-size:1.1rem; }
    .banner-err { background:#7f1d1d; border-radius:10px; padding:14px 20px;
                  color:#fca5a5; font-weight:700; font-size:1.1rem; }
</style>
""",
    unsafe_allow_html=True,
)


# ──────────────────────────────────────────────────────────────────────────────
# Sidebar — global config
# ──────────────────────────────────────────────────────────────────────────────
with st.sidebar:
    st.markdown("## ⚙️ Configuration")
    st.markdown("---")

    seed = st.number_input("Random seed", value=42, min_value=0, step=1)
    num_rows = st.slider("Number of platform rows", 50, 2000, 300, 50)
    settlement_days = st.slider("Settlement window T+N (days)", 1, 7, 2)
    psp_missing_rate = st.slider("PSP-ref missing rate", 0.0, 0.8, 0.30, 0.05)

    st.markdown("---")
    st.markdown("### Anomaly injection rates")
    st.caption("These must sum to < 1.0 (remainder = normal matched rows)")

    anomaly_defaults = {
        LABEL_IN_TRANSIT:     0.05,
        LABEL_DUPLICATE_BANK: 0.03,
        LABEL_ORPHAN_REFUND:  0.04,
        LABEL_REVERSED:       0.03,
        LABEL_MISSING_PSP:    0.04,
        LABEL_TIMING_GAP:     0.02,
        LABEL_UNCLASSIFIED:   0.01,
    }

    anomaly_rates_input: dict[str, float] = {}
    for lbl, default_val in anomaly_defaults.items():
        anomaly_rates_input[lbl] = st.slider(
            lbl,
            0.0, 0.20,
            default_val,
            0.01,
            key=f"anomaly_{lbl}",
        )

    total_anomaly = sum(anomaly_rates_input.values())
    if total_anomaly >= 1.0:
        st.error(f"⚠️ Anomaly rates sum to {total_anomaly:.2f} — must be < 1.0")
    else:
        st.success(f"✔ Normal-match rate: {1 - total_anomaly:.0%}")

    period_start = st.date_input(
        "Period start",
        value=datetime.date.today() - datetime.timedelta(days=30),
    )
    period_end = st.date_input(
        "Period end",
        value=datetime.date.today() - datetime.timedelta(days=1),
    )


# ──────────────────────────────────────────────────────────────────────────────
# Cached computation helpers
# ──────────────────────────────────────────────────────────────────────────────

@st.cache_data(show_spinner="Generating synthetic data…")
def cached_generate(
    seed: int,
    num_rows: int,
    period_start: datetime.date,
    period_end: datetime.date,
    settlement_days: int,
    psp_missing_rate: float,
    anomaly_rates_json: str,  # serialised so cache key works
) -> tuple[list[PlatformTxn], list[BankTxn]]:
    import json

    anomaly_rates_dict: dict[str, float] = json.loads(anomaly_rates_json)
    cfg = GeneratorConfig(
        seed=seed,
        num_rows=num_rows,
        period_start=period_start,
        period_end=period_end,
        settlement_window_days=settlement_days,
        psp_ref_missing_rate=psp_missing_rate,
        anomaly_rates=[
            AnomalyRate(label=lbl, probability=prob)
            for lbl, prob in anomaly_rates_dict.items()
        ],
    )
    return generate_data(cfg)


@st.cache_data(show_spinner="Running waterfall matcher…")
def cached_match(
    platform_txns: tuple,  # tuples for hashability
    bank_txns: tuple,
    settlement_days: int,
) -> tuple[list[MatchResult], list[str]]:
    return run_matcher(list(platform_txns), list(bank_txns), settlement_days)


# ──────────────────────────────────────────────────────────────────────────────
# Helpers: build DataFrames
# ──────────────────────────────────────────────────────────────────────────────

LABEL_BADGE_MAP = {
    LABEL_MATCHED:        "badge-matched",
    LABEL_IN_TRANSIT:     "badge-transit",
    LABEL_DUPLICATE_BANK: "badge-dup",
    LABEL_ORPHAN_REFUND:  "badge-refund",
    LABEL_REVERSED:       "badge-reversed",
    LABEL_MISSING_PSP:    "badge-missing",
    LABEL_TIMING_GAP:     "badge-timing",
    LABEL_UNCLASSIFIED:   "badge-unclass",
}

LABEL_COLOR_MAP = {
    LABEL_MATCHED:        "#86efac",
    LABEL_IN_TRANSIT:     "#93c5fd",
    LABEL_DUPLICATE_BANK: "#fcd34d",
    LABEL_ORPHAN_REFUND:  "#ddd6fe",
    LABEL_REVERSED:       "#a5b4fc",
    LABEL_MISSING_PSP:    "#fbbf24",
    LABEL_TIMING_GAP:     "#a8a29e",
    LABEL_UNCLASSIFIED:   "#d1d5db",
}
# Emoji indicators — visual label differentiation without pandas Styler
LABEL_EMOJI_MAP = {
    LABEL_MATCHED:        "✅",
    LABEL_IN_TRANSIT:     "🕐",
    LABEL_DUPLICATE_BANK: "⚠️",
    LABEL_ORPHAN_REFUND:  "💜",
    LABEL_REVERSED:       "↩️",
    LABEL_MISSING_PSP:    "❓",
    LABEL_TIMING_GAP:     "⏳",
    LABEL_UNCLASSIFIED:   "🔲",
}


def platform_to_df(txns: list[PlatformTxn]) -> pd.DataFrame:
    rows = []
    for t in txns:
        emoji = LABEL_EMOJI_MAP.get(t._true_label, "")
        rows.append({
            "txn_id": t.txn_id,
            "customer_id": t.customer_id,
            "amount (₹)": float(paise_to_inr(t.amount_paise)),
            "platform_date": t.platform_date.isoformat(),
            "psp_ref": t.psp_ref or "—",
            "txn_type": t.txn_type,
            "true_label": f"{emoji} {t._true_label}",
        })
    return pd.DataFrame(rows)


def bank_to_df(txns: list[BankTxn]) -> pd.DataFrame:
    rows = []
    for t in txns:
        emoji = LABEL_EMOJI_MAP.get(t._true_label, "")
        rows.append({
            "bank_ref": t.bank_ref,
            "amount (₹)": float(paise_to_inr(t.amount_paise)),
            "bank_date": t.bank_date.isoformat(),
            "psp_ref": t.psp_ref or "—",
            "description": t.description,
            "true_label": f"{emoji} {t._true_label}",
        })
    return pd.DataFrame(rows)


def results_to_df(results: list[MatchResult]) -> pd.DataFrame:
    rows = []
    for r in results:
        emoji = LABEL_EMOJI_MAP.get(r.label, "")
        rows.append({
            "platform_id": r.platform_txn.txn_id if r.platform_txn else "—",
            "bank_ref": r.bank_txn.bank_ref if r.bank_txn else "—",
            "label": f"{emoji} {r.label}",
            "tier": r.match_tier or "—",
            "score": round(r.score, 4),
            "amount (₹)": float(
                paise_to_inr(
                    r.platform_txn.amount_paise
                    if r.platform_txn
                    else (r.bank_txn.amount_paise if r.bank_txn else 0)
                )
            ),
            "audit_note": r.audit_note,
        })
    return pd.DataFrame(rows)


# ──────────────────────────────────────────────────────────────────────────────
# Main content
# ──────────────────────────────────────────────────────────────────────────────
st.markdown(
    """
<div style='text-align:center; padding: 10px 0 6px 0;'>
  <span style='font-size:2.4rem; font-weight:800; background:linear-gradient(90deg,#a78bfa,#60a5fa);
  -webkit-background-clip:text; -webkit-text-fill-color:transparent;'>
    🏦 Payments Reconciliation Engine
  </span>
  <div style='color:#6b7280; font-size:0.88rem; margin-top:4px;'>
    INR-only · Integer Paise · T+N Settlement Window · Waterfall Matching
  </div>
</div>
""",
    unsafe_allow_html=True,
)

# Guard against invalid config before running
if total_anomaly >= 1.0:
    st.error("Fix anomaly rates in the sidebar before proceeding.")
    st.stop()

if period_start >= period_end:
    st.error("Period start must be before period end.")
    st.stop()

# ── Generate data ────────────────────────────────────────────────────────────
import json

anomaly_rates_json = json.dumps(anomaly_rates_input)
platform_txns, bank_txns = cached_generate(
    seed=int(seed),
    num_rows=int(num_rows),
    period_start=period_start,
    period_end=period_end,
    settlement_days=settlement_days,
    psp_missing_rate=psp_missing_rate,
    anomaly_rates_json=anomaly_rates_json,
)

# ── Run matcher ──────────────────────────────────────────────────────────────
match_results, audit_log = cached_match(
    platform_txns=tuple(platform_txns),
    bank_txns=tuple(bank_txns),
    settlement_days=settlement_days,
)

# ── Build report ─────────────────────────────────────────────────────────────
recon = build_reconciliation(match_results, settlement_window_days=settlement_days)

# ──────────────────────────────────────────────────────────────────────────────
# Top-level KPI strip
# ──────────────────────────────────────────────────────────────────────────────
col1, col2, col3, col4, col5 = st.columns(5)

def kpi_card(col, value, label):
    col.markdown(
        f"""<div class='metric-card'>
          <div class='metric-value'>{value}</div>
          <div class='metric-label'>{label}</div>
        </div>""",
        unsafe_allow_html=True,
    )

kpi_card(col1, len(platform_txns), "Platform Rows")
kpi_card(col2, len(bank_txns), "Bank Rows")
kpi_card(col3, recon.matched_count, "Matched Pairs")
kpi_card(col4, recon.unmatched_platform_count, "Unmatched Platform")
kpi_card(col5, recon.unmatched_bank_count, "Unmatched Bank")

st.markdown("<br>", unsafe_allow_html=True)

# ──────────────────────────────────────────────────────────────────────────────
# Tabs
# ──────────────────────────────────────────────────────────────────────────────
tab_data, tab_match, tab_report = st.tabs(
    ["📊  Data", "🔗  Matching & Audit Trail", "📋  Report & Scoring"]
)

# ═══════════════════════════════════════════════════════════════════════════════
# TAB 1 — Data
# ═══════════════════════════════════════════════════════════════════════════════
with tab_data:
    left, right = st.columns(2, gap="large")

    with left:
        st.markdown("<div class='section-header'>Platform Transactions</div>", unsafe_allow_html=True)
        p_df = platform_to_df(platform_txns)
        st.dataframe(p_df, width="stretch", height=380)

        # Label distribution pie
        st.markdown("<div class='section-header'>True-label distribution (Platform)</div>", unsafe_allow_html=True)
        label_counts_p = p_df["true_label"].value_counts().reset_index()
        label_counts_p.columns = ["label", "count"]
        st.bar_chart(label_counts_p.set_index("label"))

    with right:
        st.markdown("<div class='section-header'>Bank Transactions</div>", unsafe_allow_html=True)
        b_df = bank_to_df(bank_txns)
        st.dataframe(b_df, width="stretch", height=380)

        st.markdown("<div class='section-header'>True-label distribution (Bank)</div>", unsafe_allow_html=True)
        label_counts_b = b_df["true_label"].value_counts().reset_index()
        label_counts_b.columns = ["label", "count"]
        st.bar_chart(label_counts_b.set_index("label"))


# ═══════════════════════════════════════════════════════════════════════════════
# TAB 2 — Matching & Audit Trail
# ═══════════════════════════════════════════════════════════════════════════════
with tab_match:
    st.markdown("<div class='section-header'>Match Results</div>", unsafe_allow_html=True)

    # Filter controls
    filter_col1, filter_col2 = st.columns([1, 3])
    with filter_col1:
        tier_filter = st.multiselect(
            "Filter by tier",
            options=["TIER1", "TIER3", "—"],
            default=["TIER1", "TIER3", "—"],
        )
    with filter_col2:
        # Labels in the DataFrame are emoji-prefixed (e.g. "✅ MATCHED")
        # so the multiselect options must match that format exactly.
        label_options = [f"{LABEL_EMOJI_MAP.get(l, '')} {l}" for l in ALL_LABELS]
        label_filter = st.multiselect(
            "Filter by label",
            options=label_options,
            default=label_options,
        )

    r_df = results_to_df(match_results)
    mask = r_df["tier"].isin(tier_filter) & r_df["label"].isin(label_filter)
    filtered_df = r_df[mask]

    st.dataframe(filtered_df, width="stretch", height=340)

    st.caption(f"Showing {len(filtered_df)} / {len(r_df)} results")

    st.markdown("<div class='section-header'>Audit Trail</div>", unsafe_allow_html=True)

    audit_search = st.text_input("🔍 Search audit log", placeholder="e.g. PSP-001234 or TIER3")
    audit_entries_html = ""
    for entry in audit_log:
        if audit_search and audit_search.lower() not in entry.lower():
            continue
        is_section = entry.startswith("===")
        css_class = "audit-entry-section" if is_section else "audit-entry"
        safe_entry = entry.replace("<", "&lt;").replace(">", "&gt;")
        audit_entries_html += f"<div class='{css_class}'>{safe_entry}</div>\n"

    st.markdown(
        f"<div class='audit-scroll'>{audit_entries_html}</div>",
        unsafe_allow_html=True,
    )

    # Tier breakdown bar chart
    st.markdown("<div class='section-header'>Match Tier Breakdown</div>", unsafe_allow_html=True)
    tier_counts = r_df["tier"].value_counts().reset_index()
    tier_counts.columns = ["tier", "count"]
    st.bar_chart(tier_counts.set_index("tier"))


# ═══════════════════════════════════════════════════════════════════════════════
# TAB 3 — Report & Scoring
# ═══════════════════════════════════════════════════════════════════════════════
with tab_report:
    rep_left, rep_right = st.columns([1, 1], gap="large")

    with rep_left:
        st.markdown("<div class='section-header'>Reconciliation Statement</div>", unsafe_allow_html=True)

        # Balance banner
        if recon.residual_paise == 0:
            st.markdown(
                "<div class='banner-ok'>✅ BALANCED — Residual is ₹0.00</div>",
                unsafe_allow_html=True,
            )
        else:
            st.markdown(
                f"<div class='banner-err'>⚠️ OUT OF BALANCE — Residual: "
                f"{inr_str(abs(recon.residual_paise))} "
                f"({'debit' if recon.residual_paise < 0 else 'credit'})</div>",
                unsafe_allow_html=True,
            )

        st.markdown("<br>", unsafe_allow_html=True)
        statement_html = "<br>".join(
            line.replace("─" * 5, "<hr style='border-color:rgba(255,255,255,0.1);margin:4px 0;'>")
            if "─" * 5 in line
            else f"<span>{line}</span>"
            for line in recon.summary_lines()
        )
        st.markdown(
            f"<div class='statement-block'>{statement_html}</div>",
            unsafe_allow_html=True,
        )

        # Quick monetary KPIs
        st.markdown("<div class='section-header'>Monetary Summary</div>", unsafe_allow_html=True)
        m_cols = st.columns(2)
        kpi_card(m_cols[0], inr_str(recon.platform_total_paise), "Platform Total")
        kpi_card(m_cols[1], inr_str(recon.actual_bank_paise), "Actual Bank Total")
        st.markdown("<br>", unsafe_allow_html=True)
        m_cols2 = st.columns(2)
        kpi_card(m_cols2[0], inr_str(recon.in_transit_paise), "In-Transit")
        kpi_card(m_cols2[1], inr_str(recon.duplicate_paise), "Duplicates Removed")

    with rep_right:
        st.markdown("<div class='section-header'>Scorer — Confusion Matrix</div>", unsafe_allow_html=True)

        matrix = build_confusion_matrix(match_results)
        metrics = compute_metrics(matrix)

        # Render confusion matrix as a heatmap DataFrame
        labels_present = [l for l in ALL_LABELS if any(matrix[t].get(l, 0) > 0 for t in ALL_LABELS)]
        cm_data = {
            pred: [matrix[true].get(pred, 0) for true in labels_present]
            for pred in labels_present
        }
        cm_df = pd.DataFrame(cm_data, index=labels_present)

        st.dataframe(cm_df, width="stretch")
        st.caption("Rows = True label · Columns = Predicted label")

        st.markdown("<div class='section-header'>Per-Label Metrics</div>", unsafe_allow_html=True)
        metrics_rows = [
            {
                "label": lbl,
                "precision": round(m["precision"], 3),
                "recall": round(m["recall"], 3),
                "f1": round(m["f1"], 3),
                "support": int(m["support"]),
            }
            for lbl, m in metrics.items()
            if m["support"] > 0
        ]
        metrics_df = pd.DataFrame(metrics_rows).set_index("label")

        st.dataframe(metrics_df.round(3), width="stretch")

        active = [m for m in metrics.values() if m["support"] > 0]
        macro_f1 = sum(m["f1"] for m in active) / len(active) if active else 0.0
        st.metric("Macro-average F1 (active labels)", f"{macro_f1:.3f}")

        st.markdown("<div class='section-header'>Label Distribution in Results</div>", unsafe_allow_html=True)
        r_df_all = results_to_df(match_results)
        lbl_dist = r_df_all["label"].value_counts().reset_index()
        lbl_dist.columns = ["label", "count"]
        st.bar_chart(lbl_dist.set_index("label"))
