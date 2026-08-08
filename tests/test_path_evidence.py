from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from fraud_graph.domain import EvidenceGrade, RiskScore, TransactionEvent
from fraud_graph.evidence.associations import rank_associated_accounts
from fraud_graph.evidence.builder import build_evidence_packet
from fraud_graph.graph.path_engine import PathSearchConfig, TemporalPathEngine
from fraud_graph.graph.repository import InMemoryEventRepository

START = datetime(2025, 1, 1, tzinfo=UTC)


def _event(txn: str, payer: str, payee: str, minutes: int, amount: str) -> TransactionEvent:
    return TransactionEvent(
        transaction_id=txn,
        payer_account_id=payer,
        payee_account_id=payee,
        event_time_utc=START + timedelta(minutes=minutes),
        amount=Decimal(amount),
    )


def test_outgoing_path_is_time_ordered_and_cites_transactions() -> None:
    events = [
        _event("t1", "a", "b", 0, "100"),
        _event("t2", "b", "c", 10, "90"),
        _event("t3", "c", "d", 20, "85"),
        _event("bad-time", "b", "x", -10, "90"),
        _event("bad-amount", "b", "y", 5, "1000"),
    ]
    engine = TemporalPathEngine(
        InMemoryEventRepository(events),
        PathSearchConfig(max_hops=3, max_gap=timedelta(hours=1), max_paths=5),
    )
    paths = engine.find_paths("a", START - timedelta(hours=1), START + timedelta(hours=2))

    assert paths
    assert [step.transaction_id for step in paths[0].steps] == ["t1", "t2", "t3"]
    assert paths[0].evidence_codes == [
        "TEMPORAL_CHAIN",
        "RAPID_SUCCESSION",
        "AMOUNT_CONTINUITY",
    ]


def test_evidence_grade_never_invents_a_path() -> None:
    score = RiskScore(
        account_id="isolated",
        risk_score=0.9,
        risk_rank=1,
        model_version="m1",
        data_version="d1",
        feature_as_of=START,
        has_transactions=False,
    )
    packet = build_evidence_packet(score, [])

    assert packet.grade == EvidenceGrade.C
    assert packet.paths == []
    assert "no funding path" in packet.limitations[0].lower()


def test_direct_transaction_is_not_graded_as_multi_hop_evidence() -> None:
    score = RiskScore(
        account_id="a",
        risk_score=0.8,
        risk_rank=2,
        model_version="m1",
        data_version="d1",
        feature_as_of=START,
        has_transactions=True,
    )
    engine = TemporalPathEngine(
        InMemoryEventRepository([_event("t1", "a", "b", 0, "100")]),
        PathSearchConfig(max_hops=3, max_gap=timedelta(hours=1), max_paths=5),
    )
    paths = engine.find_paths("a", START - timedelta(hours=1), START + timedelta(hours=1))
    packet = build_evidence_packet(score, paths)

    assert paths[0].evidence_codes == ["DIRECT_TRANSACTION"]
    assert packet.grade == EvidenceGrade.B
    assert any(fact.code == "CITED_DIRECT_TRANSACTIONS" for fact in packet.facts)
    assert not any(fact.code == "SUPPORTED_MULTI_HOP_PATHS" for fact in packet.facts)


def test_associations_are_ranked_and_transaction_backed() -> None:
    events = [
        _event("t1", "a", "b", 0, "100"),
        _event("t2", "b", "c", 10, "90"),
    ]
    engine = TemporalPathEngine(
        InMemoryEventRepository(events),
        PathSearchConfig(max_hops=2, max_gap=timedelta(hours=1), max_paths=5),
    )
    paths = engine.find_paths("a", START - timedelta(hours=1), START + timedelta(hours=1))
    associations = rank_associated_accounts("a", paths)

    assert [item.account_id for item in associations] == ["b", "c"]
    assert associations[0].min_hops == 1
    assert associations[1].min_hops == 2
    assert associations[1].transaction_ids == ["t1", "t2"]
