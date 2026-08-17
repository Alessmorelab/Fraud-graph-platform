from __future__ import annotations

import json
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
    review_page = client.get("/reviews")
    assert review_page.status_code == 200
    assert "API 合同" not in review_page.text
    assert 'href="/docs"' not in review_page.text
    assert client.get("/assets/reviews.js").status_code == 200
    cases = client.get("/v1/reviews/cases")
    assert cases.status_code == 200
    assert cases.json()["write_enabled"] is False
    assert len(cases.json()["items"]) == 5
    assert cases.json()["items"][0]["status"] == "awaiting_primary_reviews"
    markdown_report = client.get("/v1/reviews/cases/case-01/report")
    assert markdown_report.status_code == 200
    assert markdown_report.headers["content-type"].startswith("text/markdown")
    assert "No transaction evidence." in markdown_report.text
    assert "Human review only." in markdown_report.text
    html_report = client.get(
        "/v1/reviews/cases/case-01/report", params={"view": "html"}
    )
    assert html_report.status_code == 200
    assert html_report.headers["content-type"].startswith("text/html")
    assert "案例 case-01" in html_report.text
    assert 'class="report-page"' in html_report.text
    assert "当前没有可核验的交易证据。" in html_report.text
    assert "仅供人工复核。" in html_report.text
    review_script = client.get("/assets/reviews.js").text
    assert "report?view=html" in review_script
    assert "主模型风险分数" in review_script
    assert "字段代码" in review_script
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


def test_read_only_workbench_loads_offline_batch_reviews(tmp_path: Path) -> None:
    manifest = export_illustrative_cases(_case_packets(), tmp_path / "cases", score_version="s")
    case_dir = tmp_path / "cases" / manifest.case_sample_version
    for reviewer_id, template_file in zip(
        ("reviewer-1", "reviewer-2"),
        manifest.primary_review_template_files,
        strict=True,
    ):
        payload = json.loads((case_dir / template_file).read_text(encoding="utf-8"))
        for review in payload:
            review.update(
                reviewer_id=reviewer_id,
                reviewer_role="independent reviewer",
                reviewed_at="2026-08-17T15:30:00+08:00",
                independence_attested=True,
                evidence_consistent=True,
                path_explanation_pass=True,
                readability_pass=True,
                evidence_types_verified=["MODEL", "BEHAVIOR"],
                notes="independently reviewed",
            )
        target = case_dir / f"{reviewer_id}-submission.json"
        target.write_text(json.dumps(payload), encoding="utf-8")

    workflow = ReviewWorkflow(case_dir)

    assert workflow.write_enabled is False
    assert workflow.acceptance_status().acceptance_passed is True
    assert workflow.acceptance_status().human_reviews == 10
    assert {item.status for item in workflow.list_cases().items} == {"passed"}


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
