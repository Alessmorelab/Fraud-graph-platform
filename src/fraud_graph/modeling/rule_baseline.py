from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from fraud_graph.features.builder import BEHAVIOR_FEATURES
from fraud_graph.modeling.evaluation import BinaryMetrics, evaluate_binary_scores

RULE_BASELINE_VERSION = "behavior-rules-v1"


def build_rule_scores(features: pd.DataFrame) -> pd.DataFrame:
    """Build a deterministic behavior-only score with no label-dependent fitting."""
    missing = {"account_id", *BEHAVIOR_FEATURES} - set(features.columns)
    if missing:
        raise ValueError(f"rule features missing columns: {sorted(missing)}")
    values = features[BEHAVIOR_FEATURES].copy()
    numeric = values.apply(pd.to_numeric, errors="coerce").fillna(0.0)

    def saturation(column: str, scale: float) -> np.ndarray:
        array = np.maximum(numeric[column].to_numpy(dtype=float), 0.0)
        result: np.ndarray = array / (array + scale)
        return result

    same_day = np.clip(numeric["same_day_turnover_ratio_avg"].to_numpy(dtype=float), 0.0, 1.0)
    fan_in = saturation("max_daily_fan_in", 5.0)
    fan_out = saturation("max_daily_fan_out", 5.0)
    velocity = saturation("same_day_in_out_days", 3.0)
    counterparties = saturation("counterparty_count_total", 10.0)
    activity = saturation("txn_count_total", 20.0)
    anomalies = np.clip(
        0.5 * saturation("negative_txn_count", 1.0) + 0.5 * saturation("self_transfer_count", 1.0),
        0.0,
        1.0,
    )
    score = np.clip(
        0.22 * velocity
        + 0.18 * same_day
        + 0.16 * fan_in
        + 0.16 * fan_out
        + 0.12 * counterparties
        + 0.10 * activity
        + 0.06 * anomalies,
        0.0,
        1.0,
    )
    return pd.DataFrame(
        {
            "account_id": features["account_id"].astype(str).to_numpy(),
            "rule_score": score,
        }
    )


def evaluate_rule_baseline(
    features: pd.DataFrame,
    target: np.ndarray | list[int],
    *,
    top_fraction: float = 0.05,
) -> tuple[pd.DataFrame, BinaryMetrics]:
    scores = build_rule_scores(features)
    metrics = evaluate_binary_scores(
        target,
        scores["rule_score"].to_numpy(dtype=float),
        top_fraction=top_fraction,
    )
    return scores, metrics


def rule_score_explanation(row: Any) -> list[str]:
    """Return deterministic rule codes suitable for evidence, without inventing facts."""
    explanations: list[str] = []
    if float(row.get("same_day_in_out_days", 0.0)) > 0:
        explanations.append("SAME_DAY_IN_OUT")
    if float(row.get("max_daily_fan_in", 0.0)) >= 5:
        explanations.append("FAN_IN_CONCENTRATION")
    if float(row.get("max_daily_fan_out", 0.0)) >= 5:
        explanations.append("FAN_OUT_DISPERSION")
    if float(row.get("self_transfer_count", 0.0)) > 0:
        explanations.append("SELF_TRANSFER")
    if float(row.get("negative_txn_count", 0.0)) > 0:
        explanations.append("NEGATIVE_AMOUNT")
    return explanations
