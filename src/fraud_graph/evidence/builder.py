from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fraud_graph.domain import (
    EvidenceFact,
    EvidenceGrade,
    EvidencePacket,
    FlowPattern,
    RiskScore,
    SuspiciousPath,
)


def build_evidence_packet(
    score: RiskScore,
    paths: list[SuspiciousPath],
    feature_values: dict[str, Any] | None = None,
    patterns: list[FlowPattern] | None = None,
) -> EvidencePacket:
    features = feature_values or {}
    flow_patterns = patterns or []
    facts = [
        EvidenceFact(
            code="MODEL_RISK_SCORE",
            description="Versioned risk model score",
            value=round(score.risk_score, 6),
        )
    ]
    if score.has_transactions:
        for feature_name, description in (
            ("txn_count_total", "Transactions in the observation window"),
            ("counterparty_count_total", "Unique directed counterparties in the window"),
            ("same_day_in_out_days", "Days with both incoming and outgoing activity"),
            ("same_day_turnover_ratio_avg", "Average same-day inflow/outflow coverage"),
        ):
            if feature_name in features:
                facts.append(
                    EvidenceFact(
                        code=feature_name.upper(),
                        description=description,
                        value=_json_scalar(features[feature_name]),
                    )
                )
    multi_hop_paths = [path for path in paths if len(path.steps) >= 2]
    direct_paths = [path for path in paths if len(path.steps) == 1]
    if direct_paths:
        transaction_ids = sorted({path.steps[0].transaction_id for path in direct_paths})
        facts.append(
            EvidenceFact(
                code="CITED_DIRECT_TRANSACTIONS",
                description="Direct relationships backed by source transactions",
                value=len(direct_paths),
                transaction_ids=transaction_ids,
            )
        )
    if multi_hop_paths:
        transaction_ids = sorted(
            {step.transaction_id for path in multi_hop_paths for step in path.steps}
        )
        facts.append(
            EvidenceFact(
                code="SUPPORTED_MULTI_HOP_PATHS",
                description="Multi-hop time-respecting paths backed by source transactions",
                value=len(multi_hop_paths),
                transaction_ids=transaction_ids,
            )
        )
    if flow_patterns:
        pattern_transaction_ids = sorted(
            {
                transaction_id
                for pattern in flow_patterns
                for transaction_id in pattern.transaction_ids
            }
        )
        facts.append(
            EvidenceFact(
                code="FLOW_PATTERNS",
                description="Convergence, dispersion, pass-through, or reciprocal flow patterns",
                value=len(flow_patterns),
                transaction_ids=pattern_transaction_ids,
            )
        )

    limitations: list[str] = []
    if multi_hop_paths:
        grade = EvidenceGrade.A
    elif score.has_transactions:
        grade = EvidenceGrade.B
        if direct_paths:
            limitations.append(
                "Only direct transaction relationships were found; no qualifying multi-hop "
                "temporal path was established."
            )
        else:
            limitations.append(
                "No qualifying multi-hop temporal path was found in the configured window."
            )
    else:
        grade = EvidenceGrade.C
        limitations.append(
            "No transaction was observed in the configured window; no funding path can be asserted."
        )
    limitations.append(
        "Model output is decision support and does not authorize automatic freezing."
    )
    if not score.production_eligible:
        limitations.append(
            "The referenced model is not production-eligible and must not drive an operational "
            "decision."
        )
        if score.eligibility_blockers:
            limitations.append(
                "Model eligibility blockers: " + ", ".join(score.eligibility_blockers) + "."
            )
    return EvidencePacket(
        account_id=score.account_id,
        generated_at=datetime.now(UTC),
        score=score,
        grade=grade,
        facts=facts,
        paths=paths,
        patterns=flow_patterns,
        limitations=limitations,
        recommended_actions=_recommended_actions(grade),
    )


def _recommended_actions(grade: EvidenceGrade) -> list[str]:
    if grade == EvidenceGrade.A:
        return [
            "Prioritize manual case review.",
            "Verify the cited transactions and related accounts against authorized internal data.",
            "Escalate through the bank's existing approval workflow when corroborated.",
        ]
    if grade == EvidenceGrade.B:
        return [
            "Review behavioral anomalies and expand the observation window.",
            "Check additional authorized channels before escalation.",
        ]
    return [
        "Treat the score as unsupported by observed transaction evidence.",
        "Obtain additional transaction or channel evidence before case escalation.",
    ]


def _json_scalar(value: Any) -> float | int | str | None:
    if value is None:
        return None
    if hasattr(value, "item"):
        value = value.item()
    if isinstance(value, (float, int, str)):
        return value
    return str(value)
