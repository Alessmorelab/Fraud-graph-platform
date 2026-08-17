from __future__ import annotations

from pathlib import Path

import pandas as pd
import pyarrow.parquet as pq
import pytest

from fraud_graph.config import AppConfig
from fraud_graph.data.ingest import ingest_xlsx
from fraud_graph.errors import ProductionSafetyError
from fraud_graph.features.builder import build_account_features
from fraud_graph.graph.snapshots import GraphSnapshotBuildConfig, build_graph_snapshots
from fraud_graph.modeling.retrospective import (
    ANOMALY_FEATURES,
    GRAPH_FEATURES,
    TEMPORAL_FEATURES,
    build_graph_features,
    build_locked_account_split,
    build_snapshot_change_features,
    run_retrospective_experiment,
)


def test_locked_account_split_is_deterministic_disjoint_and_stratified() -> None:
    frame = pd.DataFrame(
        {
            "account_id": [str(index) for index in range(60)],
            "target": [1 if index < 12 else 0 for index in range(60)],
            "has_transactions": [1 if index % 4 else 0 for index in range(60)],
        }
    )
    first = build_locked_account_split(
        frame,
        random_seed=42,
        validation_fraction=0.2,
        test_fraction=0.2,
    )
    second = build_locked_account_split(
        frame,
        random_seed=42,
        validation_fraction=0.2,
        test_fraction=0.2,
    )

    pd.testing.assert_frame_equal(first, second)
    assert first["account_id"].is_unique
    assert set(first["account_id"]) == set(frame["account_id"])
    assert set(first["split"]) == {"train", "validation", "test"}
    assert all(
        first.loc[first["split"].eq(split), "target"].nunique() == 2
        for split in ("train", "validation", "test")
    )


def test_graph_and_snapshot_change_features_are_label_free(
    source_dir: Path,
    config: AppConfig,
) -> None:
    data = ingest_xlsx(source_dir, config)
    snapshots = build_graph_snapshots(
        config.storage.normalized_dir,
        config.storage.artifact_dir / "graph_snapshots",
        data.data_version,
        GraphSnapshotBuildConfig(
            observation_days=1,
            anchor_step_days=1,
            include_events=True,
        ),
    )
    snapshot_dir = (
        config.storage.artifact_dir / "graph_snapshots" / snapshots.snapshot_set_version
    )

    graph = build_graph_features(snapshot_dir, snapshots, random_seed=42)
    temporal = build_snapshot_change_features(snapshot_dir, snapshots)

    assert len(graph) == len(temporal) == data.account_rows
    assert set(GRAPH_FEATURES) <= set(graph.columns)
    assert set(TEMPORAL_FEATURES) <= set(temporal.columns)
    assert not ({"label", "label_type"} & set(graph.columns))
    assert not ({"label", "label_type"} & set(temporal.columns))
    assert graph["account_id"].is_unique
    assert temporal["account_id"].is_unique


def test_retrospective_experiment_builds_locked_test_artifacts(
    source_dir: Path,
    config: AppConfig,
) -> None:
    data = ingest_xlsx(source_dir, config)
    features = build_account_features(
        config.storage.normalized_dir,
        config.storage.feature_dir,
        data.data_version,
        lookback_days=30,
    )
    snapshots = build_graph_snapshots(
        config.storage.normalized_dir,
        config.storage.artifact_dir / "graph_snapshots",
        data.data_version,
        GraphSnapshotBuildConfig(
            observation_days=1,
            anchor_step_days=1,
            include_events=True,
        ),
    )

    with pytest.raises(ProductionSafetyError, match="allow_static_labels"):
        run_retrospective_experiment(
            config.storage.normalized_dir,
            config.storage.feature_dir,
            config.storage.artifact_dir,
            features.feature_version,
            snapshots.snapshot_set_version,
            positive_labels=[1],
        )

    metadata = run_retrospective_experiment(
        config.storage.normalized_dir,
        config.storage.feature_dir,
        config.storage.artifact_dir,
        features.feature_version,
        snapshots.snapshot_set_version,
        positive_labels=[1],
        allow_static_labels=True,
        random_seed=42,
        validation_fraction=0.2,
        test_fraction=0.2,
        top_fraction=0.1,
    )
    repeated = run_retrospective_experiment(
        config.storage.normalized_dir,
        config.storage.feature_dir,
        config.storage.artifact_dir,
        features.feature_version,
        snapshots.snapshot_set_version,
        positive_labels=[1],
        allow_static_labels=True,
        random_seed=42,
        validation_fraction=0.2,
        test_fraction=0.2,
        top_fraction=0.1,
    )

    experiment_dir = (
        config.storage.artifact_dir
        / "retrospective_experiments"
        / metadata.experiment_version
    )
    enhanced = pq.read_table(experiment_dir / metadata.files["enhanced_features"])
    assignments = pq.read_table(experiment_dir / metadata.files["split_assignments"]).to_pandas()
    test_predictions = pq.read_table(experiment_dir / metadata.files["test_predictions"])

    assert repeated.created_at == metadata.created_at
    assert metadata.production_eligible is False
    assert metadata.selected_candidate in metadata.selection_eligible_candidates
    assert metadata.test_metrics["selected_model"].evaluable is True
    assert set(assignments["split"]) == {"train", "validation", "test"}
    assert set(ANOMALY_FEATURES) <= set(enhanced.column_names)
    assert "target" not in enhanced.column_names
    assert test_predictions.num_rows == metadata.split_summary["test"].rows
    assert (experiment_dir / metadata.files["report"]).is_file()
    assert (experiment_dir / metadata.files["scoring_model"]).is_file()
    assert (experiment_dir / metadata.files["feature_importance"]).is_file()
