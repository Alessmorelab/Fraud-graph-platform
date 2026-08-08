from __future__ import annotations

from typing import Any

import numpy as np
from pydantic import BaseModel, Field
from sklearn.metrics import average_precision_score, brier_score_loss, roc_auc_score


class BinaryMetrics(BaseModel):
    """Metrics for one fixed evaluation population and one score vector."""

    roc_auc: float | None = None
    pr_auc: float | None = None
    recall_at_top_fraction: float | None = None
    precision_at_top_fraction: float | None = None
    lift_at_top_fraction: float | None = None
    brier_score: float | None = None
    top_fraction: float
    cutoff: int
    prevalence: float
    positives: int
    negatives: int
    rows: int
    evaluable: bool
    blockers: list[str] = Field(default_factory=list)


class MetricComparison(BaseModel):
    candidate: BinaryMetrics
    baseline: BinaryMetrics
    absolute_delta: dict[str, float | None]
    relative_delta: dict[str, float | None]


class SubgroupMetric(BaseModel):
    subgroup: str
    value: str
    metrics: BinaryMetrics


def evaluate_binary_scores(
    target: np.ndarray | list[int],
    scores: np.ndarray | list[float],
    *,
    top_fraction: float = 0.05,
) -> BinaryMetrics:
    if not 0.0 < top_fraction <= 1.0:
        raise ValueError("top_fraction must be in (0, 1]")
    target_array = np.asarray(target, dtype=np.int8)
    score_array = np.asarray(scores, dtype=np.float64)
    if target_array.ndim != 1 or score_array.ndim != 1 or len(target_array) != len(score_array):
        raise ValueError("target and scores must be one-dimensional arrays of equal length")
    if len(target_array) == 0:
        raise ValueError("cannot evaluate an empty population")
    if not np.isfinite(score_array).all():
        raise ValueError("scores contain non-finite values")
    if not np.isin(target_array, [0, 1]).all():
        raise ValueError("target must contain only 0 and 1")

    positives = int(target_array.sum())
    negatives = int(len(target_array) - positives)
    cutoff = max(1, int(np.ceil(len(target_array) * top_fraction)))
    top_indices = np.argsort(-score_array, kind="stable")[:cutoff]
    top_positive = int(target_array[top_indices].sum())
    prevalence = positives / len(target_array)
    blockers: list[str] = []
    roc_auc: float | None = None
    pr_auc: float | None = None
    if positives == 0:
        blockers.append("NO_POSITIVES")
    if negatives == 0:
        blockers.append("NO_NEGATIVES")
    if not blockers:
        roc_auc = float(roc_auc_score(target_array, score_array))
        pr_auc = float(average_precision_score(target_array, score_array))
    recall = top_positive / positives if positives else None
    precision = top_positive / cutoff
    lift = precision / prevalence if prevalence else None
    return BinaryMetrics(
        roc_auc=roc_auc,
        pr_auc=pr_auc,
        recall_at_top_fraction=recall,
        precision_at_top_fraction=precision,
        lift_at_top_fraction=lift,
        brier_score=float(brier_score_loss(target_array, np.clip(score_array, 0.0, 1.0))),
        top_fraction=top_fraction,
        cutoff=cutoff,
        prevalence=prevalence,
        positives=positives,
        negatives=negatives,
        rows=len(target_array),
        evaluable=not blockers,
        blockers=blockers,
    )


def compare_metrics(candidate: BinaryMetrics, baseline: BinaryMetrics) -> MetricComparison:
    metric_names = (
        "roc_auc",
        "pr_auc",
        "recall_at_top_fraction",
        "precision_at_top_fraction",
        "lift_at_top_fraction",
        "brier_score",
    )
    absolute: dict[str, float | None] = {}
    relative: dict[str, float | None] = {}
    for name in metric_names:
        candidate_value = getattr(candidate, name)
        baseline_value = getattr(baseline, name)
        if candidate_value is None or baseline_value is None:
            absolute[name] = None
            relative[name] = None
            continue
        absolute[name] = float(candidate_value - baseline_value)
        relative[name] = (
            None
            if baseline_value == 0
            else float((candidate_value - baseline_value) / baseline_value)
        )
    return MetricComparison(
        candidate=candidate,
        baseline=baseline,
        absolute_delta=absolute,
        relative_delta=relative,
    )


def evaluate_subgroups(
    frame: Any,
    target: np.ndarray | list[int],
    scores: np.ndarray | list[float],
    columns: list[str],
    *,
    top_fraction: float = 0.05,
) -> list[SubgroupMetric]:
    target_array = np.asarray(target, dtype=np.int8)
    score_array = np.asarray(scores, dtype=np.float64)
    if len(frame) != len(target_array):
        raise ValueError("frame and target lengths differ")
    results: list[SubgroupMetric] = []
    for column in columns:
        if column not in frame.columns:
            continue
        values = frame[column].fillna("__MISSING__").astype(str)
        for value in sorted(values.unique()):
            mask = values.to_numpy() == value
            if not mask.any():
                continue
            results.append(
                SubgroupMetric(
                    subgroup=column,
                    value=value,
                    metrics=evaluate_binary_scores(
                        target_array[mask], score_array[mask], top_fraction=top_fraction
                    ),
                )
            )
    return results
