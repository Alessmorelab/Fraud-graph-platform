from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal

import pandas as pd
import pytest

from fraud_graph.domain import (
    EvidenceGrade,
    EvidencePacket,
    PathStep,
    RiskScore,
    SuspiciousPath,
)
from fraud_graph.errors import ProductionSafetyError
from fraud_graph.evidence.cases import (
    evaluate_case_sample_reports,
    evidence_packet_sha256,
    export_illustrative_cases,
)
from fraud_graph.evidence.evaluation import (
    evaluate_case_associations,
    evaluate_manual_cases,
    evaluate_report_consistency,
    validate_evidence_packet,
)


def test_association_evaluation_requires_case_truth() -> None:
    with pytest.raises(ProductionSafetyError, match="case-level truth"):
        evaluate_case_associations(
            {"root": ["other"]}, pd.DataFrame({"account_id": ["root"], "label": [1]})
        )


def test_association_metrics_are_computed() -> None:
    labels = pd.DataFrame(
        {
            "account_id": ["root", "other", "unrelated"],
            "label": [1, 0, 0],
            "case_id": ["case-1", "case-1", "case-2"],
        }
    )
    result = evaluate_case_associations({"root": ["other"]}, labels)
    assert result.root_hit_rate == 1.0
    assert result.associated_precision == 1.0


def test_manual_case_acceptance_requires_two_hash_bound_human_reviews() -> None:
    case_bindings = {f"case-{index}": f"{index:064x}" for index in range(1, 6)}
    reviews = []
    for case_id, packet_hash in case_bindings.items():
        for reviewer_id in ("reviewer-a", "reviewer-b"):
            reviews.append(
                {
                    "case_id": case_id,
                    "packet_sha256": packet_hash,
                    "reviewer_id": reviewer_id,
                    "reviewer_role": "analyst",
                    "reviewer_kind": "human",
                    "reviewed_at": "2025-01-01T08:00:00+08:00",
                    "independence_attested": True,
                    "evidence_consistent": True,
                    "path_explanation_pass": True,
                    "readability_pass": True,
                    "evidence_types_verified": ["MODEL", "TRANSACTION"],
                }
            )
    manual = evaluate_manual_cases(reviews, case_bindings)
    assert manual.evaluable is True
    assert manual.acceptance_passed is True
    assert manual.evidence_pass_rate == 1.0
    assert manual.path_explanation_pass_rate == 1.0
    assert manual.readability_pass_rate == 1.0


def test_automated_prechecks_do_not_count_as_human_reviews() -> None:
    case_bindings = {f"case-{index}": f"{index:064x}" for index in range(1, 6)}
    reviews = [
        {
            "case_id": case_id,
            "packet_sha256": packet_hash,
            "reviewer_id": "automated-check",
            "reviewer_role": "validator",
            "reviewer_kind": "automated_precheck",
            "reviewed_at": "2025-01-01T00:00:00Z",
            "independence_attested": False,
            "evidence_consistent": True,
            "path_explanation_pass": True,
            "readability_pass": True,
            "evidence_types_verified": ["MODEL", "TRANSACTION"],
        }
        for case_id, packet_hash in case_bindings.items()
    ]
    manual = evaluate_manual_cases(reviews, case_bindings)
    assert manual.evaluable is False
    assert manual.acceptance_passed is False
    assert "NO_HUMAN_REVIEWS" in manual.blockers


def test_report_consistency_rejects_uncited_transaction() -> None:
    score = RiskScore(
        account_id="a",
        risk_score=0.9,
        risk_rank=1,
        model_version="m",
        data_version="d",
        feature_as_of=datetime(2025, 1, 1, tzinfo=UTC),
        has_transactions=True,
    )
    packet = EvidencePacket(
        account_id="a",
        generated_at=datetime(2025, 1, 1, tzinfo=UTC),
        score=score,
        grade=EvidenceGrade.B,
        facts=[
            {"code": "MODEL_RISK_SCORE", "description": "score", "value": 0.9},
            {
                "code": "CITED_DIRECT_TRANSACTIONS",
                "description": "tx",
                "value": 1,
                "transaction_ids": ["not-in-path"],
            },
        ],
        paths=[
            SuspiciousPath(
                path_id="p1",
                root_account_id="a",
                direction="outgoing",
                score=0.8,
                steps=[
                    PathStep(
                        transaction_id="t1",
                        payer_account_id="a",
                        payee_account_id="b",
                        event_time_utc=datetime(2025, 1, 1, tzinfo=UTC),
                        amount=Decimal("10"),
                    )
                ],
                evidence_codes=["DIRECT_TRANSACTION"],
            )
        ],
        limitations=[],
        recommended_actions=[],
    )
    result = evaluate_report_consistency([packet])
    assert result.consistency_rate == 0.0
    assert "FACT_TRANSACTION_NOT_IN_PATH:CITED_DIRECT_TRANSACTIONS" in result.issues[0]["errors"]


