from __future__ import annotations

from datetime import UTC, datetime

from fastapi.testclient import TestClient

from fraud_graph.domain import (
    AccountWorkspace,
    EvidenceGrade,
    EvidencePacket,
    OperationsSummary,
    RiskQueueItem,
    RiskQueuePage,
    RiskScore,
)
from fraud_graph.service.app import create_app


class FakeMetadata:
    model_version = "m1"
    production_eligible = False
    eligibility_blockers = ["NOT_APPROVED"]


class FakeManifest:
    data_version = "d1"
    feature_version = "f1"


class FakeState:
    model_metadata = FakeMetadata()
    score_manifest = FakeManifest()

    def risk_score(self, account_id: str) -> RiskScore:
        if account_id != "1":
            raise KeyError(account_id)
        return RiskScore(
            account_id="1",
            risk_score=0.8,
            risk_rank=2,
            model_version="m1",
            data_version="d1",
            feature_as_of=datetime(2025, 1, 1, tzinfo=UTC),
            has_transactions=False,
        )

    def evidence(self, account_id: str) -> EvidencePacket:
        score = self.risk_score(account_id)
        return EvidencePacket(
            account_id=account_id,
            generated_at=datetime.now(UTC),
            score=score,
            grade=EvidenceGrade.C,
            facts=[],
            paths=[],
            limitations=["No transactions."],
            recommended_actions=["Review."],
        )

    def operations_summary(self) -> OperationsSummary:
        return OperationsSummary(
            scored_accounts=1,
            active_accounts=0,
            review_capacity_count=1,
            review_capacity_fraction=0.05,
            model_version="m1",
            data_version="d1",
            feature_version="f1",
            feature_as_of=datetime(2025, 1, 1, tzinfo=UTC),
            model_training_mode="retrospective_static",
            production_eligible=False,
            eligibility_blockers=["NOT_APPROVED"],
        )

    def risk_queue(
        self,
        *,
        query: str = "",
        active_only: bool = True,
        offset: int = 0,
        limit: int = 50,
    ) -> RiskQueuePage:
        item = RiskQueueItem(
            account_id="1",
            risk_score=0.8,
            risk_rank=2,
            has_transactions=False,
            model_training_mode="retrospective_static",
            production_eligible=False,
        )
        items = [] if active_only or (query and query not in "1") else [item]
        return RiskQueuePage(total=len(items), offset=offset, limit=limit, items=items)

    def workspace(self, account_id: str, association_limit: int = 20) -> AccountWorkspace:
        return AccountWorkspace(
            account_id=account_id,
            evidence=self.evidence(account_id),
            associations=[],
        )


def test_risk_endpoint_and_request_id() -> None:
    client = TestClient(create_app(FakeState()))  # type: ignore[arg-type]
    response = client.get("/v1/accounts/1/risk", headers={"x-request-id": "request-1"})

    assert response.status_code == 200
    assert response.json()["risk_score"] == 0.8
    assert response.headers["x-request-id"] == "request-1"
    assert client.get("/v1/accounts/missing/risk").status_code == 404


def test_operations_workbench_and_frontend_contracts() -> None:
    client = TestClient(create_app(FakeState()))  # type: ignore[arg-type]

    home = client.get("/")
    assert home.status_code == 200
    assert "反诈资金图谱" in home.text
    assert client.get("/assets/app.js").status_code == 200
    assert client.get("/assets/styles.css").status_code == 200

    summary = client.get("/v1/operations/summary")
    assert summary.status_code == 200
    assert summary.json()["production_eligible"] is False

    queue = client.get("/v1/accounts", params={"active_only": False})
    assert queue.status_code == 200
    assert queue.json()["items"][0]["account_id"] == "1"

    workspace = client.get("/v1/accounts/1/workspace")
    assert workspace.status_code == 200
    assert workspace.json()["evidence"]["score"]["risk_score"] == 0.8

    capabilities = client.get("/v1/capabilities")
    assert capabilities.status_code == 200
    statuses = {item["key"]: item["status"] for item in capabilities.json()}
    assert statuses["risk_queue"] == "available"
    assert statuses["manual_review"] == "contract_ready"
    assert statuses["iam"] == "upstream_required"
