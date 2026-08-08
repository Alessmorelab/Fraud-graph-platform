from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from fastapi.testclient import TestClient

from fraud_graph.domain import EvidenceGrade, EvidencePacket, RiskScore
from fraud_graph.errors import ProductionSafetyError
from fraud_graph.evidence.cases import export_illustrative_cases
from fraud_graph.service.app import create_app
from fraud_graph.service.review_workflow import (
    ReviewDecisionInput,
    ReviewerPrincipal,
    ReviewWorkflow,
)


def _case_packets() -> list[EvidencePacket]:
    return [
        EvidencePacket(
            account_id=account_id,
            generated_at=datetime(2025, 1, 1, tzinfo=UTC),
            score=RiskScore(
                account_id=account_id,
                risk_score=0.8,
                risk_rank=rank,
                model_version="m",
                data_version="d",
                feature_as_of=datetime(2025, 1, 1, tzinfo=UTC),
                has_transactions=False,
            ),
            grade=EvidenceGrade.C,
            facts=[],
            paths=[],
            limitations=["No transaction evidence."],
            recommended_actions=["Human review only."],
        )
        for rank, account_id in enumerate(("a", "b", "c", "d", "e"), start=1)
    ]


def _decision(workflow: ReviewWorkflow, reviewer_type: str) -> ReviewDecisionInput:
    detail = workflow.case_detail("case-01")
    return ReviewDecisionInput(
        case_id=detail.case_id,
        packet_sha256=detail.packet_sha256,
        review_type="primary",
        independence_attested=True,
        evidence_consistent=True,
        path_explanation_pass=True,
        readability_pass=True,
        evidence_types_verified=["MODEL", "BEHAVIOR"],
        notes=reviewer_type,
    )


def test_review_workbench_is_read_only_by_default(tmp_path: Path) -> None:
    manifest = export_illustrative_cases(_case_packets(), tmp_path / "cases", score_version="s")
    workflow = ReviewWorkflow(tmp_path / "cases" / manifest.case_sample_version)

    class State:
        review_workflow = workflow
        trust_review_identity_headers = False

    client = TestClient(create_app(State()))  # type: ignore[arg-type]
    assert client.get("/reviews").status_code == 200
    assert client.get("/assets/reviews.js").status_code == 200
    cases = client.get("/v1/reviews/cases")
    assert cases.status_code == 200
    assert cases.json()["write_enabled"] is False
    assert len(cases.json()["items"]) == 5
    assert cases.json()["items"][0]["status"] == "awaiting_primary_reviews"
    assert client.get("/v1/reviews/cases/case-01/report").status_code == 200
    assert client.get("/v1/reviews/templates/primary-reviewer-1.json").status_code == 200
    assert (
        client.post(
            "/v1/reviews/submissions",
            json={
                "case_id": "case-01",
                "packet_sha256": "0" * 64,
                "independence_attested": True,
                "evidence_consistent": True,
                "path_explanation_pass": True,
                "readability_pass": True,
                "evidence_types_verified": ["MODEL", "BEHAVIOR"],
            },
        ).status_code
        == 503
    )


def test_review_workflow_accepts_hash_bound_primary_reviews(tmp_path: Path) -> None:
    manifest = export_illustrative_cases(_case_packets(), tmp_path / "cases", score_version="s")
    workflow = ReviewWorkflow(
        tmp_path / "cases" / manifest.case_sample_version,
        submission_dir=tmp_path / "submissions",
        write_enabled=True,
    )

    first = workflow.submit(
        _decision(workflow, "first"),
        ReviewerPrincipal(reviewer_id="r1", reviewer_role="analyst"),
    )
    assert first.case_status == "awaiting_primary_reviews"
    second = workflow.submit(
        _decision(workflow, "second"),
        ReviewerPrincipal(reviewer_id="r2", reviewer_role="analyst"),
    )
    assert second.case_status == "passed"
    assert second.acceptance.resolved_cases == 1

    try:
        workflow.submit(
            _decision(workflow, "duplicate"),
            ReviewerPrincipal(reviewer_id="r1", reviewer_role="analyst"),
        )
    except ProductionSafetyError as exc:
        assert "closed" in str(exc)
    else:
        raise AssertionError("closed primary review phase accepted a third primary review")
