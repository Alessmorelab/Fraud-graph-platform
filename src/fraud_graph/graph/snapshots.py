from __future__ import annotations

import hashlib
import json
import shutil
from collections import Counter
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
from pydantic import BaseModel, Field, model_validator

from fraud_graph.artifacts import atomic_artifact_directory
from fraud_graph.data.manifest import DataManifest
from fraud_graph.errors import ArtifactNotReadyError, DataContractError, ProductionSafetyError

SNAPSHOT_IMPLEMENTATION_VERSION = "rolling-directed-graph-v2"
SNAPSHOT_STATS_SCHEMA = pa.schema(
    [
        pa.field("snapshot_id", pa.string(), nullable=False),
        pa.field("anchor_at", pa.timestamp("us", tz="UTC"), nullable=False),
        pa.field("window_start", pa.timestamp("us", tz="UTC"), nullable=False),
        pa.field("event_count", pa.int64(), nullable=False),
        pa.field("active_node_count", pa.int64(), nullable=False),
        pa.field("isolated_node_count", pa.int64(), nullable=False),
        pa.field("directed_edge_count", pa.int64(), nullable=False),
        pa.field("self_transfer_count", pa.int64(), nullable=False),
        pa.field("negative_event_count", pa.int64(), nullable=False),
        pa.field("total_amount", pa.decimal128(38, 2), nullable=False),
        pa.field("total_abs_amount", pa.decimal128(38, 2), nullable=False),
        pa.field("weak_component_count", pa.int64(), nullable=False),
        pa.field("largest_component_size", pa.int64(), nullable=False),
        pa.field("max_in_degree", pa.int64(), nullable=False),
        pa.field("max_out_degree", pa.int64(), nullable=False),
        pa.field("active_other_count", pa.int64(), nullable=False),
        pa.field("active_suspect_count", pa.int64(), nullable=False),
        pa.field("active_victim_count", pa.int64(), nullable=False),
        pa.field("future_transaction_count", pa.int64(), nullable=False),
        pa.field("future_transaction_leakage_rate", pa.float64(), nullable=False),
    ],
    metadata={b"schema_version": b"1.0"},
)


class GraphSnapshotBuildConfig(BaseModel):
    observation_days: int = Field(default=28, ge=1, le=3_650)
    anchor_step_days: int = Field(default=7, ge=1, le=365)
    include_events: bool = True

    @model_validator(mode="after")
    def validate_window_coverage(self) -> GraphSnapshotBuildConfig:
        if self.anchor_step_days > self.observation_days:
            raise ValueError("anchor_step_days must not exceed observation_days")
        return self


class GraphSnapshotRecord(BaseModel):
    snapshot_id: str
    anchor_at: datetime
    window_start: datetime
    event_count: int
    active_node_count: int
    isolated_node_count: int
    directed_edge_count: int
    self_transfer_count: int
    negative_event_count: int
    total_amount: Decimal
    total_abs_amount: Decimal
    weak_component_count: int
    largest_component_size: int
    max_in_degree: int
    max_out_degree: int
    active_other_count: int
    active_suspect_count: int
    active_victim_count: int
    future_transaction_count: int
    future_transaction_leakage_rate: float
    files: dict[str, str]


class GraphSnapshotSetManifest(BaseModel):
    schema_version: str = "1.0"
    implementation_version: str = SNAPSHOT_IMPLEMENTATION_VERSION
    snapshot_set_version: str
    data_version: str
    created_at: datetime
    source_timezone: str
    interval_semantics: str = "[window_start, anchor_at)"
    observation_days: int
    anchor_step_days: int
    account_count: int
    source_transaction_count: int
    covered_source_transaction_count: int
    source_transaction_coverage_rate: float
    snapshot_count: int
    anchor_min: datetime
    anchor_max: datetime
    label_time_status: Literal["available", "missing"]
    supervised_temporal_training_ready: bool
    eligibility_blockers: list[str] = Field(default_factory=list)
    files: dict[str, str]
    snapshots: list[GraphSnapshotRecord]

    @classmethod
    def load(cls, path: Path) -> GraphSnapshotSetManifest:
        return cls.model_validate_json(path.read_text(encoding="utf-8"))