def test_static_retrospective_packets_accept_canonical_blockers() -> None:
    score = RiskScore(
        account_id="a",
        risk_score=0.9,
        risk_rank=1,
        model_version="m",
        data_version="d",
        feature_as_of=datetime(2025, 1, 1, tzinfo=UTC),
        has_transactions=False,
        model_training_mode="retrospective_static",
        model_feature_set="combined",
        eligibility_blockers=["LABEL_TIME_MISSING", "RETROSPECTIVE_STATIC_TARGET"],
    )
    packet = EvidencePacket(
        account_id="a",
        generated_at=datetime(2025, 1, 1, tzinfo=UTC),
        score=score,
        grade=EvidenceGrade.C,
        facts=[
            {"code": "MODEL_RISK_SCORE", "description": "score", "value": 0.9},
            {"code": "PROFILE_REGION", "description": "region", "value": "east"},
        ],
        paths=[],
        limitations=["Static retrospective only."],
        recommended_actions=["Human review only."],
    )
    assert validate_evidence_packet(packet) == []


def test_illustrative_case_export_is_explicitly_not_evaluable(tmp_path) -> None:
    packets = []
    for account_id in ["a", "b", "c", "d", "e"]:
        packets.append(
            EvidencePacket(
                account_id=account_id,
                generated_at=datetime(2025, 1, 1, tzinfo=UTC),
                score=RiskScore(
                    account_id=account_id,
                    risk_score=0.8,
                    risk_rank=1,
                    model_version="m",
                    data_version="d",
                    feature_as_of=datetime(2025, 1, 1, tzinfo=UTC),
                    has_transactions=False,
                ),
                grade=EvidenceGrade.C,
                facts=[],
                paths=[],
                limitations=[],
                recommended_actions=[],
            )
        )
    manifest = export_illustrative_cases(packets, tmp_path, score_version="s")
    assert manifest.status == "pending_human_review"
    assert (tmp_path / manifest.case_sample_version / "cases.json").is_file()
    assert all(
        (tmp_path / manifest.case_sample_version / template_file).is_file()
        for template_file in manifest.primary_review_template_files
    )
    assert (tmp_path / manifest.case_sample_version / manifest.adjudication_template_file).is_file()


def test_case_sample_version_changes_when_packet_content_changes(tmp_path) -> None:
    packets = []
    for account_id in ["a", "b", "c", "d", "e"]:
        packets.append(
            EvidencePacket(
                account_id=account_id,
                generated_at=datetime(2025, 1, 1, tzinfo=UTC),
                score=RiskScore(
                    account_id=account_id,
                    risk_score=0.8,
                    risk_rank=1,
                    model_version="m",
                    data_version="d",
                    feature_as_of=datetime(2025, 1, 1, tzinfo=UTC),
                    has_transactions=False,
                ),
                grade=EvidenceGrade.C,
                facts=[],
                paths=[],
                limitations=[],
                recommended_actions=[],
            )
        )
    first = export_illustrative_cases(packets, tmp_path, score_version="s")
    changed = [packet.model_copy(deep=True) for packet in packets]
    changed[0].score = changed[0].score.model_copy(update={"risk_score": 0.81})
    second = export_illustrative_cases(changed, tmp_path, score_version="s")
    assert first.case_sample_version != second.case_sample_version


def test_task4_acceptance_verifies_structured_and_rendered_reports(tmp_path) -> None:
    packets = []
    for rank, account_id in enumerate(["a", "b", "c", "d", "e"], start=1):
        score = RiskScore(
            account_id=account_id,
            risk_score=0.8,
            risk_rank=rank,
            model_version="m",
            data_version="d",
            feature_as_of=datetime(2025, 1, 1, tzinfo=UTC),
            has_transactions=True,
            model_training_mode="retrospective_static",
            model_feature_set="behavior",
            eligibility_blockers=[
                "REFERENCE_MODEL_NOT_APPROVED",
                "NON_TEMPORAL_DIAGNOSTIC_EVALUATION",
                "LABEL_TIME_MISSING",
            ],
        )
        transaction_id = f"t-{account_id}"
        packets.append(
            EvidencePacket(
                account_id=account_id,
                generated_at=datetime(2025, 1, 1, tzinfo=UTC),
                score=score,
                grade=EvidenceGrade.B,
                facts=[
                    {"code": "MODEL_RISK_SCORE", "description": "score", "value": 0.8},
                    {
                        "code": "CITED_DIRECT_TRANSACTIONS",
                        "description": "transaction evidence",
                        "value": 1,
                        "transaction_ids": [transaction_id],
                    },
                ],
                paths=[
                    SuspiciousPath(
                        path_id=f"p-{account_id}",
                        root_account_id=account_id,
                        direction="outgoing",
                        score=0.8,
                        steps=[
                            PathStep(
                                transaction_id=transaction_id,
                                payer_account_id=account_id,
                                payee_account_id=f"other-{account_id}",
                                event_time_utc=datetime(2025, 1, 1, tzinfo=UTC),
                                amount=Decimal("10"),
                            )
                        ],
                        evidence_codes=["DIRECT_TRANSACTION"],
                    )
                ],
                limitations=["Static retrospective experiment."],
                recommended_actions=["Human review only."],
            )
        )
    manifest = export_illustrative_cases(packets, tmp_path, score_version="s")
    case_dir = tmp_path / manifest.case_sample_version
    records = json.loads((case_dir / "cases.json").read_text(encoding="utf-8"))
    assert records[0]["packet_sha256"] == evidence_packet_sha256(packets[0])
    acceptance = evaluate_case_sample_reports(case_dir)
    assert acceptance.acceptance_passed is True
    assert acceptance.exact_render_match_rate == 1.0
