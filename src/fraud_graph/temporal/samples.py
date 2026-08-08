from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
from pydantic import BaseModel, Field, model_validator

from fraud_graph.artifacts import atomic_artifact_directory
from fraud_graph.data.manifest import DataManifest
from fraud_graph.errors import ArtifactNotReadyError, ProductionSafetyError
from fraud_graph.features.builder import BEHAVIOR_FEATURES, build_account_features

TEMPORAL_SAMPLE_IMPLEMENTATION_VERSION = "temporal-samples-v1"


class TemporalSampleConfig(BaseModel):
    observation_days: int = Field(default=28, ge=1, le=3_650)
    prediction_days: int = Field(default=7, ge=1, le=365)
    label_maturity_days: int = Field(default=7, ge=0, le=365)
    anchor_step_days: int = Field(default=7, ge=1, le=365)
    train_fraction: float = Field(default=0.6, gt=0.0, lt=1.0)
    validation_fraction: float = Field(default=0.2, gt=0.0, lt=1.0)
    positive_labels: list[int] = Field(default_factory=lambda: [1])

    @model_validator(mode="after")
    def validate_fractions(self) -> TemporalSampleConfig:
        if self.train_fraction + self.validation_fraction >= 1.0:
            raise ValueError("train_fraction + validation_fraction must be less than 1")
        return self


class TemporalSampleManifest(BaseModel):
    schema_version: str = "1.0"
    implementation_version: str = TEMPORAL_SAMPLE_IMPLEMENTATION_VERSION
    sample_version: str
    data_version: str
    created_at: datetime
    observation_days: int
    prediction_days: int
    label_maturity_days: int
    anchor_step_days: int
    anchor_min: datetime
    anchor_max: datetime
    anchor_count: int
    row_count: int
    positive_count: int
    split_counts: dict[str, int]
    feature_columns: list[str]
    future_transaction_leakage_rate: float = 0.0
    future_label_leakage_rate: float = 0.0
    file: str = "temporal_samples.parquet"

    @classmethod
    def load(cls, path: Path) -> TemporalSampleManifest:
        return cls.model_validate_json(path.read_text(encoding="utf-8"))