def build_graph_snapshots(
    normalized_root: Path,
    snapshot_root: Path,
    data_version: str,
    config: GraphSnapshotBuildConfig | None = None,
) -> GraphSnapshotSetManifest:
    build_config = config or GraphSnapshotBuildConfig()
    data_dir = normalized_root / data_version
    data_manifest_path = data_dir / "manifest.json"
    if not data_manifest_path.exists():
        raise ArtifactNotReadyError(f"data manifest not found: {data_manifest_path}")
    data_manifest = DataManifest.load(data_manifest_path)
    if (
        data_manifest.transaction_time_min is None
        or data_manifest.transaction_time_max is None
        or data_manifest.transaction_rows == 0
    ):
        raise ArtifactNotReadyError("transaction time bounds are unavailable")

    anchors = _build_anchors(
        data_manifest.transaction_time_min,
        data_manifest.transaction_time_max,
        data_manifest.source_timezone,
        build_config,
    )
    version_material = json.dumps(
        {
            "implementation": SNAPSHOT_IMPLEMENTATION_VERSION,
            "data_version": data_version,
            "config": build_config.model_dump(mode="json"),
            "anchors": [anchor.isoformat() for anchor in anchors],
        },
        sort_keys=True,
    )
    snapshot_set_version = hashlib.sha256(version_material.encode()).hexdigest()[:16]
    output_dir = snapshot_root / snapshot_set_version
    manifest_path = output_dir / "manifest.json"
    if manifest_path.exists():
        return GraphSnapshotSetManifest.load(manifest_path)
    if output_dir.exists():
        raise ArtifactNotReadyError(
            f"incomplete graph snapshot artifact exists: {output_dir}"
        )

    accounts_path = data_dir / "accounts.parquet"
    labels_path = data_dir / "labels.parquet"
    transaction_glob = data_dir / "transactions" / "year=*" / "month=*" / "*.parquet"
    if not accounts_path.exists() or not labels_path.exists():
        raise ArtifactNotReadyError("normalized account or label artifact is missing")

    with atomic_artifact_directory(output_dir) as temporary_dir:
        shutil.copy2(accounts_path, temporary_dir / "accounts.parquet")
        shutil.copy2(labels_path, temporary_dir / "labels.parquet")
        connection = duckdb.connect(database=":memory:")
        try:
            connection.execute("SET threads TO 4")
            _register_source_views(
                connection,
                accounts_path,
                labels_path,
                transaction_glob,
            )
            _validate_normalized_source(connection, data_manifest)
            snapshot_records = _write_snapshots(
                connection,
                temporary_dir,
                anchors,
                data_manifest,
                build_config,
            )
            covered_source_transactions = _covered_source_transactions(
                connection,
                anchors,
                build_config.observation_days,
            )
        finally:
            connection.close()

        if covered_source_transactions != data_manifest.transaction_rows:
            raise ProductionSafetyError(
                "rolling snapshot windows do not cover every source transaction: "
                f"{covered_source_transactions}/{data_manifest.transaction_rows}"
            )

        stats_table = pa.Table.from_pylist(
            [_flat_stats(record) for record in snapshot_records],
            schema=SNAPSHOT_STATS_SCHEMA,
        )
        pq.write_table(stats_table, temporary_dir / "snapshot_stats.parquet", compression="zstd")

        label_time_status: Literal["available", "missing"] = (
            "available" if data_manifest.temporal_training_ready else "missing"
        )
        blockers = [] if data_manifest.temporal_training_ready else ["LABEL_TIME_MISSING"]
        manifest = GraphSnapshotSetManifest(
            snapshot_set_version=snapshot_set_version,
            data_version=data_version,
            created_at=datetime.now(UTC),
            source_timezone=data_manifest.source_timezone,
            observation_days=build_config.observation_days,
            anchor_step_days=build_config.anchor_step_days,
            account_count=data_manifest.account_rows,
            source_transaction_count=data_manifest.transaction_rows,
            covered_source_transaction_count=covered_source_transactions,
            source_transaction_coverage_rate=(
                covered_source_transactions / data_manifest.transaction_rows
            ),
            snapshot_count=len(snapshot_records),
            anchor_min=anchors[0],
            anchor_max=anchors[-1],
            label_time_status=label_time_status,
            supervised_temporal_training_ready=data_manifest.temporal_training_ready,
            eligibility_blockers=blockers,
            files={
                "accounts": "accounts.parquet",
                "labels": "labels.parquet",
                "snapshot_stats": "snapshot_stats.parquet",
                "summary": "summary.md",
                "snapshot_pattern": "snapshots/<snapshot_id>/",
            },
            snapshots=snapshot_records,
        )
        (temporary_dir / "manifest.json").write_text(
            manifest.model_dump_json(indent=2), encoding="utf-8"
        )
        (temporary_dir / "summary.md").write_text(
            _render_summary(manifest), encoding="utf-8"
        )
    return manifest


