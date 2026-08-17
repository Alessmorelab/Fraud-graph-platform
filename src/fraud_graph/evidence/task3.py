from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from pathlib import Path
from typing import Any

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
from pydantic import BaseModel, Field

from fraud_graph.artifacts import atomic_artifact_directory
from fraud_graph.data.manifest import DataManifest
from fraud_graph.domain import (
    EvidencePacket,
    FlowPattern,
    RiskScore,
    SuspiciousPath,
    TransactionEvent,
)
from fraud_graph.errors import ArtifactNotReadyError, ProductionSafetyError
from fraud_graph.evidence.associations import rank_associated_accounts
from fraud_graph.evidence.builder import build_evidence_packet
from fraud_graph.evidence.cases import CaseSampleManifest, export_illustrative_cases
from fraud_graph.features.builder import FeatureManifest
from fraud_graph.graph.path_engine import PathSearchConfig, TemporalPathEngine
from fraud_graph.graph.repository import DuckDbParquetEventRepository
from fraud_graph.modeling.evaluation import BinaryMetrics
from fraud_graph.modeling.retrospective import (
    RetrospectiveExperimentMetadata,
)
from fraud_graph.modeling.risk_model import (
    EvaluationMetrics,
    ModelMetadata,
    ScoreManifest,
)

TASK3_IMPLEMENTATION_VERSION = "task3-evidence-v3"


class Task3ArtifactManifest(BaseModel):
    schema_version: str = "1.0"
    implementation_version: str = TASK3_IMPLEMENTATION_VERSION
    task3_version: str
    experiment_version: str
    score_version: str
    case_sample_version: str
    data_version: str
    feature_version: str
    snapshot_set_version: str
    created_at: datetime
    root_count: int
    case_count: int
    top_k: int
    candidate_limit: int
    path_config: dict[str, Any]
    association_truth_available: bool = False
    manual_review_required: bool = True
    files: dict[str, str]
    eligibility_blockers: list[str] = Field(default_factory=list)

    @classmethod
    def load(cls, path: Path) -> Task3ArtifactManifest:
        return cls.model_validate_json(path.read_text(encoding="utf-8"))


