from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Protocol

import duckdb
import pyarrow as pa

from fraud_graph.domain import TransactionEvent


class EventRepository(Protocol):
    def events_touching(
        self,
        account_ids: set[str],
        start_time: datetime,
        end_time: datetime,
    ) -> list[TransactionEvent]: ...


class InMemoryEventRepository:
    def __init__(self, events: list[TransactionEvent]) -> None:
        self._events = events

    def events_touching(
        self,
        account_ids: set[str],
        start_time: datetime,
        end_time: datetime,
    ) -> list[TransactionEvent]:
        return [
            event
            for event in self._events
            if start_time <= event.event_time_utc < end_time
            and (event.payer_account_id in account_ids or event.payee_account_id in account_ids)
        ]


class DuckDbParquetEventRepository:
    def __init__(self, transaction_glob: Path, max_rows_per_query: int = 50_000) -> None:
        self._transaction_glob = str(transaction_glob.resolve()).replace("'", "''")
        self._max_rows_per_query = max_rows_per_query

    def events_touching(
        self,
        account_ids: set[str],
        start_time: datetime,
        end_time: datetime,
    ) -> list[TransactionEvent]:
        if not account_ids:
            return []
        connection = duckdb.connect(database=":memory:")
        try:
            connection.register("requested_accounts", pa.table({"account_id": sorted(account_ids)}))
            rows = connection.execute(
                f"""
                SELECT
                    transaction_id,
                    payer_account_id,
                    payee_account_id,
                    event_time_utc,
                    amount
                FROM read_parquet('{self._transaction_glob}', hive_partitioning = true) tx
                WHERE event_time_utc >= ?
                  AND event_time_utc < ?
                  AND (
                    payer_account_id IN (SELECT account_id FROM requested_accounts)
                    OR payee_account_id IN (SELECT account_id FROM requested_accounts)
                  )
                ORDER BY event_time_utc DESC
                LIMIT ?
                """,
                [start_time, end_time, self._max_rows_per_query],
            ).fetchall()
        finally:
            connection.close()
        return [
            TransactionEvent(
                transaction_id=row[0],
                payer_account_id=row[1],
                payee_account_id=row[2],
                event_time_utc=row[3].astimezone(UTC),
                amount=Decimal(str(row[4])),
            )
            for row in rows
        ]