def _build_anchors(
    transaction_time_min: datetime,
    transaction_time_max: datetime,
    source_timezone: str,
    config: GraphSnapshotBuildConfig,
) -> list[datetime]:
    timezone = ZoneInfo(source_timezone)
    time_min = _as_utc(transaction_time_min).astimezone(timezone)
    time_max = _as_utc(transaction_time_max).astimezone(timezone)
    first_anchor_local = datetime.combine(
        time_min.date() + timedelta(days=config.observation_days),
        time.min,
        tzinfo=timezone,
    )
    terminal_anchor_local = datetime.combine(
        time_max.date() + timedelta(days=1),
        time.min,
        tzinfo=timezone,
    )
    first_anchor = first_anchor_local.astimezone(UTC)
    terminal_anchor = terminal_anchor_local.astimezone(UTC)
    if first_anchor > terminal_anchor:
        raise ArtifactNotReadyError(
            "transaction history is shorter than the configured observation window"
        )
    step = timedelta(days=config.anchor_step_days)
    anchors: list[datetime] = []
    current = first_anchor
    while current <= terminal_anchor:
        anchors.append(current)
        current += step
    if anchors[-1] != terminal_anchor:
        anchors.append(terminal_anchor)
    return anchors


def _register_source_views(
    connection: duckdb.DuckDBPyConnection,
    accounts_path: Path,
    labels_path: Path,
    transaction_glob: Path,
) -> None:
    accounts_sql = _sql_path(accounts_path)
    labels_sql = _sql_path(labels_path)
    transaction_sql = _sql_path(transaction_glob)
    connection.execute(
        f"""
        CREATE TEMP VIEW accounts AS
        SELECT * FROM read_parquet('{accounts_sql}');

        CREATE TEMP VIEW labels AS
        SELECT * FROM read_parquet('{labels_sql}');

        CREATE TEMP VIEW transactions AS
        SELECT
            transaction_id,
            payer_account_id,
            payee_account_id,
            event_time_utc,
            event_time_local,
            amount,
            is_negative,
            is_self_transfer,
            channel_type,
            transaction_type,
            debit_credit_direction,
            is_cross_region,
            source_row
        FROM read_parquet('{transaction_sql}', hive_partitioning = true);
        """
    )


def _validate_normalized_source(
    connection: duckdb.DuckDBPyConnection,
    manifest: DataManifest,
) -> None:
    source_row = connection.execute(
        """
        SELECT
            count(*) AS rows,
            count(*) - count(DISTINCT transaction_id) AS duplicate_ids
        FROM transactions
        """
    ).fetchone()
    if source_row is None:
        raise ArtifactNotReadyError("source transaction statistics are unavailable")
    source_count, duplicate_ids = (int(source_row[0]), int(source_row[1]))
    if source_count != manifest.transaction_rows:
        raise DataContractError(
            f"transaction manifest mismatch: {source_count} != {manifest.transaction_rows}"
        )
    if duplicate_ids:
        raise DataContractError(f"normalized transactions contain {duplicate_ids} duplicate IDs")
    unknown_accounts = connection.execute(
        """
        SELECT count(*)
        FROM transactions tx
        LEFT JOIN accounts payer ON payer.account_id = tx.payer_account_id
        LEFT JOIN accounts payee ON payee.account_id = tx.payee_account_id
        WHERE payer.account_id IS NULL OR payee.account_id IS NULL
        """
    ).fetchone()
    if unknown_accounts is None:
        raise ArtifactNotReadyError("account coverage statistics are unavailable")
    if int(unknown_accounts[0]):
        raise DataContractError(
            f"normalized transactions reference {int(unknown_accounts[0])} unknown accounts"
        )


