# Production readiness gates

## Blocking data gates

- Label availability time is present and its business meaning is documented.
- Risk event time is distinct from investigation/confirmation time.
- Transaction reversal and refund semantics are available.
- Sampling rules and missing-counterparty behavior are documented.
- Account, label, and transaction referential integrity passes.
- A case or organization identifier is available for association evaluation when permitted.

## Model gates

- Rolling-origin temporal validation is used; random splits are diagnostic only.
- PR-AUC, recall at review capacity, calibration, and confidence intervals are reported.
- Performance is reported separately for active/inactive, customer type, region, and channel.
- Profile-only, behavior-only, graph-only, and combined models are compared.
- Model and threshold changes require registry approval and a rollback target.

## Service gates

- Authentication and authorization are enforced upstream.
- Readiness checks include score, model, feature, and data version compatibility.
- Rate limits and query timeouts protect path expansion.
- Audit events contain requester, purpose, versions, evidence IDs, and decision outcome.
- Automated account restriction is prohibited without the bank's approved decision workflow.

### Review workbench deployment boundary

The `/reviews` page and `GET /v1/reviews/*` endpoints can be exposed in read-only mode with a case
sample package. `POST /v1/reviews/submissions` is intentionally disabled unless the process is
started with `--enable-review-writes --trust-review-identity-headers` and placed behind the bank's
trusted IAM/API gateway. The gateway must inject the verified reviewer identity headers; client JSON
cannot set identity, timestamps, or reviewer kind. Review submissions are immutable JSON records
whose case ID and packet SHA-256 are validated before an exclusive append. This makes the current
UI useful for evidence review while preserving the upstream authentication, authorization, and
audit ownership required for production.

## Operations gates

- Data freshness and schema-drift alerts are configured.
- Feature drift, score drift, alert volume, and analyst outcomes are monitored.
- Incident, rollback, and model-disable runbooks are exercised.
- Sensitive data never enters external LLM services.
