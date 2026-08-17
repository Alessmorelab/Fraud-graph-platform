from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Literal

from fraud_graph.domain import AssociatedAccount, FlowPattern, SuspiciousPath


def rank_associated_accounts(
    root_account_id: str,
    paths: list[SuspiciousPath],
    *,
    limit: int = 20,
    risk_scores: Mapping[str, float] | None = None,
    evidence_scores: Mapping[str, float] | None = None,
    patterns: Sequence[FlowPattern] = (),
) -> list[AssociatedAccount]:
    if limit < 1:
        raise ValueError("limit must be positive")

    aggregates: dict[str, dict[str, Any]] = {}
    for path in paths:
        if path.direction == "outgoing":
            traversal = [
                (step.payee_account_id, hop, step.transaction_id)
                for hop, step in enumerate(path.steps, start=1)
            ]
        else:
            traversal = [
                (step.payer_account_id, hop, step.transaction_id)
                for hop, step in enumerate(reversed(path.steps), start=1)
            ]

        supporting_transactions: list[str] = []
        for account_id, hop, transaction_id in traversal:
            supporting_transactions.append(transaction_id)
            if account_id == root_account_id:
                continue
            association_score = path.score * (0.9 ** (hop - 1))
            aggregate = aggregates.setdefault(
                account_id,
                {
                    "score": association_score,
                    "min_hops": hop,
                    "directions": set(),
                    "transaction_ids": set(),
                    "path_ids": set(),
                    "evidence_codes": set(),
                    "supporting_path_count": 0,
                    "pattern_types": set(),
                },
            )
            aggregate["score"] = max(aggregate["score"], association_score)
            aggregate["min_hops"] = min(aggregate["min_hops"], hop)
            aggregate["directions"].add(path.direction)
            aggregate["transaction_ids"].update(supporting_transactions)
            aggregate["path_ids"].add(path.path_id)
            aggregate["evidence_codes"].update(path.evidence_codes)
            aggregate["supporting_path_count"] += 1

    for pattern in patterns:
        for account_id in pattern.related_account_ids:
            maybe_aggregate = aggregates.get(account_id)
            if maybe_aggregate is not None:
                maybe_aggregate["pattern_types"].add(pattern.pattern_type)

    ranked = []
    for account_id, aggregate in aggregates.items():
        directions: set[str] = aggregate["directions"]
        direction: Literal["incoming", "outgoing", "both"]
        if directions == {"incoming"}:
            direction = "incoming"
        elif directions == {"outgoing"}:
            direction = "outgoing"
        else:
            direction = "both"
        ranked.append(
            AssociatedAccount(
                root_account_id=root_account_id,
                account_id=account_id,
                association_score=aggregate["score"],
                direction=direction,
                min_hops=aggregate["min_hops"],
                transaction_ids=sorted(aggregate["transaction_ids"]),
                path_ids=sorted(aggregate["path_ids"]),
                evidence_codes=sorted(aggregate["evidence_codes"]),
                supporting_path_count=max(1, int(aggregate["supporting_path_count"])),
                associated_risk_score=(
                    float(risk_scores[account_id])
                    if risk_scores is not None and account_id in risk_scores
                    else None
                ),
                associated_evidence_score=(
                    float(evidence_scores[account_id])
                    if evidence_scores is not None and account_id in evidence_scores
                    else None
                ),
                pattern_types=sorted(aggregate["pattern_types"]),
            )
        )
    ranked.sort(key=lambda item: (-item.association_score, item.min_hops, item.account_id))
    return ranked[:limit]
