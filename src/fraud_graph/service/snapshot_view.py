from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq

from fraud_graph.graph.snapshots import GraphSnapshotSetManifest


class GraphSnapshotView:
    """Read a built snapshot set and prepare bounded payloads for the workbench."""

    def __init__(self, root: Path, manifest: GraphSnapshotSetManifest) -> None:
        self.root = root
        self.manifest = manifest
        self._records = {record.snapshot_id: record for record in manifest.snapshots}
        labels = pq.read_table(
            root / manifest.files["labels"],
            columns=["account_id", "label_type"],
        )
        self._labels = {
            str(account_id): str(label_type)
            for account_id, label_type in zip(
                labels.column("account_id").to_pylist(),
                labels.column("label_type").to_pylist(),
                strict=True,
            )
        }

    @classmethod
    def discover(cls, snapshot_root: Path, data_version: str) -> GraphSnapshotView | None:
        candidates: list[tuple[datetime, Path, GraphSnapshotSetManifest]] = []
        if not snapshot_root.exists():
            return None
        for manifest_path in snapshot_root.glob("*/manifest.json"):
            try:
                manifest = GraphSnapshotSetManifest.load(manifest_path)
            except (OSError, ValueError):
                continue
            if manifest.data_version == data_version:
                candidates.append((manifest.created_at, manifest_path.parent, manifest))
        if not candidates:
            return None
        _, root, manifest = max(candidates, key=lambda item: item[0])
        return cls(root, manifest)

    def list_payload(self) -> dict[str, Any]:
        return self.manifest.model_dump(mode="json")

    def detail_payload(
        self,
        snapshot_id: str,
        *,
        node_limit: int = 70,
        edge_limit: int = 150,
    ) -> dict[str, Any]:
        record = self._records.get(snapshot_id)
        if record is None:
            raise KeyError(snapshot_id)
        snapshot_dir = self.root / "snapshots" / snapshot_id
        node_rows = pq.read_table(snapshot_dir / record.files["nodes"]).to_pylist()
        edge_rows = pq.read_table(snapshot_dir / record.files["edges"]).to_pylist()
        active_nodes = [row for row in node_rows if bool(row["is_active"])]
        selected_ids = self._select_node_ids(active_nodes, edge_rows, node_limit)
        selected_nodes = [row for row in active_nodes if str(row["account_id"]) in selected_ids]
        selected_nodes.sort(key=self._node_rank, reverse=True)
        selected_edges = [
            row
            for row in edge_rows
            if str(row["payer_account_id"]) in selected_ids
            and str(row["payee_account_id"]) in selected_ids
        ]
        selected_edges.sort(key=self._edge_rank, reverse=True)
        selected_edges = selected_edges[:edge_limit]

        nodes = []
        for row in selected_nodes:
            payload = {key: _json_value(value) for key, value in row.items()}
            payload["label_type"] = self._labels.get(str(row["account_id"]), "其它")
            nodes.append(payload)
        edges = [
            {key: _json_value(value) for key, value in row.items()}
            for row in selected_edges
        ]
        return {
            "snapshot_set_version": self.manifest.snapshot_set_version,
            "snapshot": record.model_dump(mode="json"),
            "sample": {
                "strategy": "high_activity_and_high_value",
                "node_limit": node_limit,
                "edge_limit": edge_limit,
                "node_count": len(nodes),
                "edge_count": len(edges),
            },
            "nodes": nodes,
            "edges": edges,
        }

    def _select_node_ids(
        self,
        nodes: list[dict[str, Any]],
        edges: list[dict[str, Any]],
        node_limit: int,
    ) -> set[str]:
        active_ids = {str(row["account_id"]) for row in nodes}
        labeled_ids = [
            account_id
            for account_id in active_ids
            if self._labels.get(account_id, "其它") != "其它"
        ]
        labeled_ids.sort()
        selected = set(labeled_ids[:node_limit])

        ranked_nodes = sorted(nodes, key=self._node_rank, reverse=True)
        activity_target = min(node_limit, max(24, node_limit // 2))
        for row in ranked_nodes:
            if len(selected) >= activity_target:
                break
            selected.add(str(row["account_id"]))

        ranked_edges = sorted(edges, key=self._edge_rank, reverse=True)
        for row in ranked_edges:
            if len(selected) >= node_limit:
                break
            payer = str(row["payer_account_id"])
            payee = str(row["payee_account_id"])
            missing = {payer, payee} - selected
            if len(selected) + len(missing) <= node_limit:
                selected.update(missing)

        for row in ranked_nodes:
            if len(selected) >= node_limit:
                break
            selected.add(str(row["account_id"]))
        return selected

    @staticmethod
    def _node_rank(row: dict[str, Any]) -> tuple[Decimal, int, int, str]:
        amount = Decimal(row["in_amount_abs_sum"]) + Decimal(row["out_amount_abs_sum"])
        degree = int(row["in_degree"]) + int(row["out_degree"])
        transactions = int(row["in_transaction_count"]) + int(row["out_transaction_count"])
        return amount, degree, transactions, str(row["account_id"])

    @staticmethod
    def _edge_rank(row: dict[str, Any]) -> tuple[Decimal, int, str, str]:
        return (
            Decimal(row["amount_abs_sum"]),
            int(row["transaction_count"]),
            str(row["payer_account_id"]),
            str(row["payee_account_id"]),
        )


def _json_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat().replace("+00:00", "Z")
    return value
