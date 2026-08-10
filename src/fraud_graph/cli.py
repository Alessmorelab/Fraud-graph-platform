from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Annotated, Any

import pyarrow.parquet as pq
import typer

from fraud_graph.config import AppConfig, load_config
from fraud_graph.data.ingest import ingest_xlsx
from fraud_graph.domain import EvidencePacket
from fraud_graph.evidence.cases import (
    build_automated_prechecks,
    evaluate_case_sample_reports,
    export_illustrative_cases,
    load_case_records,
)
from fraud_graph.evidence.evaluation import (
    evaluate_case_associations,
    evaluate_manual_cases,
    evaluate_report_consistency,
)
from fraud_graph.features.builder import build_account_features
from fraud_graph.graph.path_engine import PathSearchConfig
from fraud_graph.graph.snapshots import GraphSnapshotBuildConfig, build_graph_snapshots
from fraud_graph.logging import configure_logging
from fraud_graph.modeling.risk_model import score_accounts, train_risk_model
from fraud_graph.service.app import ServiceState, create_app
from fraud_graph.temporal.samples import TemporalSampleConfig, build_temporal_samples
from fraud_graph.temporal.trainer import train_temporal_model

app = typer.Typer(no_args_is_help=True, pretty_exceptions_enable=False)


def _config(path: Path) -> AppConfig:
    configure_logging()
    return load_config(path)


@app.command()
def ingest(
    source_dir: Annotated[Path, typer.Option(exists=True, file_okay=False)],
    config_path: Annotated[Path, typer.Option("--config", exists=True, dir_okay=False)] = Path(
        "config/default.yaml"
    ),
) -> None:
    config = _config(config_path)
    manifest = ingest_xlsx(source_dir.resolve(), config)
    typer.echo(manifest.model_dump_json(indent=2))


@app.command("build-features")
def build_features_command(
    data_version: Annotated[str, typer.Option()],
    as_of: Annotated[datetime | None, typer.Option()] = None,
    lookback_days: Annotated[int | None, typer.Option(min=1)] = None,
    config_path: Annotated[Path, typer.Option("--config", exists=True, dir_okay=False)] = Path(
        "config/default.yaml"
    ),
) -> None:
    config = _config(config_path)
    manifest = build_account_features(
        config.storage.normalized_dir,
        config.storage.feature_dir,
        data_version,
        as_of=as_of,
        lookback_days=lookback_days or config.features.lookback_days,
    )
    typer.echo(manifest.model_dump_json(indent=2))


@app.command("build-graph-snapshots")
def build_graph_snapshots_command(
    data_version: Annotated[str, typer.Option()],
    observation_days: Annotated[int | None, typer.Option(min=1)] = None,
    anchor_step_days: Annotated[int | None, typer.Option(min=1)] = None,
    include_events: Annotated[bool | None, typer.Option()] = None,
    config_path: Annotated[Path, typer.Option("--config", exists=True, dir_okay=False)] = Path(
        "config/default.yaml"
    ),
) -> None:
    config = _config(config_path)
    snapshot_config = GraphSnapshotBuildConfig(
        observation_days=observation_days or config.snapshots.observation_days,
        anchor_step_days=anchor_step_days or config.snapshots.anchor_step_days,
        include_events=(
            config.snapshots.include_events if include_events is None else include_events
        ),
    )
    manifest = build_graph_snapshots(
        config.storage.normalized_dir,
        config.storage.artifact_dir / "graph_snapshots",
        data_version,
        snapshot_config,
    )
    typer.echo(manifest.model_dump_json(indent=2))


@app.command("build-temporal-samples")
def build_temporal_samples_command(
    data_version: Annotated[str, typer.Option()],
    config_path: Annotated[Path, typer.Option("--config", exists=True, dir_okay=False)] = Path(
        "config/default.yaml"
    ),
) -> None:
    config = _config(config_path)
    temporal_config = TemporalSampleConfig(
        observation_days=config.temporal.observation_days,
        prediction_days=config.temporal.prediction_days,
        label_maturity_days=config.temporal.label_maturity_days,
        anchor_step_days=config.temporal.anchor_step_days,
        train_fraction=config.temporal.train_fraction,
        validation_fraction=config.temporal.validation_fraction,
        positive_labels=config.model.positive_labels,
    )
    manifest = build_temporal_samples(
        config.storage.normalized_dir,
        config.storage.feature_dir,
        config.storage.artifact_dir / "temporal_samples",
        data_version,
        temporal_config,
    )
    typer.echo(manifest.model_dump_json(indent=2))


