from __future__ import annotations

from pathlib import Path

import pyarrow.parquet as pq
import pytest
from pydantic import ValidationError

from fraud_graph.data.ingest import ingest_xlsx
from fraud_graph.graph.snapshots import GraphSnapshotBuildConfig, build_graph_snapshots


def test_build_graph_snapshots_reconciles_events_and_preserves_label_boundary(
    source_dir: Path, config
) -> None:
    data = ingest_xlsx(source_dir, config)
    snapshot_config = GraphSnapshotBuildConfig(
        observation_days=1,
        anchor_step_days=1,
        include_events=True,
    )
    manifest = build_graph_snapshots(
        config.storage.normalized_dir,
        config.storage.artifact_dir / "graph_snapshots",
        data.data_version,
        snapshot_config,
    )
    repeated = build_graph_snapshots(
        config.storage.normalized_dir,
        config.storage.artifact_dir / "graph_snapshots",
        data.data_version,
        snapshot_config,
    )

    assert repeated.snapshot_set_version == manifest.snapshot_set_version
    assert manifest.snapshot_count == 2
    assert manifest.covered_source_transaction_count == data.transaction_rows == 6
    assert manifest.source_transaction_coverage_rate == 1.0
    assert manifest.label_time_status == "missing"
    assert manifest.supervised_temporal_training_ready is False
    assert manifest.eligibility_blockers == ["LABEL_TIME_MISSING"]

    first = manifest.snapshots[0]
    assert first.event_count == 5
    assert first.active_node_count == 7
    assert first.directed_edge_count == 5
    assert first.weak_component_count == 2
    assert first.largest_component_size == 4
    assert first.active_suspect_count == 2
    assert first.future_transaction_count == 0
    assert first.future_transaction_leakage_rate == 0.0

    snapshot_dir = (
        config.storage.artifact_dir
        / "graph_snapshots"
        / manifest.snapshot_set_version
        / "snapshots"
        / first.snapshot_id
    )
    nodes = pq.read_table(snapshot_dir / first.files["nodes"]).to_pandas()
    edges = pq.read_table(snapshot_dir / first.files["edges"]).to_pandas()
    events = pq.read_table(snapshot_dir / first.files["events"]).to_pandas()
    labels = pq.read_table(
        config.storage.artifact_dir
        / "graph_snapshots"
        / manifest.snapshot_set_version
        / manifest.files["labels"]
    ).to_pandas()

    assert len(nodes) == data.account_rows
    assert int(nodes["out_transaction_count"].sum()) == first.event_count
    assert int(nodes["in_transaction_count"].sum()) == first.event_count
    assert int(edges["transaction_count"].sum()) == first.event_count
    assert len(events) == first.event_count
    assert events["event_time_utc"].min().to_pydatetime() >= first.window_start
    assert events["event_time_utc"].max().to_pydatetime() < first.anchor_at
    assert set(labels.columns) >= {"account_id", "label", "risk_event_at", "label_available_at"}
    assert "label" not in nodes.columns


def test_graph_snapshot_config_rejects_gaps_between_windows() -> None:
    with pytest.raises(ValidationError, match="anchor_step_days"):
        GraphSnapshotBuildConfig(observation_days=7, anchor_step_days=8)
