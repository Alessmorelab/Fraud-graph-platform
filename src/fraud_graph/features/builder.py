from __future__ import annotations

import hashlib
import os
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

import duckdb
import pyarrow as pa
from pydantic import BaseModel

from fraud_graph.data.manifest import DataManifest
from fraud_graph.errors import ArtifactNotReadyError

BEHAVIOR_FEATURES = [
    "out_txn_count",
    "in_txn_count",
    "out_counterparty_count",
    "in_counterparty_count",
    "out_active_days",
    "in_active_days",
    "out_amount_abs_sum",
    "in_amount_abs_sum",
    "out_amount_max",
    "in_amount_max",
    "night_txn_count",
    "negative_txn_count",
    "self_transfer_count",
    "same_day_in_out_days",
    "same_day_turnover_ratio_avg",
    "max_daily_fan_in",
    "max_daily_fan_out",
    "txn_count_total",
    "counterparty_count_total",
    "amount_abs_total",
    "net_flow_amount",
    "in_out_count_ratio",
    "in_out_amount_ratio",
    "has_transactions",
]

PROFILE_FEATURES = ["tenure_months", "region_code", "customer_type"]
FEATURE_IMPLEMENTATION_VERSION = "asof-account-v3"


class FeatureManifest(BaseModel):
    schema_version: str = "1.0"
    implementation_version: str = "unknown"
    feature_version: str
    data_version: str
    created_at: datetime
    as_of: datetime
    lookback_days: int
    row_count: int
    behavior_features: list[str]
    profile_features: list[str]
    file: str

    @classmethod
    def load(cls, path: Path) -> FeatureManifest:
        return cls.model_validate_json(path.read_text(encoding="utf-8"))


