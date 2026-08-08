from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import joblib
import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
from pydantic import BaseModel, Field

from fraud_graph.artifacts import atomic_artifact_directory
from fraud_graph.errors import ArtifactNotReadyError, ProductionSafetyError
from fraud_graph.modeling.evaluation import (
    BinaryMetrics,
    evaluate_binary_scores,
    evaluate_subgroups,
)
from fraud_graph.modeling.risk_model import _make_pipeline
from fraud_graph.modeling.rule_baseline import build_rule_scores
from fraud_graph.temporal.samples import TemporalSampleManifest

TEMPORAL_MODEL_VERSION = "temporal-logistic-v1"


class TemporalModelMetadata(BaseModel):
    schema_version: str = "1.0"
    implementation_version: str = TEMPORAL_MODEL_VERSION
    model_version: str
    sample_version: str
    data_version: str
    created_at: datetime
    feature_columns: list[str]
    training_rows: int
    training_positives: int
    metrics_by_split: dict[str, BinaryMetrics]
    rule_metrics_by_split: dict[str, BinaryMetrics]
    subgroup_metrics: list[dict[str, Any]] = Field(default_factory=list)
    operating_threshold: float | None = None
    production_eligible: bool = False
    eligibility_blockers: list[str] = Field(default_factory=list)
    model_file: str = "model.joblib"
    predictions_file: str = "predictions.parquet"

    @classmethod
    def load(cls, path: Path) -> TemporalModelMetadata:
        return cls.model_validate_json(path.read_text(encoding="utf-8"))