def _write_snapshots(
    connection: duckdb.DuckDBPyConnection,
    temporary_dir: Path,
    anchors: list[datetime],
    data_manifest: DataManifest,
    config: GraphSnapshotBuildConfig,
) -> list[GraphSnapshotRecord]:
    records: list[GraphSnapshotRecord] = []
    snapshots_dir = temporary_dir / "snapshots"
    snapshots_dir.mkdir()
    for index, anchor in enumerate(anchors, start=1):
        window_start = anchor - timedelta(days=config.observation_days)
        snapshot_id = f"s{index:03d}_{anchor.strftime('%Y%m%dT%H%M%S%fZ')}"
        snapshot_dir = snapshots_dir / snapshot_id
        snapshot_dir.mkdir()
        connection.register(
            "snapshot_bounds",
            pa.table({"window_start": [window_start], "anchor_at": [anchor]}),
        )
        try:
            _create_snapshot_views(connection)
            files = _write_snapshot_files(connection, snapshot_dir, config.include_events)
            record = _snapshot_record(
                connection,
                snapshot_dir,
                snapshot_id,
                anchor,
                window_start,
                data_manifest.account_rows,
                files,
            )
            (snapshot_dir / "manifest.json").write_text(
                record.model_dump_json(indent=2), encoding="utf-8"
            )
            records.append(record)
        finally:
            _drop_snapshot_views(connection)
            connection.unregister("snapshot_bounds")
    return records


def _create_snapshot_views(connection: duckdb.DuckDBPyConnection) -> None:
    connection.execute(
        """
        CREATE TEMP VIEW snapshot_events AS
        SELECT tx.*
        FROM transactions tx
        CROSS JOIN snapshot_bounds bounds
        WHERE tx.event_time_utc >= bounds.window_start
          AND tx.event_time_utc < bounds.anchor_at;

        CREATE TEMP VIEW snapshot_active_accounts AS
        SELECT payer_account_id AS account_id FROM snapshot_events
        UNION
        SELECT payee_account_id AS account_id FROM snapshot_events;

        CREATE TEMP VIEW snapshot_edges AS
        SELECT
            payer_account_id,
            payee_account_id,
            count(*)::BIGINT AS transaction_count,
            sum(amount) AS amount_sum,
            sum(abs(amount)) AS amount_abs_sum,
            min(amount) AS amount_min,
            max(amount) AS amount_max,
            min(event_time_utc) AS first_event_utc,
            max(event_time_utc) AS last_event_utc,
            sum(CASE WHEN is_negative THEN 1 ELSE 0 END)::BIGINT AS negative_event_count,
            sum(CASE WHEN is_self_transfer THEN 1 ELSE 0 END)::BIGINT AS self_transfer_count
        FROM snapshot_events
        GROUP BY payer_account_id, payee_account_id;
        """
    )


