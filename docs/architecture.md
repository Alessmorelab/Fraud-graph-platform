# Architecture

## Runtime topology

```text
Source extract
  -> contract validation
  -> immutable Parquet lake
  -> rolling directed-graph snapshots
  -> as-of batch feature job
  -> governed training job
  -> model registry adapter
  -> batch score store
  -> read-only risk/evidence API
                         |
                         -> temporal path repository
```

This implementation uses local Parquet and DuckDB to provide a reproducible vertical slice.
The interfaces are intentionally separable so enterprise deployments can replace them with:

- object storage and a governed catalog for Parquet;
- Spark or Flink for feature computation;
- MLflow or an internal registry for model promotion;
- ClickHouse, PostgreSQL, or an approved graph store for low-latency path queries;
- Kafka for event delivery when near-real-time scoring is required.

## Data contracts

All identifiers are stored as opaque strings. Transaction time is normalized to UTC while the
source-local timestamp is retained for behavioral features. Amount uses `DECIMAL(20,2)` in the
normalized layer and is cast to floating point only inside model feature computation.

Production labels must include at least:

```text
account_id
label
risk_event_at
label_available_at
case_id (optional but strongly recommended)
```

`label_available_at` controls what was knowable at each historical anchor. `risk_event_at`
defines the prediction target. They are not interchangeable.

## Model governance

The reference model is an interpretable weighted logistic classifier over behavior features.
It is a contract-compatible baseline, not the final champion model. A promoted model must pass:

- temporal backtesting with mature labels;
- static/profile versus behavior versus graph ablation;
- segment stability checks;
- calibration and review-capacity metrics;
- reproducibility from registered data and feature versions;
- model-risk and compliance approval.

Graph neural networks should be added behind the same scoring contract. They must not use
account-ID embeddings when unseen-account generalization is required.

## Evidence semantics

The path engine searches directed paths with monotonically ordered event times, bounded gaps,
bounded hops, and configurable amount continuity. Each fact references normalized transaction
IDs. Model importance can nominate candidate edges, but only deterministic validation can place
an edge in an evidence packet.

Evidence grades:

- A: scored account, behavioral facts, and one or more source-backed multi-hop temporal paths;
- B: scored account and transaction facts, but no qualifying multi-hop path;
- C: score with no observed transaction evidence. The score alone is not case evidence.

## Security boundary

The application intentionally does not embed credentials or implement a bespoke identity
system. In production it must run behind the bank's approved API gateway with mTLS/OAuth,
authorization by purpose and role, field-level access control, encrypted storage, secrets from a
managed vault, immutable audit events, and retention/deletion policies.

Raw account identifiers must not be written to general application logs. The included logging
helper emits a short one-way reference for operational correlation.