@app.command("train-temporal")
def train_temporal_command(
    sample_version: Annotated[str, typer.Option()],
    config_path: Annotated[Path, typer.Option("--config", exists=True, dir_okay=False)] = Path(
        "config/default.yaml"
    ),
) -> None:
    config = _config(config_path)
    metadata = train_temporal_model(
        config.storage.artifact_dir / "temporal_samples",
        config.storage.artifact_dir,
        sample_version,
        random_seed=config.model.random_seed,
        top_fraction=config.model.top_fraction,
    )
    typer.echo(metadata.model_dump_json(indent=2))


@app.command("evaluate-reports")
def evaluate_reports_command(
    input_path: Annotated[Path, typer.Option("--input", exists=True, dir_okay=False)],
) -> None:
    payload = json.loads(input_path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise typer.BadParameter("input must be a JSON array of evidence packets")
    packets = [EvidencePacket.model_validate(item.get("packet", item)) for item in payload]
    result = evaluate_report_consistency(packets)
    typer.echo(result.model_dump_json(indent=2))


@app.command("accept-task4")
def accept_task4_command(
    case_sample_dir: Annotated[
        Path, typer.Option("--case-sample-dir", exists=True, file_okay=False)
    ],
    output_path: Annotated[Path | None, typer.Option("--output", dir_okay=False)] = None,
) -> None:
    result = evaluate_case_sample_reports(case_sample_dir)
    rendered = result.model_dump_json(indent=2)
    if output_path is not None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(rendered + "\n", encoding="utf-8")
    typer.echo(rendered)


@app.command("evaluate-associations")
def evaluate_associations_command(
    predictions_path: Annotated[Path, typer.Option("--predictions", exists=True, dir_okay=False)],
    labels_path: Annotated[Path, typer.Option("--labels", exists=True, dir_okay=False)],
    top_k: Annotated[int, typer.Option("--top-k", min=1, max=100)] = 20,
) -> None:
    predictions = json.loads(predictions_path.read_text(encoding="utf-8"))
    if not isinstance(predictions, dict):
        raise typer.BadParameter("predictions must be a JSON object: root_account -> account IDs")
    labels = pq.read_table(labels_path).to_pandas()
    result = evaluate_case_associations(predictions, labels, top_k=top_k)
    typer.echo(result.model_dump_json(indent=2))


@app.command("evaluate-cases")
def evaluate_cases_command(
    input_paths: Annotated[list[Path], typer.Option("--input", exists=True, dir_okay=False)],
    case_sample_dir: Annotated[
        Path, typer.Option("--case-sample-dir", exists=True, file_okay=False)
    ],
    output_path: Annotated[Path | None, typer.Option("--output", dir_okay=False)] = None,
) -> None:
    payload: list[dict[str, Any]] = []
    for input_path in input_paths:
        submission = json.loads(input_path.read_text(encoding="utf-8"))
        if not isinstance(submission, list) or not all(
            isinstance(item, dict) for item in submission
        ):
            raise typer.BadParameter(
                f"{input_path} must be a JSON array of reviewer-labelled cases"
            )
        payload.extend(submission)
    records = load_case_records(case_sample_dir / "cases.json")
    case_bindings = {record.case_id: record.packet_sha256 for record in records}
    result = evaluate_manual_cases(payload, case_bindings)
    rendered = result.model_dump_json(indent=2)
    if output_path is not None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(rendered + "\n", encoding="utf-8")
    typer.echo(rendered)


@app.command("precheck-cases")
def precheck_cases_command(
    case_sample_dir: Annotated[
        Path, typer.Option("--case-sample-dir", exists=True, file_okay=False)
    ],
    output_path: Annotated[Path, typer.Option("--output", dir_okay=False)],
) -> None:
    records = load_case_records(case_sample_dir / "cases.json")
    checks = build_automated_prechecks(records)
    rendered = json.dumps(
        [check.model_dump(mode="json") for check in checks], ensure_ascii=False, indent=2
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(rendered + "\n", encoding="utf-8")
    typer.echo(rendered)


@app.command("export-case-samples")
def export_case_samples_command(
    score_version: Annotated[str, typer.Option()],
    limit: Annotated[int, typer.Option(min=5, max=20)] = 5,
    config_path: Annotated[Path, typer.Option("--config", exists=True, dir_okay=False)] = Path(
        "config/default.yaml"
    ),
) -> None:
    config = _config(config_path)
    path_config = PathSearchConfig(
        max_hops=config.paths.max_hops,
        max_gap=timedelta(hours=config.paths.max_gap_hours),
        max_paths=config.paths.max_paths,
        min_amount_ratio=config.paths.min_amount_ratio,
        max_amount_ratio=config.paths.max_amount_ratio,
    )
    state = ServiceState(
        config.storage.normalized_dir,
        config.storage.feature_dir,
        config.storage.artifact_dir,
        score_version,
        path_config,
    )
    score_rows = sorted(
        (row for row in state.scores.values() if bool(row["has_transactions"])),
        key=lambda row: int(row["risk_rank"]),
    )[:limit]
    if len(score_rows) < limit:
        raise typer.BadParameter("fewer than the requested number of active accounts are scored")
    packets = [state.evidence(str(row["account_id"])) for row in score_rows]
    manifest = export_illustrative_cases(
        packets,
        config.storage.artifact_dir / "case_samples",
        score_version=score_version,
    )
    typer.echo(manifest.model_dump_json(indent=2))


@app.command()
def train(
    feature_version: Annotated[str, typer.Option()],
    feature_set: Annotated[str, typer.Option()] = "behavior",
    allow_static_labels: Annotated[bool, typer.Option()] = False,
    config_path: Annotated[Path, typer.Option("--config", exists=True, dir_okay=False)] = Path(
        "config/default.yaml"
    ),
) -> None:
    config = _config(config_path)
    if feature_set not in {"behavior", "profile", "combined"}:
        raise typer.BadParameter("feature-set must be behavior, profile, or combined")
    metadata = train_risk_model(
        config.storage.normalized_dir,
        config.storage.feature_dir,
        config.storage.artifact_dir,
        feature_version,
        positive_labels=config.model.positive_labels,
        feature_set=feature_set,  # type: ignore[arg-type]
        allow_static_labels=allow_static_labels,
        random_seed=config.model.random_seed,
        top_fraction=config.model.top_fraction,
    )
    typer.echo(metadata.model_dump_json(indent=2))


@app.command()
def score(
    model_version: Annotated[str, typer.Option()],
    config_path: Annotated[Path, typer.Option("--config", exists=True, dir_okay=False)] = Path(
        "config/default.yaml"
    ),
) -> None:
    config = _config(config_path)
    manifest = score_accounts(
        config.storage.feature_dir,
        config.storage.artifact_dir,
        model_version,
    )
    typer.echo(manifest.model_dump_json(indent=2))


@app.command()
def serve(
    score_version: Annotated[str, typer.Option()],
    host: Annotated[str, typer.Option()] = "127.0.0.1",
    port: Annotated[int, typer.Option(min=1, max=65535)] = 8080,
    case_sample_version: Annotated[str | None, typer.Option()] = None,
    review_submission_dir: Annotated[
        Path | None, typer.Option(file_okay=False)
    ] = None,
    enable_review_writes: Annotated[bool, typer.Option()] = False,
    trust_review_identity_headers: Annotated[bool, typer.Option()] = False,
    config_path: Annotated[Path, typer.Option("--config", exists=True, dir_okay=False)] = Path(
        "config/default.yaml"
    ),
) -> None:
    import uvicorn

    config = _config(config_path)
    path_config = PathSearchConfig(
        max_hops=config.paths.max_hops,
        max_gap=timedelta(hours=config.paths.max_gap_hours),
        max_paths=config.paths.max_paths,
        min_amount_ratio=config.paths.min_amount_ratio,
        max_amount_ratio=config.paths.max_amount_ratio,
    )
    if enable_review_writes and case_sample_version is None:
        raise typer.BadParameter("case-sample-version is required when review writes are enabled")
    if enable_review_writes and not trust_review_identity_headers:
        raise typer.BadParameter(
            "review writes require trust-review-identity-headers behind a trusted gateway"
        )
    if trust_review_identity_headers and not enable_review_writes:
        raise typer.BadParameter(
            "trust-review-identity-headers is only valid when review writes are enabled"
        )
    if enable_review_writes and review_submission_dir is None:
        assert case_sample_version is not None
        review_submission_dir = (
            config.storage.artifact_dir / "review_submissions" / case_sample_version
        )
    state = ServiceState(
        config.storage.normalized_dir,
        config.storage.feature_dir,
        config.storage.artifact_dir,
        score_version,
        path_config,
        case_sample_version=case_sample_version,
        review_submission_dir=review_submission_dir,
        review_write_enabled=enable_review_writes,
        trust_review_identity_headers=trust_review_identity_headers,
    )
    uvicorn.run(create_app(state), host=host, port=port)


if __name__ == "__main__":
    app()