def _write_snapshot_files(
    connection: duckdb.DuckDBPyConnection,
    snapshot_dir: Path,
    include_events: bool,
) -> dict[str, str]:
    nodes_path = _sql_path(snapshot_dir / "nodes.parquet")
    edges_path = _sql_path(snapshot_dir / "edges.parquet")
    connection.execute(
        f"""
        COPY (
            WITH outgoing AS (
                SELECT
                    payer_account_id AS account_id,
                    count(*)::BIGINT AS out_transaction_count,
                    count(DISTINCT CASE
                        WHEN payer_account_id <> payee_account_id THEN payee_account_id
                    END)::BIGINT AS out_degree,
                    sum(amount) AS out_amount_sum,
                    sum(abs(amount)) AS out_amount_abs_sum,
                    min(event_time_utc) AS first_out_utc,
                    max(event_time_utc) AS last_out_utc
                FROM snapshot_events
                GROUP BY payer_account_id
            ),
            incoming AS (
                SELECT
                    payee_account_id AS account_id,
                    count(*)::BIGINT AS in_transaction_count,
                    count(DISTINCT CASE
                        WHEN payer_account_id <> payee_account_id THEN payer_account_id
                    END)::BIGINT AS in_degree,
                    sum(amount) AS in_amount_sum,
                    sum(abs(amount)) AS in_amount_abs_sum,
                    min(event_time_utc) AS first_in_utc,
                    max(event_time_utc) AS last_in_utc
                FROM snapshot_events
                GROUP BY payee_account_id
            )
            SELECT
                accounts.account_id,
                CASE WHEN active.account_id IS NULL THEN false ELSE true END AS is_active,
                coalesce(incoming.in_degree, 0)::BIGINT AS in_degree,
                coalesce(outgoing.out_degree, 0)::BIGINT AS out_degree,
                coalesce(incoming.in_transaction_count, 0)::BIGINT AS in_transaction_count,
                coalesce(outgoing.out_transaction_count, 0)::BIGINT AS out_transaction_count,
                coalesce(incoming.in_amount_sum, 0) AS in_amount_sum,
                coalesce(outgoing.out_amount_sum, 0) AS out_amount_sum,
                coalesce(incoming.in_amount_abs_sum, 0) AS in_amount_abs_sum,
                coalesce(outgoing.out_amount_abs_sum, 0) AS out_amount_abs_sum,
                coalesce(incoming.in_amount_sum, 0)
                    - coalesce(outgoing.out_amount_sum, 0) AS net_flow_amount,
                least(incoming.first_in_utc, outgoing.first_out_utc) AS first_activity_utc,
                greatest(incoming.last_in_utc, outgoing.last_out_utc) AS last_activity_utc
            FROM accounts
            LEFT JOIN snapshot_active_accounts active USING (account_id)
            LEFT JOIN outgoing USING (account_id)
            LEFT JOIN incoming USING (account_id)
            ORDER BY accounts.account_id
        ) TO '{nodes_path}' (FORMAT PARQUET, COMPRESSION ZSTD);

        COPY (
            SELECT * FROM snapshot_edges
            ORDER BY payer_account_id, payee_account_id
        ) TO '{edges_path}' (FORMAT PARQUET, COMPRESSION ZSTD);
        """
    )
    files = {"nodes": "nodes.parquet", "edges": "edges.parquet"}
    if include_events:
        events_path = _sql_path(snapshot_dir / "events.parquet")
        connection.execute(
            f"""
            COPY (
                SELECT * FROM snapshot_events
                ORDER BY event_time_utc, transaction_id
            ) TO '{events_path}' (FORMAT PARQUET, COMPRESSION ZSTD)
            """
        )
        files["events"] = "events.parquet"
    return files