def build_account_features(
    normalized_root: Path,
    feature_root: Path,
    data_version: str,
    *,
    as_of: datetime | None = None,
    lookback_days: int = 184,
) -> FeatureManifest:
    data_dir = normalized_root / data_version
    data_manifest_path = data_dir / "manifest.json"
    if not data_manifest_path.exists():
        raise ArtifactNotReadyError(f"data manifest not found: {data_manifest_path}")
    data_manifest = DataManifest.load(data_manifest_path)
    if as_of is None:
        if data_manifest.transaction_time_max is None:
            raise ArtifactNotReadyError("transaction_time_max is unavailable")
        as_of = data_manifest.transaction_time_max + timedelta(microseconds=1)
    as_of = as_of.replace(tzinfo=UTC) if as_of.tzinfo is None else as_of.astimezone(UTC)
    window_start = as_of - timedelta(days=lookback_days)

    version_material = (
        f"{FEATURE_IMPLEMENTATION_VERSION}|{data_version}|{as_of.isoformat()}|{lookback_days}"
    )
    feature_version = hashlib.sha256(version_material.encode()).hexdigest()[:16]
    output_dir = feature_root / feature_version
    manifest_path = output_dir / "manifest.json"
    if manifest_path.exists():
        return FeatureManifest.load(manifest_path)
    if output_dir.exists():
        raise ArtifactNotReadyError(
            f"incomplete feature artifact already exists and requires quarantine: {output_dir}"
        )
    feature_root.mkdir(parents=True, exist_ok=True)
    temporary_dir = feature_root / f".tmp-{feature_version}-{os.getpid()}"
    if temporary_dir.exists():
        _remove_owned_temporary_dir(temporary_dir, feature_root)
    temporary_dir.mkdir(parents=False, exist_ok=False)

    accounts_path = _sql_path(data_dir / "accounts.parquet")
    transaction_glob = _sql_path(data_dir / "transactions" / "year=*" / "month=*" / "*.parquet")
    output_path = _sql_path(temporary_dir / "account_features.parquet")
    connection = duckdb.connect(database=":memory:")
    try:
        connection.execute("SET threads TO 4")
        connection.register(
            "feature_bounds",
            pa.table({"window_start": [window_start], "as_of": [as_of]}),
        )
        connection.execute(
            f"""
            CREATE TEMP VIEW accounts AS
            SELECT * FROM read_parquet('{accounts_path}');

            CREATE TEMP VIEW tx AS
            SELECT source.*
            FROM read_parquet('{transaction_glob}', hive_partitioning = true) source
            CROSS JOIN feature_bounds bounds
            WHERE event_time_utc >= bounds.window_start
              AND event_time_utc < bounds.as_of;
            """
        )
        connection.execute(
            """
            CREATE TEMP VIEW outgoing AS
            SELECT
                payer_account_id AS account_id,
                count(*)::DOUBLE AS out_txn_count,
                count(DISTINCT payee_account_id)::DOUBLE AS out_counterparty_count,
                count(DISTINCT CAST(event_time_local AS DATE))::DOUBLE AS out_active_days,
                sum(abs(CAST(amount AS DOUBLE))) AS out_amount_abs_sum,
                max(abs(CAST(amount AS DOUBLE))) AS out_amount_max,
                min(event_time_utc) AS first_out_time,
                max(event_time_utc) AS last_out_time
            FROM tx
            GROUP BY payer_account_id;

            CREATE TEMP VIEW incoming AS
            SELECT
                payee_account_id AS account_id,
                count(*)::DOUBLE AS in_txn_count,
                count(DISTINCT payer_account_id)::DOUBLE AS in_counterparty_count,
                count(DISTINCT CAST(event_time_local AS DATE))::DOUBLE AS in_active_days,
                sum(abs(CAST(amount AS DOUBLE))) AS in_amount_abs_sum,
                max(abs(CAST(amount AS DOUBLE))) AS in_amount_max,
                min(event_time_utc) AS first_in_time,
                max(event_time_utc) AS last_in_time
            FROM tx
            GROUP BY payee_account_id;

            CREATE TEMP VIEW event_flags AS
            SELECT
                account_id,
                sum(night_txn_count)::DOUBLE AS night_txn_count,
                sum(negative_txn_count)::DOUBLE AS negative_txn_count,
                sum(self_transfer_count)::DOUBLE AS self_transfer_count
            FROM (
                SELECT
                    payer_account_id AS account_id,
                    sum(CASE
                        WHEN hour(event_time_local) < 7 OR hour(event_time_local) >= 22
                        THEN 1 ELSE 0
                    END) AS night_txn_count,
                    sum(CASE WHEN is_negative THEN 1 ELSE 0 END) AS negative_txn_count,
                    sum(CASE WHEN is_self_transfer THEN 1 ELSE 0 END) AS self_transfer_count
                FROM tx GROUP BY payer_account_id
                UNION ALL
                SELECT
                    payee_account_id AS account_id,
                    sum(CASE
                        WHEN hour(event_time_local) < 7 OR hour(event_time_local) >= 22
                        THEN 1 ELSE 0
                    END) AS night_txn_count,
                    sum(CASE WHEN is_negative THEN 1 ELSE 0 END) AS negative_txn_count,
                    0 AS self_transfer_count
                FROM tx GROUP BY payee_account_id
            ) flags
            GROUP BY account_id;

            CREATE TEMP VIEW daily_direction AS
            SELECT
                payer_account_id AS account_id,
                CAST(event_time_local AS DATE) AS event_date,
                0.0 AS in_amount,
                sum(abs(CAST(amount AS DOUBLE))) AS out_amount,
                0.0 AS fan_in,
                count(DISTINCT payee_account_id)::DOUBLE AS fan_out
            FROM tx GROUP BY payer_account_id, event_date
            UNION ALL
            SELECT
                payee_account_id AS account_id,
                CAST(event_time_local AS DATE) AS event_date,
                sum(abs(CAST(amount AS DOUBLE))) AS in_amount,
                0.0 AS out_amount,
                count(DISTINCT payer_account_id)::DOUBLE AS fan_in,
                0.0 AS fan_out
            FROM tx GROUP BY payee_account_id, event_date;

            CREATE TEMP VIEW daily AS
            SELECT
                account_id,
                event_date,
                sum(in_amount) AS in_amount,
                sum(out_amount) AS out_amount,
                sum(fan_in) AS fan_in,
                sum(fan_out) AS fan_out
            FROM daily_direction
            GROUP BY account_id, event_date;

            CREATE TEMP VIEW daily_patterns AS
            SELECT
                account_id,
                sum(CASE
                    WHEN in_amount > 0 AND out_amount > 0 THEN 1 ELSE 0
                END)::DOUBLE AS same_day_in_out_days,
                avg(CASE
                    WHEN in_amount > 0 AND out_amount > 0
                    THEN least(in_amount, out_amount) / greatest(in_amount, out_amount)
                    ELSE NULL
                END) AS same_day_turnover_ratio_avg,
                max(fan_in)::DOUBLE AS max_daily_fan_in,
                max(fan_out)::DOUBLE AS max_daily_fan_out
            FROM daily
            GROUP BY account_id;
            """
        )
        connection.execute(
            f"""
            COPY (
                SELECT
                    a.account_id,
                    a.tenure_months,
                    a.region_code,
                    a.customer_type,
                    coalesce(o.out_txn_count, 0.0) AS out_txn_count,
                    coalesce(i.in_txn_count, 0.0) AS in_txn_count,
                    coalesce(o.out_counterparty_count, 0.0) AS out_counterparty_count,
                    coalesce(i.in_counterparty_count, 0.0) AS in_counterparty_count,
                    coalesce(o.out_active_days, 0.0) AS out_active_days,
                    coalesce(i.in_active_days, 0.0) AS in_active_days,
                    coalesce(o.out_amount_abs_sum, 0.0) AS out_amount_abs_sum,
                    coalesce(i.in_amount_abs_sum, 0.0) AS in_amount_abs_sum,
                    coalesce(o.out_amount_max, 0.0) AS out_amount_max,
                    coalesce(i.in_amount_max, 0.0) AS in_amount_max,
                    coalesce(f.night_txn_count, 0.0) AS night_txn_count,
                    coalesce(f.negative_txn_count, 0.0) AS negative_txn_count,
                    coalesce(f.self_transfer_count, 0.0) AS self_transfer_count,
                    coalesce(d.same_day_in_out_days, 0.0) AS same_day_in_out_days,
                    coalesce(d.same_day_turnover_ratio_avg, 0.0) AS same_day_turnover_ratio_avg,
                    coalesce(d.max_daily_fan_in, 0.0) AS max_daily_fan_in,
                    coalesce(d.max_daily_fan_out, 0.0) AS max_daily_fan_out,
                    coalesce(o.out_txn_count, 0.0)
                        + coalesce(i.in_txn_count, 0.0) AS txn_count_total,
                    coalesce(o.out_counterparty_count, 0.0)
                        + coalesce(i.in_counterparty_count, 0.0) AS counterparty_count_total,
                    coalesce(o.out_amount_abs_sum, 0.0)
                        + coalesce(i.in_amount_abs_sum, 0.0) AS amount_abs_total,
                    coalesce(i.in_amount_abs_sum, 0.0)
                        - coalesce(o.out_amount_abs_sum, 0.0) AS net_flow_amount,
                    (coalesce(i.in_txn_count, 0.0) + 1.0)
                        / (coalesce(o.out_txn_count, 0.0) + 1.0) AS in_out_count_ratio,
                    (coalesce(i.in_amount_abs_sum, 0.0) + 1.0)
                        / (coalesce(o.out_amount_abs_sum, 0.0) + 1.0) AS in_out_amount_ratio,
                    CASE
                        WHEN coalesce(o.out_txn_count, 0.0)
                            + coalesce(i.in_txn_count, 0.0) > 0
                        THEN 1.0 ELSE 0.0
                    END AS has_transactions,
                    greatest(o.last_out_time, i.last_in_time) AS last_activity_utc
                FROM accounts a
                LEFT JOIN outgoing o USING (account_id)
                LEFT JOIN incoming i USING (account_id)
                LEFT JOIN event_flags f USING (account_id)
                LEFT JOIN daily_patterns d USING (account_id)
                ORDER BY a.account_id
            ) TO '{output_path}' (FORMAT PARQUET, COMPRESSION ZSTD);
            """
        )
        row_count_result = connection.execute(
            f"SELECT count(*) FROM read_parquet('{output_path}')"
        ).fetchone()
        if row_count_result is None:
            raise ArtifactNotReadyError("feature output row count is unavailable")
        row_count = int(row_count_result[0])
    except Exception:
        _remove_owned_temporary_dir(temporary_dir, feature_root)
        raise
    finally:
        connection.close()

    manifest = FeatureManifest(
        implementation_version=FEATURE_IMPLEMENTATION_VERSION,
        feature_version=feature_version,
        data_version=data_version,
        created_at=datetime.now(UTC),
        as_of=as_of,
        lookback_days=lookback_days,
        row_count=row_count,
        behavior_features=BEHAVIOR_FEATURES,
        profile_features=PROFILE_FEATURES,
        file="account_features.parquet",
    )
    try:
        (temporary_dir / "manifest.json").write_text(
            manifest.model_dump_json(indent=2), encoding="utf-8"
        )
        temporary_dir.rename(output_dir)
        return manifest
    except FileExistsError:
        _remove_owned_temporary_dir(temporary_dir, feature_root)
        if manifest_path.exists():
            return FeatureManifest.load(manifest_path)
        raise
    except Exception:
        _remove_owned_temporary_dir(temporary_dir, feature_root)
        raise


def _sql_path(path: Path) -> str:
    return str(path.resolve()).replace("'", "''")


def _remove_owned_temporary_dir(path: Path, parent: Path) -> None:
    resolved_path = path.resolve()
    resolved_parent = parent.resolve()
    if resolved_path.parent != resolved_parent or not resolved_path.name.startswith(".tmp-"):
        raise RuntimeError(f"refusing to remove non-owned path: {path}")
    shutil.rmtree(resolved_path)
