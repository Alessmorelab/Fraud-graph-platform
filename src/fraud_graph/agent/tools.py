from __future__ import annotations

from typing import Any

from fraud_graph.agent.registry import ACCOUNT_TOOL_NAMES, SYSTEM_TOOL_NAMES, TOOL_NAMES
from fraud_graph.agent.schemas import AgentToolTrace
from fraud_graph.domain import AssociatedAccount, EvidencePacket
from fraud_graph.evidence.presentation import (
    blocker_label,
    fact_description,
    fact_label,
    localize_action,
    localize_limitation,
    pattern_label,
)


class ToolRejectedError(ValueError):
    pass


class EvidenceToolRegistry:
    def __init__(
        self,
        account_id: str,
        packet: EvidencePacket,
        associations: list[AssociatedAccount],
        *,
        knowledge_domain: str = "account_investigation",
        system_context: dict[str, Any] | None = None,
    ) -> None:
        self.account_id = account_id
        self.packet = packet
        self.associations = associations
        self.knowledge_domain = knowledge_domain
        self.system_context = system_context or {}

    def execute(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
    ) -> tuple[Any, AgentToolTrace]:
        arguments = arguments or {}
        if name not in TOOL_NAMES:
            raise ToolRejectedError(f"未授权的工具：{name}")
        allowed_tools = (
            set(SYSTEM_TOOL_NAMES)
            if self.knowledge_domain == "system_explanation"
            else set(ACCOUNT_TOOL_NAMES)
        )
        if name not in allowed_tools:
            raise ToolRejectedError("工具与当前提问范围不匹配")
        requested_account = arguments.pop("account_id", self.account_id)
        if str(requested_account) != self.account_id:
            raise ToolRejectedError("工具调用不得跨账户读取证据")
        allowed_arguments = (
            {"limit"}
            if name in {"get_suspicious_paths", "get_associated_accounts"}
            else set()
        )
        unexpected = set(arguments) - allowed_arguments
        if unexpected:
            raise ToolRejectedError(f"工具参数不受支持：{', '.join(sorted(unexpected))}")
        result = getattr(self, name)(**arguments)
        count = len(result) if isinstance(result, list) else 1
        return result, AgentToolTrace(tool=name, status="ok", result_count=count)

    def get_risk_summary(self) -> dict[str, Any]:
        score = self.packet.score
        return {
            "account_id": self.account_id,
            "risk_score": score.risk_score,
            "risk_rank": score.risk_rank,
            "evidence_grade": self.packet.grade.value,
            "evidence_score": score.evidence_score,
            "anomaly_score": score.anomaly_score,
            "production_eligible": score.production_eligible,
            "model_version": score.model_version,
            "data_version": score.data_version,
        }

    def get_structured_evidence(self) -> list[dict[str, Any]]:
        return [
            {
                "code": fact.code,
                "name": fact_label(fact.code),
                "description": fact_description(fact.code, fact.description),
                "value": fact.value,
                "transaction_ids": fact.transaction_ids,
            }
            for fact in self.packet.facts
        ]

    def get_suspicious_paths(self, limit: int = 5) -> list[dict[str, Any]]:
        normalized_limit = _bounded_limit(limit)
        return [item.model_dump(mode="json") for item in self.packet.paths[:normalized_limit]]

    def get_associated_accounts(self, limit: int = 10) -> list[dict[str, Any]]:
        normalized_limit = _bounded_limit(limit)
        return [item.model_dump(mode="json") for item in self.associations[:normalized_limit]]

    def get_flow_patterns(self) -> list[dict[str, Any]]:
        return [
            {
                **item.model_dump(mode="json"),
                "pattern_name": pattern_label(item.pattern_type),
            }
            for item in self.packet.patterns
        ]

    def get_report_boundary(self) -> dict[str, Any]:
        return {
            "production_eligible": self.packet.score.production_eligible,
            "eligibility_blockers": [
                {"code": code, "name": blocker_label(code)}
                for code in self.packet.score.eligibility_blockers
            ],
            "limitations": [localize_limitation(item) for item in self.packet.limitations],
            "recommended_actions": [
                localize_action(item) for item in self.packet.recommended_actions
            ],
            "prohibited_actions": [
                "自动认定账户涉诈",
                "自动冻结或止付",
                "自动提交人工复核结果",
                "修改证据包或报告哈希",
            ],
        }

    def get_model_methodology(self) -> dict[str, Any]:
        return self._system_entry("model_methodology")

    def get_risk_scoring_policy(self) -> dict[str, Any]:
        return self._system_entry("risk_scoring_policy")

    def get_model_evaluation(self) -> dict[str, Any]:
        return self._system_entry("model_evaluation")

    def get_task4_requirements(self) -> dict[str, Any]:
        return self._system_entry("task4_requirements")

    def _system_entry(self, key: str) -> dict[str, Any]:
        value = self.system_context.get(key)
        if not isinstance(value, dict):
            raise ToolRejectedError("系统解释知识尚未配置")
        return value


def _bounded_limit(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 20:
        raise ToolRejectedError("limit 必须是 1 至 20 的整数")
    return value