def _snapshot_record(
    connection: duckdb.DuckDBPyConnection,
    snapshot_dir: Path,
    snapshot_id: str,
    anchor: datetime,
    window_start: datetime,
    account_count: int,
    files: dict[str, str],
) -> GraphSnapshotRecord:
    event_stats = connection.execute(
        """
        SELECT
            count(*),
            coalesce(sum(CASE WHEN is_self_transfer THEN 1 ELSE 0 END), 0),
            coalesce(sum(CASE WHEN is_negative THEN 1 ELSE 0 END), 0),
            coalesce(sum(amount), 0),
            coalesce(sum(abs(amount)), 0),
            coalesce(sum(CASE
                WHEN event_time_utc < bounds.window_start
                  OR event_time_utc >= bounds.anchor_at
                THEN 1 ELSE 0
            END), 0)
        FROM snapshot_events
        CROSS JOIN snapshot_bounds bounds
        """
    ).fetchone()
    node_path = str((snapshot_dir / files["nodes"]).resolve())
    edge_path = str((snapshot_dir / files["edges"]).resolve())
    graph_stats = connection.execute(
        """
        SELECT
            (SELECT count(*) FROM snapshot_active_accounts),
            (SELECT count(*) FROM snapshot_edges),
            (SELECT coalesce(max(in_degree), 0) FROM read_parquet(?)),
            (SELECT coalesce(max(out_degree), 0) FROM read_parquet(?))
        """,
        [node_path, node_path],
    ).fetchone()
    if event_stats is None or graph_stats is None:
        raise ArtifactNotReadyError("snapshot statistics are unavailable")

    event_count = int(event_stats[0])
    future_transaction_count = int(event_stats[5])
    node_totals = connection.execute(
        """
        SELECT sum(out_transaction_count), sum(in_transaction_count)
        FROM read_parquet(?)
        """,
        [node_path],
    ).fetchone()
    edge_totals = connection.execute(
        "SELECT sum(transaction_count), coalesce(sum(amount_sum), 0) FROM read_parquet(?)",
        [edge_path],
    ).fetchone()
    if node_totals is None or edge_totals is None:
        raise ArtifactNotReadyError("snapshot reconciliation statistics are unavailable")
    if int(node_totals[0]) != event_count or int(node_totals[1]) != event_count:
        raise DataContractError("snapshot node transaction counts do not reconcile")
    if int(edge_totals[0]) != event_count or Decimal(edge_totals[1]) != Decimal(event_stats[3]):
        raise DataContractError("snapshot edge aggregates do not reconcile")
    if future_transaction_count:
        raise ProductionSafetyError(
            f"snapshot {snapshot_id} contains {future_transaction_count} out-of-window events"
        )

    active_accounts = {
        str(row[0])
        for row in connection.execute(
            "SELECT account_id FROM snapshot_active_accounts"
        ).fetchall()
    }
    edge_pairs = [
        (str(row[0]), str(row[1]))
        for row in connection.execute(
            "SELECT payer_account_id, payee_account_id FROM snapshot_edges"
        ).fetchall()
    ]
    weak_component_count, largest_component_size = _weak_component_stats(
        active_accounts, edge_pairs
    )
    label_counts = Counter(
        {
            int(row[0]): int(row[1])
            for row in connection.execute(
                """
                SELECT labels.label, count(*)
                FROM labels
                JOIN snapshot_active_accounts USING (account_id)
                GROUP BY labels.label
                """
            ).fetchall()
        }
    )
    return GraphSnapshotRecord(
        snapshot_id=snapshot_id,
        anchor_at=anchor,
        window_start=window_start,
        event_count=event_count,
        active_node_count=int(graph_stats[0]),
        isolated_node_count=account_count - int(graph_stats[0]),
        directed_edge_count=int(graph_stats[1]),
        self_transfer_count=int(event_stats[1]),
        negative_event_count=int(event_stats[2]),
        total_amount=Decimal(event_stats[3]),
        total_abs_amount=Decimal(event_stats[4]),
        weak_component_count=weak_component_count,
        largest_component_size=largest_component_size,
        max_in_degree=int(graph_stats[2]),
        max_out_degree=int(graph_stats[3]),
        active_other_count=label_counts[0],
        active_suspect_count=label_counts[1],
        active_victim_count=label_counts[2],
        future_transaction_count=future_transaction_count,
        future_transaction_leakage_rate=(
            future_transaction_count / event_count if event_count else 0.0
        ),
        files=files,
    )


def _drop_snapshot_views(connection: duckdb.DuckDBPyConnection) -> None:
    connection.execute(
        """
        DROP VIEW IF EXISTS snapshot_edges;
        DROP VIEW IF EXISTS snapshot_active_accounts;
        DROP VIEW IF EXISTS snapshot_events;
        """
    )


