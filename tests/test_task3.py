from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pyarrow as pa
import pyarrow.parquet as pq

from fraud_graph.domain import EvidenceGrade, EvidencePacket, PathStep, RiskScore, SuspiciousPath
from fraud_graph.evidence.task3 import (
    Task3ArtifactManifest,
    _prepare_compatibility_score,
    _task3_version,
    detect_flow_patterns,
)
from fraud_graph.features.builder import FeatureManifest
from fraud_graph.graph.path_engine import PathSearchConfig
from fraud_graph.modeling.evaluation import BinaryMetrics
from fraud_graph.modeling.risk_model import ScoreManifest
from fraud_graph.service.app import ServiceState


def _direct_path(
    path_id: str,
    root: str,
    payer: str,
    payee: str,
    event_time: datetime,
    amount: str,
    direction: str,
) -> SuspiciousPath:
    return SuspiciousPath(
        path_id=path_id,
        root_account_id=root,
        direction=direction,
        score=0.8,
        steps=[
            PathStep(
                transaction_id=path_id,
                payer_account_id=payer,
                payee_account_id=payee,
                event_time_utc=event_time,
                amount=Decimal(amount),
            )
        ],
        evidence_codes=["DIRECT_TRANSACTION"],
    )


def test_detect_flow_patterns_covers_four_structures() -> None:
    root = "root"
    start = datetime(2025, 1, 1, tzinfo=UTC)
    paths = [
        _direct_path("in-a", root, "a", root, start, "100", "incoming"),
        _direct_path("in-b", root, "b", root, start + timedelta(seconds=10), "200", "incoming"),
        _direct_path("out-a", root, root, "a", start + timedelta(seconds=20), "95", "outgoing"),
        _direct_path("out-c", root, root, "c", start + timedelta(seconds=30), "180", "outgoing"),
    ]
    patterns = detect_flow_patterns(
        root,
        paths,
        PathSearchConfig(
            max_hops=4,
            max_gap=timedelta(hours=24),
            max_paths=20,
            min_amount_ratio=0.3,
            max_amount_ratio=3.0,
        ),
    )
    assert {pattern.pattern_type for pattern in patterns} == {
        "FAN_IN",
        "FAN_OUT",
        "PASS_THROUGH",
        "RECIPROCAL",
    }
    assert all(pattern.transaction_ids for pattern in patterns)


def test_task2_score_compatibility_artifact_is_version_bound(tmp_path: Path) -> None:
    experiment_dir = tmp_path / "experiments" / "exp"
    experiment_dir.mkdir(parents=True)
    metric = BinaryMetrics(
        roc_auc=0.9,
        pr_auc=0.1,
        recall_at_top_fraction=0.5,
        precision_at_top_fraction=0.05,
        top_fraction=0.05,
        cutoff=1,
        prevalence=0.1,
        positives=1,
        negatives=9,
        rows=10,
        evaluable=True,
    )
    metadata = SimpleNamespace(
        experiment_version="exp",
        data_version="data",
        feature_version="features",
        snapshot_set_version="snapshots",
        created_at=datetime(2025, 1, 1, tzinfo=UTC),
        implementation_version="impl",
        positive_labels=[1],
        random_seed=7,
        evaluation_scheme="static",
        eligibility_blockers=["LABEL_TIME_MISSING", "RETROSPECTIVE_STATIC_TARGET"],
        feature_groups={"behavior": ["txn_count_total"], "profile_static_only": ["region_code"]},
        test_metrics={"selected_model": metric},
        training_mode="retrospective_static",
        files={"scores": "scores.parquet", "evaluation_model": "evaluation_model.joblib"},
    )
    pq.write_table(
        pa.table(
            {
                "account_id": ["a"],
                "risk_score": [0.8],
                "risk_rank": [1],
                "has_transactions": [True],
                "model_training_mode": ["retrospective_static"],
                "model_feature_set": ["combined"],
                "production_eligible": [False],
                "evidence_score": [0.7],
                "anomaly_score": [0.6],
            }
        ),
        experiment_dir / "scores.parquet",
    )
    feature_manifest = FeatureManifest(
        feature_version="features",
        data_version="data",
        created_at=datetime(2025, 1, 1, tzinfo=UTC),
        as_of=datetime(2025, 1, 2, tzinfo=UTC),
        lookback_days=30,
        row_count=1,
        behavior_features=["txn_count_total"],
        profile_features=["region_code"],
        file="features.parquet",
    )
    score_manifest, score_dir = _prepare_compatibility_score(
        tmp_path / "artifacts",
        experiment_dir,
        metadata,
        feature_manifest,
    )
    assert score_manifest.row_count == 1
    assert score_manifest.model_version == "exp"
    assert (score_dir / score_manifest.file).is_file()
    assert (tmp_path / "artifacts" / "models" / "exp" / "metadata.json").is_file()


