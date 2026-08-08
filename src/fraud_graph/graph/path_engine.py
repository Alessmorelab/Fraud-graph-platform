from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from datetime import datetime, timedelta

from fraud_graph.domain import PathStep, SuspiciousPath, TransactionEvent
from fraud_graph.graph.repository import EventRepository


@dataclass(frozen=True)
class PathSearchConfig:
    max_hops: int = 4
    max_gap: timedelta = timedelta(hours=24)
    max_paths: int = 20
    min_amount_ratio: float = 0.3
    max_amount_ratio: float = 3.0
    max_events_per_node: int = 100


@dataclass
class _Candidate:
    current_account: str
    steps: list[TransactionEvent]
    visited: frozenset[str]
    score_sum: float


class TemporalPathEngine:
    def __init__(self, repository: EventRepository, config: PathSearchConfig) -> None:
        self._repository = repository
        self._config = config

    @property
    def max_paths(self) -> int:
        return self._config.max_paths

    def find_paths(
        self,
        root_account_id: str,
        start_time: datetime,
        end_time: datetime,
        *,
        direction: str = "outgoing",
    ) -> list[SuspiciousPath]:
        if direction not in {"outgoing", "incoming"}:
            raise ValueError("direction must be outgoing or incoming")
        events = self._collect_neighborhood(root_account_id, start_time, end_time)
        adjacency = self._build_adjacency(events, direction)
        candidates = [
            _Candidate(
                current_account=root_account_id,
                steps=[],
                visited=frozenset({root_account_id}),
                score_sum=0.0,
            )
        ]
        completed: list[_Candidate] = []
        for _ in range(self._config.max_hops):
            expanded: list[_Candidate] = []
            for candidate in candidates:
                prior = candidate.steps[-1] if candidate.steps else None
                possible = adjacency.get(candidate.current_account, [])
                for event in possible[: self._config.max_events_per_node]:
                    next_account = (
                        event.payee_account_id
                        if direction == "outgoing"
                        else event.payer_account_id
                    )
                    if next_account in candidate.visited:
                        continue
                    edge_score = self._edge_score(prior, event, direction)
                    if edge_score is None:
                        continue
                    expanded.append(
                        _Candidate(
                            current_account=next_account,
                            steps=[*candidate.steps, event],
                            visited=candidate.visited | {next_account},
                            score_sum=candidate.score_sum + edge_score,
                        )
                    )
            if not expanded:
                break
            expanded.sort(key=self._candidate_score, reverse=True)
            candidates = expanded[: max(self._config.max_paths * 10, 100)]
            completed.extend(candidates)
        completed.sort(key=self._candidate_score, reverse=True)
        return [
            self._to_path(root_account_id, candidate, direction)
            for candidate in completed[: self._config.max_paths]
        ]

    def _collect_neighborhood(
        self, root_account_id: str, start_time: datetime, end_time: datetime
    ) -> list[TransactionEvent]:
        frontier = {root_account_id}
        visited = {root_account_id}
        events: dict[str, TransactionEvent] = {}
        for _ in range(self._config.max_hops):
            batch = self._repository.events_touching(frontier, start_time, end_time)
            next_frontier: set[str] = set()
            for event in batch:
                events[event.transaction_id] = event
                for account_id in (event.payer_account_id, event.payee_account_id):
                    if account_id not in visited:
                        next_frontier.add(account_id)
            if not next_frontier:
                break
            visited.update(next_frontier)
            frontier = next_frontier
        return list(events.values())

    def _build_adjacency(
        self, events: list[TransactionEvent], direction: str
    ) -> dict[str, list[TransactionEvent]]:
        adjacency: dict[str, list[TransactionEvent]] = {}
        for event in events:
            key = event.payer_account_id if direction == "outgoing" else event.payee_account_id
            adjacency.setdefault(key, []).append(event)
        reverse = direction == "incoming"
        for values in adjacency.values():
            values.sort(key=lambda event: event.event_time_utc, reverse=reverse)
        return adjacency

    def _edge_score(
        self,
        prior: TransactionEvent | None,
        current: TransactionEvent,
        direction: str,
    ) -> float | None:
        if prior is None:
            return 0.7
        if direction == "outgoing":
            gap = current.event_time_utc - prior.event_time_utc
        else:
            gap = prior.event_time_utc - current.event_time_utc
        if gap < timedelta(0) or gap > self._config.max_gap:
            return None
        previous_amount = abs(prior.amount)
        current_amount = abs(current.amount)
        if previous_amount == 0 or current_amount == 0:
            return None
        ratio = float(current_amount / previous_amount)
        if ratio < self._config.min_amount_ratio or ratio > self._config.max_amount_ratio:
            return None
        speed_score = math.exp(-gap.total_seconds() / self._config.max_gap.total_seconds())
        amount_score = math.exp(-abs(math.log(ratio)))
        return 0.55 * amount_score + 0.45 * speed_score

    @staticmethod
    def _candidate_score(candidate: _Candidate) -> float:
        if not candidate.steps:
            return 0.0
        mean_score = candidate.score_sum / len(candidate.steps)
        return max(0.0, min(1.0, mean_score * (0.96 ** (len(candidate.steps) - 1))))

    def _to_path(
        self, root_account_id: str, candidate: _Candidate, direction: str
    ) -> SuspiciousPath:
        chronological = (
            candidate.steps if direction == "outgoing" else list(reversed(candidate.steps))
        )
        steps: list[PathStep] = []
        previous: TransactionEvent | None = None
        for event in chronological:
            gap_seconds = None
            amount_ratio = None
            if previous is not None:
                gap_seconds = int((event.event_time_utc - previous.event_time_utc).total_seconds())
                if previous.amount != 0:
                    amount_ratio = float(abs(event.amount / previous.amount))
            steps.append(
                PathStep(
                    transaction_id=event.transaction_id,
                    payer_account_id=event.payer_account_id,
                    payee_account_id=event.payee_account_id,
                    event_time_utc=event.event_time_utc,
                    amount=event.amount,
                    gap_seconds=gap_seconds,
                    amount_ratio_to_previous=amount_ratio,
                )
            )
            previous = event
        path_material = "|".join(step.transaction_id for step in steps)
        path_id = "path_" + hashlib.sha256(path_material.encode()).hexdigest()[:16]
        evidence_codes = ["DIRECT_TRANSACTION"]
        if len(steps) > 1:
            evidence_codes = ["TEMPORAL_CHAIN", "RAPID_SUCCESSION", "AMOUNT_CONTINUITY"]
        return SuspiciousPath(
            path_id=path_id,
            root_account_id=root_account_id,
            direction=direction,
            score=self._candidate_score(candidate),
            steps=steps,
            evidence_codes=evidence_codes,
        )