def _covered_source_transactions(
    connection: duckdb.DuckDBPyConnection,
    anchors: list[datetime],
    observation_days: int,
) -> int:
    connection.register(
        "snapshot_windows",
        pa.table(
            {
                "window_start": [
                    anchor - timedelta(days=observation_days) for anchor in anchors
                ],
                "anchor_at": anchors,
            }
        ),
    )
    try:
        row = connection.execute(
            """
            SELECT count(DISTINCT tx.transaction_id)
            FROM transactions tx
            JOIN snapshot_windows windows
              ON tx.event_time_utc >= windows.window_start
             AND tx.event_time_utc < windows.anchor_at
            """
        ).fetchone()
    finally:
        connection.unregister("snapshot_windows")
    if row is None:
        raise ArtifactNotReadyError("snapshot source coverage is unavailable")
    return int(row[0])


def _weak_component_stats(
    active_accounts: set[str], edge_pairs: list[tuple[str, str]]
) -> tuple[int, int]:
    if not active_accounts:
        return 0, 0
    parent = {account_id: account_id for account_id in active_accounts}
    size = {account_id: 1 for account_id in active_accounts}

    def find(account_id: str) -> str:
        root = account_id
        while parent[root] != root:
            root = parent[root]
        while parent[account_id] != account_id:
            next_account = parent[account_id]
            parent[account_id] = root
            account_id = next_account
        return root

    for payer, payee in edge_pairs:
        payer_root = find(payer)
        payee_root = find(payee)
        if payer_root == payee_root:
            continue
        if size[payer_root] < size[payee_root]:
            payer_root, payee_root = payee_root, payer_root
        parent[payee_root] = payer_root
        size[payer_root] += size[payee_root]
    component_sizes = Counter(find(account_id) for account_id in active_accounts)
    return len(component_sizes), max(component_sizes.values())


def _flat_stats(record: GraphSnapshotRecord) -> dict[str, Any]:
    payload = record.model_dump(mode="python")
    payload.pop("files")
    return payload


def _render_summary(manifest: GraphSnapshotSetManifest) -> str:
    source_timezone = ZoneInfo(manifest.source_timezone)
    label_note = (
        "标签时间字段完整，可进一步构建严格时序监督样本。"
        if manifest.supervised_temporal_training_ready
        else "标签缺少风险发生/可得时间；本产物是交易动态图快照，不声称具备未来风险标签预测能力。"
    )
    lines = [
        "# 动态资金图快照构建报告",
        "",
        f"- 快照集版本：`{manifest.snapshot_set_version}`",
        f"- 数据版本：`{manifest.data_version}`",
        f"- 观察窗口：{manifest.observation_days} 天",
        f"- 常规步长：{manifest.anchor_step_days} 天",
        f"- 区间语义：`{manifest.interval_semantics}`",
        f"- 快照数量：{manifest.snapshot_count}",
        f"- 源交易覆盖率：{manifest.source_transaction_coverage_rate:.2%}",
        f"- 标签说明：{label_note}",
        "",
        "## 快照统计",
        "",
        f"| 快照 | 观察截止时间（{manifest.source_timezone}） | 交易数 | 活跃节点 | 聚合有向边 | "
        "弱连通分量 | 最大分量 | 嫌疑账户 | 受害账户 | 未来交易泄露 |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    lines.extend(
        "| "
        + " | ".join(
            [
                record.snapshot_id,
                record.anchor_at.astimezone(source_timezone).isoformat(),
                f"{record.event_count:,}",
                f"{record.active_node_count:,}",
                f"{record.directed_edge_count:,}",
                f"{record.weak_component_count:,}",
                f"{record.largest_component_size:,}",
                str(record.active_suspect_count),
                str(record.active_victim_count),
                str(record.future_transaction_count),
            ]
        )
        + " |"
        for record in manifest.snapshots
    )
    lines.extend(
        [
            "",
            "## 使用边界",
            "",
            "- `accounts.parquet` 与 `labels.parquet` 为静态表；标签不得作为模型输入特征。",
            "- 每个快照的 `nodes.parquet` 与 `edges.parquet` 只使用观察截止时间以前的交易。",
            "- `events.parquet` 保留源交易 ID，用于聚合对账、资金链路解释和审计追溯。",
            "- 图结构特征、风险模型和跨快照趋势特征应在本快照集之上另行构建。",
            "",
        ]
    )
    return "\n".join(lines)


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _sql_path(path: Path) -> str:
    return str(path.resolve()).replace("'", "''")