def build_temporal_samples(
    normalized_root: Path,
    feature_root: Path,
    sample_root: Path,
    data_version: str,
    config: TemporalSampleConfig | None = None,
) -> TemporalSampleManifest:
    sample_config = config or TemporalSampleConfig()
    data_dir = normalized_root / data_version
    data_manifest = DataManifest.load(data_dir / "manifest.json")
    if not data_manifest.temporal_training_ready:
        raise ProductionSafetyError(
            "temporal sample construction is blocked: risk_event_at and label_available_at "
            "are required for every eligible label"
        )
    if data_manifest.transaction_time_min is None or data_manifest.transaction_time_max is None:
        raise ArtifactNotReadyError("transaction time bounds are unavailable")

    labels = pq.read_table(data_dir / "labels.parquet").to_pandas()
    required_columns = {"account_id", "label", "risk_event_at", "label_available_at"}
    missing = required_columns - set(labels.columns)
    if missing:
        raise ArtifactNotReadyError(f"label columns missing: {sorted(missing)}")
    labels["risk_event_at"] = pd.to_datetime(labels["risk_event_at"], utc=True)
    labels["label_available_at"] = pd.to_datetime(labels["label_available_at"], utc=True)

    first_anchor = data_manifest.transaction_time_min + timedelta(
        days=sample_config.observation_days
    )
    last_anchor = data_manifest.transaction_time_max - timedelta(
        days=sample_config.prediction_days + sample_config.label_maturity_days
    )
    if first_anchor > last_anchor:
        raise ArtifactNotReadyError(
            "transaction history is shorter than the configured temporal windows"
        )

    anchors = pd.date_range(
        start=first_anchor,
        end=last_anchor,
        freq=f"{sample_config.anchor_step_days}D",
        inclusive="both",
    )
    if len(anchors) < 3:
        raise ArtifactNotReadyError("at least three chronological anchors are required")

    feature_frames: list[pd.DataFrame] = []
    for anchor_timestamp in anchors:
        anchor = anchor_timestamp.to_pydatetime().astimezone(UTC)
        feature_manifest = build_account_features(
            normalized_root,
            feature_root,
            data_version,
            as_of=anchor,
            lookback_days=sample_config.observation_days,
        )
        if feature_manifest.as_of != anchor:
            raise ProductionSafetyError("feature artifact as_of does not match the temporal anchor")
        feature_frame = pq.read_table(
            feature_root / feature_manifest.feature_version / feature_manifest.file
        ).to_pandas()
        cutoff = anchor + timedelta(
            days=sample_config.prediction_days + sample_config.label_maturity_days
        )
        prediction_end = anchor + timedelta(days=sample_config.prediction_days)
        matured = labels["label_available_at"] <= cutoff
        prior_risk = (
            labels["risk_event_at"].notna()
            & (labels["risk_event_at"] < anchor)
            & (labels["label_available_at"] <= anchor)
        )
        eligible_labels = labels.loc[matured & ~prior_risk].copy()
        if (eligible_labels["label_available_at"] > cutoff).any():
            raise ProductionSafetyError("future label entered a temporal sample")
        eligible_labels["target"] = (
            eligible_labels["label"].isin(sample_config.positive_labels)
            & (eligible_labels["risk_event_at"] >= anchor)
            & (eligible_labels["risk_event_at"] < prediction_end)
        ).astype("int8")
        merged = feature_frame.merge(
            eligible_labels[["account_id", "target"]],
            on="account_id",
            how="inner",
            validate="one_to_one",
        )
        merged["anchor_at"] = anchor
        merged["prediction_end"] = prediction_end
        merged["label_cutoff_at"] = cutoff
        merged["sample_id"] = merged["account_id"].astype(str) + "|" + anchor.isoformat()
        feature_frames.append(merged)

    samples = pd.concat(feature_frames, ignore_index=True)
    anchor_values = sorted(pd.to_datetime(samples["anchor_at"], utc=True).unique())
    split_by_anchor = _rolling_split(anchor_values, sample_config)
    samples["split"] = pd.to_datetime(samples["anchor_at"], utc=True).map(split_by_anchor)
    samples["split"] = samples["split"].astype(str)
    samples = samples.sort_values(["anchor_at", "account_id"]).reset_index(drop=True)

    version_material = json.dumps(
        {
            "implementation": TEMPORAL_SAMPLE_IMPLEMENTATION_VERSION,
            "data_version": data_version,
            "config": sample_config.model_dump(mode="json"),
        },
        sort_keys=True,
    )
    sample_version = hashlib.sha256(version_material.encode()).hexdigest()[:16]
    output_dir = sample_root / sample_version
    manifest_path = output_dir / "manifest.json"
    if manifest_path.exists():
        return TemporalSampleManifest.load(manifest_path)
    if output_dir.exists():
        raise ArtifactNotReadyError(f"incomplete temporal sample artifact exists: {output_dir}")

    feature_columns = [column for column in BEHAVIOR_FEATURES if column in samples.columns]
    table = pa.Table.from_pandas(samples, preserve_index=False)
    split_counts = {str(key): int(value) for key, value in samples["split"].value_counts().items()}
    manifest = TemporalSampleManifest(
        sample_version=sample_version,
        data_version=data_version,
        created_at=datetime.now(UTC),
        observation_days=sample_config.observation_days,
        prediction_days=sample_config.prediction_days,
        label_maturity_days=sample_config.label_maturity_days,
        anchor_step_days=sample_config.anchor_step_days,
        anchor_min=anchor_values[0].to_pydatetime().astimezone(UTC),
        anchor_max=anchor_values[-1].to_pydatetime().astimezone(UTC),
        anchor_count=len(anchor_values),
        row_count=len(samples),
        positive_count=int(samples["target"].sum()),
        split_counts=split_counts,
        feature_columns=feature_columns,
        future_transaction_leakage_rate=0.0,
        future_label_leakage_rate=0.0,
    )
    with atomic_artifact_directory(output_dir) as temporary_dir:
        pq.write_table(table, temporary_dir / manifest.file, compression="zstd")
        (temporary_dir / "manifest.json").write_text(
            manifest.model_dump_json(indent=2), encoding="utf-8"
        )
    return manifest


def _rolling_split(
    anchors: list[pd.Timestamp], config: TemporalSampleConfig
) -> dict[pd.Timestamp, str]:
    count = len(anchors)
    train_end = max(1, int(count * config.train_fraction))
    validation_end = max(
        train_end + 1,
        int(count * (config.train_fraction + config.validation_fraction)),
    )
    validation_end = min(validation_end, count - 1)
    return {
        anchor: (
            "train" if index < train_end else "validation" if index < validation_end else "test"
        )
        for index, anchor in enumerate(anchors)
    }
