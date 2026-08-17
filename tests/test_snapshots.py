from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pyarrow.parquet as pq
import pytest
from pydantic import ValidationError

from fraud_graph.data.ingest import ingest_xlsx
from fraud_graph.graph.snapshots import (
    GraphSnapshotBuildConfig,
    _assign_temporal_splits,
    build_graph_snapshots,
)
from fraud_graph.service.snapshot_view import GraphSnapshotView


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
    assert manifest.eligibility_blockers == [
        "LABEL_TIME_MISSING",
        "INSUFFICIENT_ANCHORS_FOR_PURGED_SPLIT",
    ]
    assert manifest.split_ready is False
    assert manifest.split_counts == {"train": 0, "validation": 0, "test": 0}

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

    artifact_dir = config.storage.artifact_dir / "graph_snapshots" / manifest.snapshot_set_version
    global_nodes = pq.read_table(artifact_dir / manifest.files["global_nodes"]).to_pandas()
    global_edges = pq.read_table(artifact_dir / manifest.files["global_directed_edges"]).to_pandas()
    snapshot_stats = pq.read_table(artifact_dir / manifest.files["snapshot_stats"]).to_pandas()
    audit = json.loads((artifact_dir / manifest.files["audit"]).read_text(encoding="utf-8"))

    assert len(global_nodes) == data.account_rows
    assert "label" not in global_nodes.columns
    assert int(global_nodes["out_transaction_count"].sum()) == data.transaction_rows
    assert int(global_nodes["in_transaction_count"].sum()) == data.transaction_rows
    assert int(global_edges["transaction_count"].sum()) == data.transaction_rows
    assert manifest.global_statistics.transaction_event_edge_count == data.transaction_rows
    assert manifest.global_statistics.directed_account_pair_edge_count == len(global_edges) == 6
    assert manifest.global_statistics.self_transfer_event_count == 1
    assert manifest.global_statistics.negative_event_count == 1
    assert set(snapshot_stats.columns) >= {"split", "split_status", "event_count"}
    assert audit["task1_graph_acceptance_passed"] is True
    assert audit["purged_split_ready"] is False
    assert (artifact_dir / "samples" / "accounts.csv").exists()
    assert (artifact_dir / "samples" / "transaction_events.csv").exists()
    assert (artifact_dir / "samples" / "global_directed_edges.csv").exists()

    covered_transaction_ids: set[str] = set()
    for record in manifest.snapshots:
        event_path = artifact_dir / "snapshots" / record.snapshot_id / record.files["events"]
        covered_transaction_ids.update(
            str(value) for value in pq.read_table(event_path, columns=["transaction_id"])[0]
        )
    assert len(covered_transaction_ids) == data.transaction_rows

    view = GraphSnapshotView(artifact_dir, manifest)
    detail = view.detail_payload(first.snapshot_id, node_limit=5, edge_limit=5)
    risk_ids = {
        str(node["account_id"])
        for node in detail["nodes"]
        if node["label_type"] != "其它"
    }
    connected_ids = {
        str(account_id)
        for edge in detail["edges"]
        for account_id in (edge["payer_account_id"], edge["payee_account_id"])
    }
    assert risk_ids == {"1", "2"}
    assert risk_ids <= connected_ids


def test_graph_snapshot_config_rejects_gaps_between_windows() -> None:
    with pytest.raises(ValidationError, match="anchor_step_days"):
        GraphSnapshotBuildConfig(observation_days=7, anchor_step_days=8)


def test_graph_snapshot_config_rejects_embargo_shorter_than_observation_window() -> None:
    with pytest.raises(ValidationError, match="embargo_days"):
        GraphSnapshotBuildConfig(observation_days=28, embargo_days=21)


def test_purged_temporal_split_is_chronological_and_has_no_window_reuse() -> None:
    anchors = [datetime(2025, 1, 29, tzinfo=UTC) + timedelta(days=7 * index) for index in range(24)]
    config = GraphSnapshotBuildConfig(
        observation_days=28,
        anchor_step_days=7,
        embargo_days=28,
    )

    assignments = _assign_temporal_splits(anchors, config)
    split_anchors = {
        split: [anchor for anchor in anchors if assignments[anchor][0] == split]
        for split in ("train", "validation", "test")
    }
    statuses = [assignments[anchor][1] for anchor in anchors]
    assigned_splits = [
        assignments[anchor][0] for anchor in anchors if assignments[anchor][0] is not None
    ]

    assert all(split_anchors.values())
    assert max(split_anchors["train"]) <= min(split_anchors["validation"]) - timedelta(days=28)
    assert max(split_anchors["validation"]) <= min(split_anchors["test"]) - timedelta(days=28)
    assert statuses.count("embargo") == 6
    assert "unassigned" not in statuses
    assert assigned_splits == sorted(
        assigned_splits,
        key=("train", "validation", "test").index,
    )
