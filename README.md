# Fraud Graph Platform

An auditable batch-scoring and temporal-path evidence service for bank anti-fraud operations.

The implementation deliberately separates:

- immutable source ingestion and data-quality findings;
- as-of account behavior features;
- versioned model training and batch scoring;
- deterministic transaction-path evidence;
- a read-only operations API.

## Safety guarantees

- The risk label in the account workbook is used only for cross-file validation. It is never
  written to the account feature table.
- Every data, feature, model, and score artifact has an immutable content-derived version.
- Feature generation uses the half-open interval `[as_of - lookback, as_of)`.
- Temporal model training is blocked when `label_available_at` is missing.
- `--allow-static-labels` creates a retrospective model whose metadata explicitly contains
  `production_eligible=false`.
- Evidence reports cite source transaction IDs. An account without transactions cannot receive
  an invented path.
- Model output is decision support. The service does not perform automatic freezing or other
  irreversible actions.

## Quick start

```bash
python3.12 -m venv .venv
.venv/bin/pip install -e '.[dev]'

fraud-graph ingest \
  --source-dir /path/to/source \
  --config config/default.yaml

fraud-graph build-features \
  --data-version <data-version> \
  --config config/default.yaml

# Requires risk_event_at and label_available_at in the label workbook.
fraud-graph build-temporal-samples \
  --data-version <data-version> \
  --config config/default.yaml

fraud-graph train-temporal \
  --sample-version <sample-version> \
  --config config/default.yaml

# JSON/Parquet acceptance checks; missing case truth or reviewer cases are hard failures.
fraud-graph evaluate-reports --input reports.json
fraud-graph evaluate-associations --predictions associations.json --labels labels.parquet
fraud-graph export-case-samples --score-version <score-version> --limit 5
fraud-graph accept-task4 --case-sample-dir var/artifacts/case_samples/<version>
fraud-graph evaluate-cases \
  --input var/artifacts/case_samples/<version>/reviewer-1-submission.json \
  --input var/artifacts/case_samples/<version>/reviewer-2-submission.json \
  --case-sample-dir var/artifacts/case_samples/<version>

# This override is only for replaying the supplied static-label dataset.
fraud-graph train \
  --feature-version <feature-version> \
  --feature-set behavior \
  --allow-static-labels \
  --config config/default.yaml

fraud-graph score \
  --model-version <model-version> \
  --config config/default.yaml

fraud-graph serve \
  --score-version <score-version> \
  --config config/default.yaml
```

API endpoints:

- `GET /` (anti-fraud operations workbench)
- `GET /health/live`
- `GET /health/ready`
- `GET /v1/operations/summary`
- `GET /v1/accounts`
- `GET /v1/accounts/{account_id}/workspace`
- `GET /v1/accounts/{account_id}/risk`
- `GET /v1/accounts/{account_id}/evidence`
- `GET /v1/accounts/{account_id}/associations?limit=20`
- `GET /v1/accounts/{account_id}/report`
- `GET /v1/capabilities`

The workbench is served by FastAPI from package-owned static assets, so the same Python process and
Docker image provide the UI and API. It includes a paged risk queue, searchable account selection,
an interactive directed transaction graph, Top-20 associations, path evidence, model eligibility
warnings, and report access. Capability metadata reserves the integration boundaries for manual
review, case feedback, IAM, and audit sinks without presenting unavailable functions as active.

## Artifact layout

```text
var/
  normalized/<data_version>/
    manifest.json
    accounts.parquet
    labels.parquet
    transactions/year=YYYY/month=MM/part-00000.parquet
  features/<feature_version>/
    manifest.json
    account_features.parquet
  artifacts/
    models/<model_version>/
    temporal_samples/<sample_version>/
    temporal_models/<model_version>/
    scores/<score_version>/
```

The temporal model artifact contains one prediction table for train, validation, and test anchors,
unified rule/model metrics, subgroup metrics, and explicit leakage fields. It is still marked
`production_eligible=false` until model approval, calibration, shadow running, and rollback review
are completed. Association and report acceptance helpers live in
`fraud_graph.evidence.evaluation`. Case exports include an immutable packet hash and two human
review slots per case. Task 3 acceptance requires at least five resolved cases, two independent
human reviewers per case, and a 70% strict case pass rate; automated prechecks never count as human
reviews. Reviewer disagreements require a separate adjudicator. Task 4 acceptance verifies both
the structured evidence and an exact deterministic rendering of every delivered Markdown report.
Missing case truth or reviewer-labelled cases are blocked instead of manufacturing hit rates.

With the supplied dataset, Tasks 1 and 2 are static retrospective experiments only. Their artifacts
must remain `production_eligible=false`; no chronological model claim is permitted until real label
availability times are supplied.

The repository contains a production-oriented vertical slice, not a claim that the supplied
dataset is sufficient for production model approval. See `docs/architecture.md` and
`docs/production-readiness.md` for the required upstream fields and deployment controls. The
Chinese enterprise design and GitHub component assessment are in
`docs/enterprise-solution.zh-CN.md` and `docs/github-reference.zh-CN.md`.
