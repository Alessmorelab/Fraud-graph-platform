from __future__ import annotations

import pyarrow as pa

ACCOUNT_SCHEMA = pa.schema(
    [
        pa.field("account_id", pa.string(), nullable=False),
        pa.field("tenure_months", pa.float64(), nullable=False),
        pa.field("region_code", pa.string(), nullable=False),
        pa.field("customer_type", pa.string(), nullable=False),
        pa.field("source_row", pa.int64(), nullable=False),
    ],
    metadata={b"schema_version": b"1.0"},
)

LABEL_SCHEMA = pa.schema(
    [
        pa.field("account_id", pa.string(), nullable=False),
        pa.field("label", pa.int8(), nullable=False),
        pa.field("label_type", pa.string(), nullable=False),
        pa.field("risk_event_at", pa.timestamp("us", tz="UTC"), nullable=True),
        pa.field("label_available_at", pa.timestamp("us", tz="UTC"), nullable=True),
        pa.field("label_source", pa.string(), nullable=True),
        pa.field("case_id", pa.string(), nullable=True),
        pa.field("source_row", pa.int64(), nullable=False),
    ],
    metadata={b"schema_version": b"1.0"},
)

TRANSACTION_SCHEMA = pa.schema(
    [
        pa.field("transaction_id", pa.string(), nullable=False),
        pa.field("payer_account_id", pa.string(), nullable=False),
        pa.field("payee_account_id", pa.string(), nullable=False),
        pa.field("event_time_utc", pa.timestamp("us", tz="UTC"), nullable=False),
        pa.field("event_time_local", pa.timestamp("us"), nullable=False),
        pa.field("amount", pa.decimal128(20, 2), nullable=False),
        pa.field("is_negative", pa.bool_(), nullable=False),
        pa.field("is_self_transfer", pa.bool_(), nullable=False),
        pa.field("channel_type", pa.string(), nullable=True),
        pa.field("transaction_type", pa.string(), nullable=True),
        pa.field("debit_credit_direction", pa.string(), nullable=True),
        pa.field("is_cross_region", pa.bool_(), nullable=True),
        pa.field("source_row", pa.int64(), nullable=False),
    ],
    metadata={b"schema_version": b"1.0"},
)