def test_task3_version_changes_with_path_configuration() -> None:
    metadata = SimpleNamespace(experiment_version="exp")
    score_manifest = ScoreManifest(
        implementation_version="score",
        score_version="score-v1",
        model_version="model",
        feature_version="feature",
        data_version="data",
        created_at=datetime(2025, 1, 1, tzinfo=UTC),
        feature_as_of=datetime(2025, 1, 2, tzinfo=UTC),
        row_count=1,
    )
    base = PathSearchConfig(
        max_hops=4,
        max_gap=timedelta(hours=24),
        max_paths=20,
        min_amount_ratio=0.3,
        max_amount_ratio=3.0,
    )
    changed = PathSearchConfig(
        max_hops=3,
        max_gap=base.max_gap,
        max_paths=base.max_paths,
        min_amount_ratio=base.min_amount_ratio,
        max_amount_ratio=base.max_amount_ratio,
    )
    base_version = _task3_version(
        metadata, score_manifest, base, candidate_limit=40, case_limit=5, top_k=20
    )
    changed_version = _task3_version(
        metadata, score_manifest, changed, candidate_limit=40, case_limit=5, top_k=20
    )
    assert base_version != changed_version


def test_service_loads_latest_task3_cache_and_graph_payload(tmp_path: Path) -> None:
    task3_dir = tmp_path / "task3" / "v1"
    task3_dir.mkdir(parents=True)
    packet = EvidencePacket(
        account_id="a",
        generated_at=datetime(2025, 1, 1, tzinfo=UTC),
        score=RiskScore(
            account_id="a",
            risk_score=0.8,
            risk_rank=1,
            model_version="m",
            data_version="d",
            feature_as_of=datetime(2025, 1, 1, tzinfo=UTC),
            has_transactions=False,
        ),
        grade=EvidenceGrade.C,
        facts=[{"code": "MODEL_RISK_SCORE", "description": "score", "value": 0.8}],
        paths=[],
        limitations=["none"],
        recommended_actions=["review"],
    )
    files = {
        "root_accounts": "roots.parquet",
        "evidence_packets": "packets.json",
        "association_map": "associations.json",
    }
    manifest = Task3ArtifactManifest(
        task3_version="v1",
        experiment_version="exp",
        score_version="score",
        case_sample_version="case",
        data_version="d",
        feature_version="f",
        snapshot_set_version="s",
        created_at=datetime(2025, 1, 1, tzinfo=UTC),
        root_count=1,
        case_count=1,
        top_k=20,
        candidate_limit=40,
        path_config={},
        files=files,
    )
    (task3_dir / "manifest.json").write_text(manifest.model_dump_json(), encoding="utf-8")
    (task3_dir / "packets.json").write_text(json.dumps({"a": packet.model_dump(mode="json")}))
    (task3_dir / "associations.json").write_text(json.dumps({"a": []}))
    pq.write_table(pa.table({"account_id": ["a"], "risk_rank": [1]}), task3_dir / "roots.parquet")

    state = object.__new__(ServiceState)
    state._load_task3_cache(tmp_path, "score")
    payload = state.task3_account_graph("a")
    assert state.task3_available is True
    assert payload["account_id"] == "a"
    assert payload["root"]["risk_rank"] == 1