def build_task3_artifacts(
    normalized_root: Path,
    feature_root: Path,
    artifact_root: Path,
    experiment_version: str,
    *,
    path_config: PathSearchConfig,
    candidate_limit: int = 40,
    case_limit: int = 5,
    top_k: int = 20,
) -> Task3ArtifactManifest:
    if candidate_limit < case_limit or case_limit < 5:
        raise ValueError("candidate_limit must cover at least five requested cases")
    if top_k < 1:
        raise ValueError("top_k must be positive")

    experiment_dir = artifact_root / "retrospective_experiments" / experiment_version
    metadata = RetrospectiveExperimentMetadata.load(experiment_dir / "metadata.json")
    feature_dir = feature_root / metadata.feature_version
    feature_manifest = FeatureManifest.load(feature_dir / "manifest.json")
    data_dir = normalized_root / metadata.data_version
    data_manifest = DataManifest.load(data_dir / "manifest.json")
    if metadata.data_version != feature_manifest.data_version:
        raise ArtifactNotReadyError("task3 experiment and feature data versions differ")

    score_manifest, score_dir = _prepare_compatibility_score(
        artifact_root,
        experiment_dir,
        metadata,
        feature_manifest,
    )
    task3_version = _task3_version(
        metadata,
        score_manifest,
        path_config,
        candidate_limit=candidate_limit,
        case_limit=case_limit,
        top_k=top_k,
    )
    output_dir = artifact_root / "task3" / task3_version
    manifest_path = output_dir / "manifest.json"
    if manifest_path.exists():
        return Task3ArtifactManifest.load(manifest_path)
    if output_dir.exists():
        raise ArtifactNotReadyError(f"incomplete task3 artifact exists: {output_dir}")

    scores = pq.read_table(score_dir / score_manifest.file).to_pandas()
    features = pq.read_table(feature_dir / feature_manifest.file).to_pandas()
    scores["account_id"] = scores["account_id"].astype(str)
    features["account_id"] = features["account_id"].astype(str)
    if scores["account_id"].duplicated().any() or features["account_id"].duplicated().any():
        raise ArtifactNotReadyError("task3 score and feature account IDs must be unique")
    if set(scores["account_id"]) != set(features["account_id"]):
        raise ArtifactNotReadyError("task3 score and feature account coverage differs")

    root_rows = (
        scores.loc[scores["has_transactions"].astype(bool)]
        .sort_values(["risk_rank", "account_id"], kind="stable")
        .head(candidate_limit)
    )
    if len(root_rows) < case_limit:
        raise ArtifactNotReadyError("fewer than five active scored accounts are available")

    risk_scores = scores.set_index("account_id")["risk_score"].astype(float).to_dict()
    evidence_scores = (
        scores.set_index("account_id")["evidence_score"].astype(float).to_dict()
        if "evidence_score" in scores
        else {}
    )
    feature_map = features.set_index("account_id").to_dict(orient="index")
    repository = DuckDbParquetEventRepository(
        data_dir / "transactions" / "year=*" / "month=*" / "*.parquet"
    )
    path_engine = TemporalPathEngine(repository, path_config)
    blockers = list(metadata.eligibility_blockers)
    if "TRANSDUCTIVE_GRAPH_EVALUATION" not in blockers:
        blockers.append("TRANSDUCTIVE_GRAPH_EVALUATION")
    packets: list[EvidencePacket] = []
    root_records: list[dict[str, Any]] = []
    association_records: list[dict[str, Any]] = []
    path_records: list[dict[str, Any]] = []
    pattern_records: list[dict[str, Any]] = []
    packet_records: dict[str, dict[str, Any]] = {}
    association_map: dict[str, list[dict[str, Any]]] = {}

    start_time = feature_manifest.as_of - timedelta(days=feature_manifest.lookback_days)
    for row in root_rows.to_dict(orient="records"):
        account_id = str(row["account_id"])
        score = _risk_score(row, metadata, feature_manifest, blockers)
        outgoing = path_engine.find_paths(
            account_id, start_time, feature_manifest.as_of, direction="outgoing"
        )
        incoming = path_engine.find_paths(
            account_id, start_time, feature_manifest.as_of, direction="incoming"
        )
        paths = sorted([*outgoing, *incoming], key=lambda item: (-item.score, item.path_id))[
            : path_config.max_paths
        ]
        patterns = detect_flow_patterns(account_id, paths, path_config)
        packet = build_evidence_packet(score, paths, feature_map.get(account_id), patterns)
        associations = rank_associated_accounts(
            account_id,
            paths,
            limit=top_k,
            risk_scores=risk_scores,
            evidence_scores=evidence_scores,
            patterns=patterns,
        )
        packets.append(packet)
        packet_records[account_id] = packet.model_dump(mode="json")
        association_map[account_id] = [item.model_dump(mode="json") for item in associations]
        root_records.append(
            {
                "account_id": account_id,
                "risk_rank": int(row["risk_rank"]),
                "risk_score": float(row["risk_score"]),
                "evidence_score": float(row.get("evidence_score", 0.0)),
                "anomaly_score": float(row.get("anomaly_score", 0.0)),
                "has_transactions": bool(row["has_transactions"]),
                "path_count": len(paths),
                "multi_hop_path_count": sum(len(path.steps) >= 2 for path in paths),
                "pattern_count": len(patterns),
                "evidence_grade": packet.grade.value,
            }
        )
        for item in associations:
            association_records.append(
                {"root_account_id": account_id, **item.model_dump(mode="json")}
            )
        for path in paths:
            path_records.append(_path_record(path))
        for pattern in patterns:
            pattern_records.append(
                {"root_account_id": account_id, **pattern.model_dump(mode="json")}
            )

    selected_packets = _select_case_packets(packets, case_limit)
    case_manifest = export_illustrative_cases(
        selected_packets,
        artifact_root / "case_samples",
        score_version=score_manifest.score_version,
    )
    selected_case_ids = {
        packet.account_id: f"case-{index:02d}"
        for index, packet in enumerate(selected_packets, start=1)
    }
    for record in root_records:
        case_id = selected_case_ids.get(record["account_id"])
        if case_id is not None:
            record["case_id"] = case_id

    files = {
        "root_accounts": "root_accounts.parquet",
        "associations": "top20_associations.parquet",
        "paths": "suspicious_paths.parquet",
        "patterns": "flow_patterns.parquet",
        "evidence_packets": "evidence_packets.json",
        "association_map": "association_map.json",
        "case_bindings": "case_bindings.json",
        "audit": "audit.json",
        "manifest": "manifest.json",
    }
    task_manifest = Task3ArtifactManifest(
        task3_version=task3_version,
        experiment_version=experiment_version,
        score_version=score_manifest.score_version,
        case_sample_version=case_manifest.case_sample_version,
        data_version=metadata.data_version,
        feature_version=metadata.feature_version,
        snapshot_set_version=metadata.snapshot_set_version,
        created_at=datetime.now(UTC),
        root_count=len(root_records),
        case_count=len(selected_packets),
        top_k=top_k,
        candidate_limit=candidate_limit,
        path_config={
            "max_hops": path_config.max_hops,
            "max_gap_hours": path_config.max_gap.total_seconds() / 3600,
            "max_paths": path_config.max_paths,
            "min_amount_ratio": path_config.min_amount_ratio,
            "max_amount_ratio": path_config.max_amount_ratio,
        },
        files=files,
        eligibility_blockers=blockers,
    )
    case_bindings = {
        f"case-{index:02d}": packet_records[packet.account_id]
        for index, packet in enumerate(selected_packets, start=1)
    }
    audit = _build_audit(root_records, path_records, pattern_records, data_manifest, case_manifest)
    with atomic_artifact_directory(output_dir) as temporary_dir:
        _write_frame(temporary_dir / files["root_accounts"], pd.DataFrame(root_records))
        _write_frame(temporary_dir / files["associations"], pd.DataFrame(association_records))
        _write_frame(temporary_dir / files["paths"], pd.DataFrame(path_records))
        _write_frame(temporary_dir / files["patterns"], pd.DataFrame(pattern_records))
        (temporary_dir / files["evidence_packets"]).write_text(
            json.dumps(packet_records, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (temporary_dir / files["association_map"]).write_text(
            json.dumps(association_map, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (temporary_dir / files["case_bindings"]).write_text(
            json.dumps(case_bindings, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (temporary_dir / files["audit"]).write_text(
            json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (temporary_dir / files["manifest"]).write_text(
            task_manifest.model_dump_json(indent=2), encoding="utf-8"
        )
    return task_manifest


def detect_flow_patterns(
    root_account_id: str,
    paths: list[SuspiciousPath],
    path_config: PathSearchConfig,
) -> list[FlowPattern]:
    events: dict[str, TransactionEvent] = {}
    for path in paths:
        for step in path.steps:
            events.setdefault(
                step.transaction_id,
                TransactionEvent(
                    transaction_id=step.transaction_id,
                    payer_account_id=step.payer_account_id,
                    payee_account_id=step.payee_account_id,
                    event_time_utc=step.event_time_utc,
                    amount=step.amount,
                ),
            )
    incoming = sorted(
        [event for event in events.values() if event.payee_account_id == root_account_id],
        key=lambda event: (event.event_time_utc, event.transaction_id),
    )
    outgoing = sorted(
        [event for event in events.values() if event.payer_account_id == root_account_id],
        key=lambda event: (event.event_time_utc, event.transaction_id),
    )
    patterns: list[FlowPattern] = []
    fan_in = _dense_direct_pattern(
        root_account_id,
        incoming,
        pattern_type="FAN_IN",
        path_config=path_config,
    )
    fan_out = _dense_direct_pattern(
        root_account_id,
        outgoing,
        pattern_type="FAN_OUT",
        path_config=path_config,
    )
    if fan_in is not None:
        patterns.append(fan_in)
    if fan_out is not None:
        patterns.append(fan_out)
    pass_through = _pass_through_pattern(root_account_id, incoming, outgoing, path_config)
    if pass_through is not None:
        patterns.append(pass_through)
    reciprocal = _reciprocal_pattern(root_account_id, incoming, outgoing, path_config)
    if reciprocal is not None:
        patterns.append(reciprocal)
    return sorted(patterns, key=lambda item: (-item.score, item.pattern_id))


def _dense_direct_pattern(
    root_account_id: str,
    events: list[TransactionEvent],
    *,
    pattern_type: str,
    path_config: PathSearchConfig,
) -> FlowPattern | None:
    if len(events) < 2:
        return None
    for start_index, start in enumerate(events):
        members = [
            event
            for event in events[start_index:]
            if event.event_time_utc - start.event_time_utc <= path_config.max_gap
        ]
        related = (
            {event.payer_account_id for event in members}
            if pattern_type == "FAN_IN"
            else {event.payee_account_id for event in members}
        )
        if len(related) < 2:
            continue
        score = min(0.98, 0.58 + 0.08 * min(5, len(related) - 2))
        transaction_ids = sorted(event.transaction_id for event in members)
        return FlowPattern(
            pattern_id=_pattern_id(pattern_type, transaction_ids),
            pattern_type=pattern_type,  # type: ignore[arg-type]
            root_account_id=root_account_id,
            score=score,
            transaction_ids=transaction_ids,
            related_account_ids=sorted(related),
            event_time_start=members[0].event_time_utc,
            event_time_end=members[-1].event_time_utc,
            description=(
                f"{len(related)}个不同账户在{path_config.max_gap.total_seconds() / 3600:g}小时内"
                + ("向根账户汇入资金" if pattern_type == "FAN_IN" else "从根账户接收资金")
            ),
        )
    return None


def _pass_through_pattern(
    root_account_id: str,
    incoming: list[TransactionEvent],
    outgoing: list[TransactionEvent],
    path_config: PathSearchConfig,
) -> FlowPattern | None:
    for first in incoming:
        for second in outgoing:
            gap = second.event_time_utc - first.event_time_utc
            if gap < timedelta(0) or gap > path_config.max_gap:
                continue
            if abs(first.amount) == 0 or abs(second.amount) == 0:
                continue
            ratio = float(abs(second.amount) / abs(first.amount))
            if not path_config.min_amount_ratio <= ratio <= path_config.max_amount_ratio:
                continue
            score = min(
                0.98,
                0.55
                + 0.25 * (1.0 - gap.total_seconds() / path_config.max_gap.total_seconds())
                + 0.20 * _ratio_similarity(ratio),
            )
            ids = sorted([first.transaction_id, second.transaction_id])
            return FlowPattern(
                pattern_id=_pattern_id("PASS_THROUGH", ids),
                pattern_type="PASS_THROUGH",
                root_account_id=root_account_id,
                score=score,
                transaction_ids=ids,
                related_account_ids=sorted(
                    {first.payer_account_id, second.payee_account_id} - {root_account_id}
                ),
                event_time_start=first.event_time_utc,
                event_time_end=second.event_time_utc,
                description=f"收款后{int(gap.total_seconds())}秒内发生金额比例{ratio:.2f}的转出",
            )
    return None


def _reciprocal_pattern(
    root_account_id: str,
    incoming: list[TransactionEvent],
    outgoing: list[TransactionEvent],
    path_config: PathSearchConfig,
) -> FlowPattern | None:
    for first in incoming:
        for second in outgoing:
            if first.payer_account_id != second.payee_account_id:
                continue
            gap = abs(second.event_time_utc - first.event_time_utc)
            if gap > path_config.max_gap:
                continue
            ids = sorted([first.transaction_id, second.transaction_id])
            return FlowPattern(
                pattern_id=_pattern_id("RECIPROCAL", ids),
                pattern_type="RECIPROCAL",
                root_account_id=root_account_id,
                score=max(
                    0.5,
                    0.85
                    - gap.total_seconds() / path_config.max_gap.total_seconds() * 0.25,
                ),
                transaction_ids=ids,
                related_account_ids=[first.payer_account_id],
                event_time_start=min(first.event_time_utc, second.event_time_utc),
                event_time_end=max(first.event_time_utc, second.event_time_utc),
                description="根账户与同一对手方在观察窗口内发生双向转账",
            )
    return None


def _ratio_similarity(ratio: float) -> float:
    return max(0.0, 1.0 - abs(ratio - 1.0) / 2.0)


def _pattern_id(pattern_type: str, transaction_ids: list[str]) -> str:
    material = pattern_type + "|" + "|".join(transaction_ids)
    return "pattern_" + hashlib.sha256(material.encode()).hexdigest()[:16]


def _risk_score(
    row: Mapping[str, Any],
    metadata: RetrospectiveExperimentMetadata,
    feature_manifest: FeatureManifest,
    blockers: list[str],
) -> RiskScore:
    return RiskScore.model_validate(
        {
            "account_id": str(row["account_id"]),
            "risk_score": float(row["risk_score"]),
            "risk_rank": int(row["risk_rank"]),
            "model_version": metadata.experiment_version,
            "data_version": metadata.data_version,
            "feature_as_of": feature_manifest.as_of,
            "has_transactions": bool(row["has_transactions"]),
            "model_training_mode": metadata.training_mode,
            "model_feature_set": "combined",
            "production_eligible": False,
            "eligibility_blockers": blockers,
            "evidence_score": float(row.get("evidence_score", 0.0)),
            "anomaly_score": float(row.get("anomaly_score", 0.0)),
        }
    )


def _path_record(path: SuspiciousPath) -> dict[str, Any]:
    return {
        "path_id": path.path_id,
        "root_account_id": path.root_account_id,
        "direction": path.direction,
        "score": path.score,
        "hop_count": len(path.steps),
        "evidence_codes": json.dumps(path.evidence_codes, ensure_ascii=False),
        "transaction_ids": json.dumps(
            [step.transaction_id for step in path.steps], ensure_ascii=False
        ),
        "steps": json.dumps(path.model_dump(mode="json")["steps"], ensure_ascii=False),
    }


def _select_case_packets(packets: list[EvidencePacket], case_limit: int) -> list[EvidencePacket]:
    def key(packet: EvidencePacket) -> tuple[float, float, str]:
        return (
            -float(any(len(path.steps) >= 2 for path in packet.paths)),
            -max((pattern.score for pattern in packet.patterns), default=0.0),
            packet.account_id,
        )

    selected: list[EvidencePacket] = []
    used_pattern_types: set[str] = set()
    for packet in sorted(packets, key=key):
        if len(selected) >= case_limit:
            break
        pattern_types = {pattern.pattern_type for pattern in packet.patterns}
        if pattern_types - used_pattern_types or not selected:
            selected.append(packet)
            used_pattern_types.update(pattern_types)
    if len(selected) < case_limit:
        for packet in sorted(packets, key=lambda item: item.score.risk_rank):
            if packet not in selected:
                selected.append(packet)
            if len(selected) >= case_limit:
                break
    return selected[:case_limit]


def _build_audit(
    roots: list[dict[str, Any]],
    paths: list[dict[str, Any]],
    patterns: list[dict[str, Any]],
    data_manifest: DataManifest,
    case_manifest: CaseSampleManifest,
) -> dict[str, Any]:
    path_errors: list[str] = []
    for path in paths:
        steps = json.loads(path["steps"])
        times = [step["event_time_utc"] for step in steps]
        if times != sorted(times):
            path_errors.append(str(path["path_id"]) + ":NON_MONOTONIC_TIME")
        if len({step["transaction_id"] for step in steps}) != len(steps):
            path_errors.append(str(path["path_id"]) + ":DUPLICATE_TRANSACTION")
        for previous, current in pairwise(steps):
            if previous["payee_account_id"] != current["payer_account_id"]:
                path_errors.append(str(path["path_id"]) + ":BROKEN_CONTINUITY")
    return {
        "data_version": data_manifest.data_version,
        "account_rows": data_manifest.account_rows,
        "root_count": len(roots),
        "path_count": len(paths),
        "pattern_count": len(patterns),
        "case_sample_version": case_manifest.case_sample_version,
        "case_status": case_manifest.status,
        "association_truth_available": False,
        "association_truth_blocker": "LABEL_CASE_ID_MISSING",
        "path_errors": sorted(set(path_errors)),
        "automated_precheck_is_not_human_acceptance": True,
    }


def _write_frame(path: Path, frame: pd.DataFrame) -> None:
    if frame.empty:
        frame = pd.DataFrame({"_empty": pd.Series(dtype="string")})
    pq.write_table(pa.Table.from_pandas(frame, preserve_index=False), path, compression="zstd")


def _task3_version(
    metadata: RetrospectiveExperimentMetadata,
    score_manifest: ScoreManifest,
    path_config: PathSearchConfig,
    *,
    candidate_limit: int,
    case_limit: int,
    top_k: int,
) -> str:
    material = json.dumps(
        {
            "implementation": TASK3_IMPLEMENTATION_VERSION,
            "experiment_version": metadata.experiment_version,
            "score_version": score_manifest.score_version,
            "path_config": {
                "max_hops": path_config.max_hops,
                "max_gap_seconds": path_config.max_gap.total_seconds(),
                "max_paths": path_config.max_paths,
                "min_amount_ratio": path_config.min_amount_ratio,
                "max_amount_ratio": path_config.max_amount_ratio,
            },
            "candidate_limit": candidate_limit,
            "case_limit": case_limit,
            "top_k": top_k,
        },
        sort_keys=True,
    )
    return hashlib.sha256(material.encode()).hexdigest()[:16]


def _prepare_compatibility_score(
    artifact_root: Path,
    experiment_dir: Path,
    metadata: RetrospectiveExperimentMetadata,
    feature_manifest: FeatureManifest,
) -> tuple[ScoreManifest, Path]:
    score_version = hashlib.sha256(
        f"task3-score-v1|{metadata.experiment_version}".encode()
    ).hexdigest()[:16]
    score_dir = artifact_root / "scores" / score_version
    manifest_path = score_dir / "manifest.json"
    model_dir = artifact_root / "models" / metadata.experiment_version
    model_path = model_dir / "metadata.json"
    if manifest_path.exists() and model_path.exists():
        return ScoreManifest.load(manifest_path), score_dir
    if score_dir.exists() or model_dir.exists():
        raise ArtifactNotReadyError("incomplete task3 compatibility score artifact exists")

    source_scores = pq.read_table(experiment_dir / metadata.files["scores"]).to_pandas()
    source_scores["model_version"] = metadata.experiment_version
    source_scores["data_version"] = metadata.data_version
    source_scores["feature_as_of"] = feature_manifest.as_of
    source_scores["model_feature_set"] = "combined"
    source_scores = source_scores[
        [
            "account_id",
            "risk_score",
            "risk_rank",
            "model_version",
            "data_version",
            "feature_as_of",
            "has_transactions",
            "model_training_mode",
            "model_feature_set",
            "production_eligible",
            "evidence_score",
            "anomaly_score",
        ]
    ]
    selected_metric = metadata.test_metrics["selected_model"]
    model_metric = _compatibility_metric(selected_metric)
    model_metadata = ModelMetadata(
        implementation_version=metadata.implementation_version,
        model_version=metadata.experiment_version,
        feature_version=metadata.feature_version,
        data_version=metadata.data_version,
        created_at=metadata.created_at,
        training_mode="retrospective_static",
        production_eligible=False,
        positive_labels=metadata.positive_labels,
        feature_set="combined",
        feature_columns=list(
            dict.fromkeys(column for group in metadata.feature_groups.values() for column in group)
        ),
        random_seed=metadata.random_seed,
        folds=0,
        evaluation_scheme=metadata.evaluation_scheme,
        eligibility_blockers=list(metadata.eligibility_blockers),
        metrics=model_metric,
        model_file=f"retrospective_experiments/{metadata.experiment_version}/{metadata.files['evaluation_model']}",
    )
    score_manifest = ScoreManifest(
        implementation_version="task3-score-v1",
        score_version=score_version,
        model_version=metadata.experiment_version,
        feature_version=metadata.feature_version,
        data_version=metadata.data_version,
        created_at=datetime.now(UTC),
        feature_as_of=feature_manifest.as_of,
        row_count=len(source_scores),
    )
    with atomic_artifact_directory(score_dir) as temporary_dir:
        pq.write_table(
            pa.Table.from_pandas(source_scores, preserve_index=False),
            temporary_dir / score_manifest.file,
            compression="zstd",
        )
        (temporary_dir / "manifest.json").write_text(
            score_manifest.model_dump_json(indent=2), encoding="utf-8"
        )
    with atomic_artifact_directory(model_dir) as temporary_dir:
        (temporary_dir / "metadata.json").write_text(
            model_metadata.model_dump_json(indent=2), encoding="utf-8"
        )
    return score_manifest, score_dir


def _compatibility_metric(metric: BinaryMetrics) -> EvaluationMetrics:
    if (
        metric.roc_auc is None
        or metric.pr_auc is None
        or metric.recall_at_top_fraction is None
        or metric.precision_at_top_fraction is None
    ):
        raise ProductionSafetyError("selected task2 metric is not evaluable")
    return EvaluationMetrics(
        roc_auc=metric.roc_auc,
        pr_auc=metric.pr_auc,
        recall_at_top_fraction=metric.recall_at_top_fraction,
        precision_at_top_fraction=metric.precision_at_top_fraction,
        top_fraction=metric.top_fraction,
        prevalence=metric.prevalence,
        positives=metric.positives,
        rows=metric.rows,
        lift_at_top_fraction=metric.lift_at_top_fraction,
        brier_score=metric.brier_score,
        cutoff=metric.cutoff,
        evaluable=metric.evaluable,
        blockers=metric.blockers,
    )