def train_temporal_model(
    sample_root: Path,
    artifact_root: Path,
    sample_version: str,
    *,
    random_seed: int = 20260807,
    top_fraction: float = 0.05,
    min_training_positives: int = 2,
) -> TemporalModelMetadata:
    sample_dir = sample_root / sample_version
    manifest = TemporalSampleManifest.load(sample_dir / "manifest.json")
    samples = pq.read_table(sample_dir / manifest.file).to_pandas()
    _validate_temporal_samples(samples, manifest)
    feature_columns = list(manifest.feature_columns)
    missing = set(feature_columns) - set(samples.columns)
    if missing:
        raise ArtifactNotReadyError(f"temporal sample features missing: {sorted(missing)}")
    if "target" not in samples.columns or "split" not in samples.columns:
        raise ArtifactNotReadyError("temporal samples require target and split columns")

    train_frame = samples.loc[samples["split"] == "train"].copy()
    if train_frame.empty:
        raise ProductionSafetyError("temporal training split is empty")
    target = train_frame["target"].astype(int).to_numpy()
    positives = int(target.sum())
    if positives < min_training_positives:
        raise ProductionSafetyError(
            f"only {positives} training positives are available; "
            f"at least {min_training_positives} are required"
        )
    if len(np.unique(target)) < 2:
        raise ProductionSafetyError("temporal training split must contain both classes")

    model_material = json.dumps(
        {
            "implementation": TEMPORAL_MODEL_VERSION,
            "sample_version": sample_version,
            "feature_columns": feature_columns,
            "random_seed": random_seed,
            "top_fraction": top_fraction,
        },
        sort_keys=True,
    )
    model_version = hashlib.sha256(model_material.encode()).hexdigest()[:16]
    output_dir = artifact_root / "temporal_models" / model_version
    metadata_path = output_dir / "metadata.json"
    if metadata_path.exists():
        return TemporalModelMetadata.load(metadata_path)
    if output_dir.exists():
        raise ArtifactNotReadyError(f"incomplete temporal model artifact exists: {output_dir}")

    pipeline = _make_pipeline("behavior", feature_columns, random_seed)
    pipeline.fit(train_frame[feature_columns], target)
    all_scores = pipeline.predict_proba(samples[feature_columns])[:, 1]
    rule_scores = build_rule_scores(samples[["account_id", *feature_columns]])[
        "rule_score"
    ].to_numpy(dtype=float)

    validation_mask = samples["split"].astype(str).to_numpy() == "validation"
    validation_scores = all_scores[validation_mask]
    validation_cutoff = max(1, int(np.ceil(validation_mask.sum() * top_fraction)))
    operating_threshold = float(np.sort(validation_scores)[-validation_cutoff])

    metrics_by_split: dict[str, BinaryMetrics] = {}
    rule_metrics_by_split: dict[str, BinaryMetrics] = {}
    for split in ("train", "validation", "test"):
        mask = samples["split"].astype(str).to_numpy() == split
        if not mask.any():
            raise ProductionSafetyError(f"temporal {split} split is empty")
        split_target = samples.loc[mask, "target"].astype(int).to_numpy()
        metrics_by_split[split] = evaluate_binary_scores(
            split_target, all_scores[mask], top_fraction=top_fraction
        )
        rule_metrics_by_split[split] = evaluate_binary_scores(
            split_target, rule_scores[mask], top_fraction=top_fraction
        )

    subgroup_metrics: list[dict[str, Any]] = []
    for split in ("validation", "test"):
        mask = samples["split"].astype(str).to_numpy() == split
        subgroup_metrics.extend(
            {
                "split": split,
                "subgroup": item.subgroup,
                "value": item.value,
                "metrics": item.metrics.model_dump(mode="json"),
            }
            for item in evaluate_subgroups(
                samples.loc[mask],
                samples.loc[mask, "target"].astype(int).to_numpy(),
                all_scores[mask],
                ["customer_type", "region_code", "has_transactions"],
                top_fraction=top_fraction,
            )
        )

    predictions = samples[["sample_id", "account_id", "anchor_at", "split", "target"]].copy()
    predictions["model_score"] = all_scores
    predictions["rule_score"] = rule_scores
    predictions["model_alert"] = predictions["model_score"] >= operating_threshold
    predictions_table = pa.Table.from_pandas(predictions, preserve_index=False)
    metadata = TemporalModelMetadata(
        model_version=model_version,
        sample_version=sample_version,
        data_version=manifest.data_version,
        created_at=datetime.now(UTC),
        feature_columns=feature_columns,
        training_rows=len(train_frame),
        training_positives=positives,
        metrics_by_split=metrics_by_split,
        rule_metrics_by_split=rule_metrics_by_split,
        subgroup_metrics=subgroup_metrics,
        operating_threshold=operating_threshold,
        production_eligible=False,
        eligibility_blockers=["MODEL_APPROVAL_REQUIRED"],
    )
    with atomic_artifact_directory(output_dir) as temporary_dir:
        joblib.dump(pipeline, temporary_dir / metadata.model_file)
        pq.write_table(
            predictions_table,
            temporary_dir / metadata.predictions_file,
            compression="zstd",
        )
        (temporary_dir / "metadata.json").write_text(
            metadata.model_dump_json(indent=2), encoding="utf-8"
        )
    return metadata


def _validate_temporal_samples(samples: pd.DataFrame, manifest: TemporalSampleManifest) -> None:
    if len(samples) != manifest.row_count:
        raise ArtifactNotReadyError("temporal sample row count does not match manifest")
    anchors = pd.to_datetime(samples["anchor_at"], utc=True)
    if anchors.isna().any() or not anchors.is_monotonic_increasing:
        raise ProductionSafetyError("temporal samples must be sorted by anchor time")
    split_by_anchor = samples.assign(_anchor=anchors).groupby("_anchor")["split"].nunique()
    if (split_by_anchor > 1).any():
        raise ProductionSafetyError("one anchor cannot appear in multiple temporal splits")
    split_order = {"train": 0, "validation": 1, "test": 2}
    anchor_split = samples.assign(_anchor=anchors).groupby("_anchor")["split"].first()
    ordered = [split_order[str(value)] for value in anchor_split.tolist()]
    if ordered != sorted(ordered):
        raise ProductionSafetyError("temporal splits must be chronological")
    if samples["sample_id"].duplicated().any():
        raise ProductionSafetyError("temporal sample_id must be unique")
