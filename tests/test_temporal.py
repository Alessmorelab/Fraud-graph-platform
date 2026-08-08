from __future__ import annotations

from pathlib import Path

import pyarrow.parquet as pq
import pytest

from fraud_graph.data.ingest import ingest_xlsx
from fraud_graph.errors import ProductionSafetyError
from fraud_graph.temporal.samples import TemporalSampleConfig, build_temporal_samples
from fraud_graph.temporal.trainer import train_temporal_model


def test_static_labels_are_blocked_from_temporal_samples(source_dir: Path, config) -> None:
    data = ingest_xlsx(source_dir, config)
    with pytest.raises(ProductionSafetyError, match="risk_event_at"):
        build_temporal_samples(
            config.storage.normalized_dir,
            config.storage.feature_dir,
            config.storage.artifact_dir / "temporal_samples",
            data.data_version,
            TemporalSampleConfig(observation_days=3, prediction_days=2, label_maturity_days=1),
        )


def test_temporal_samples_split_anchors_and_train_without_leakage(
    temporal_source_dir: Path, temporal_config
) -> None:
    data = ingest_xlsx(temporal_source_dir, temporal_config)
    assert data.temporal_training_ready is True
    samples = build_temporal_samples(
        temporal_config.storage.normalized_dir,
        temporal_config.storage.feature_dir,
        temporal_config.storage.artifact_dir / "temporal_samples",
        data.data_version,
        TemporalSampleConfig(
            observation_days=3,
            prediction_days=2,
            label_maturity_days=1,
            anchor_step_days=2,
            train_fraction=0.6,
            validation_fraction=0.2,
        ),
    )
    table = pq.read_table(
        temporal_config.storage.artifact_dir
        / "temporal_samples"
        / samples.sample_version
        / samples.file
    ).to_pandas()
    assert samples.anchor_count >= 3
    assert samples.future_transaction_leakage_rate == 0.0
    assert samples.future_label_leakage_rate == 0.0
    assert set(table["split"]) == {"train", "validation", "test"}
    anchor_split = table.groupby("anchor_at")["split"].nunique()
    assert int(anchor_split.max()) == 1
    ordered = (
        table[["anchor_at", "split"]].drop_duplicates().sort_values("anchor_at")["split"].tolist()
    )
    assert ordered == sorted(ordered, key={"train": 0, "validation": 1, "test": 2}.get)
    assert table["sample_id"].is_unique

    metadata = train_temporal_model(
        temporal_config.storage.artifact_dir / "temporal_samples",
        temporal_config.storage.artifact_dir,
        samples.sample_version,
        random_seed=42,
        top_fraction=0.5,
    )
    assert metadata.production_eligible is False
    assert metadata.operating_threshold is not None
    assert set(metadata.metrics_by_split) == {"train", "validation", "test"}
    assert metadata.rule_metrics_by_split["test"].rows > 0
