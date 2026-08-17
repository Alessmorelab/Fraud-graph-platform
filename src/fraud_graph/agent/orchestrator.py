from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import Any

from pydantic import ValidationError

from fraud_graph.agent.audit import log_agent_response
from fraud_graph.agent.fallback import (
    build_fallback_response,
    build_system_fallback_response,
)
from fraud_graph.agent.prompts import FORMAT_REPAIR_PROMPT, SYSTEM_PROMPT, user_prompt
from fraud_graph.agent.providers.deepseek import DeepSeekClient, DeepSeekConfig
from fraud_graph.agent.registry import SYSTEM_TOOL_NAMES, TOOL_NAMES, tool_definitions_for
from fraud_graph.agent.schemas import (
    AgentCapabilities,
    AgentDraft,
    AgentQuery,
    AgentResponse,
    AgentToolTrace,
    KnowledgeDomain,
)
from fraud_graph.agent.tools import EvidenceToolRegistry, ToolRejectedError
from fraud_graph.agent.verifier import (
    EvidenceVerificationError,
    reconcile_agent_citations,
    verify_agent_response,
)
from fraud_graph.domain import AssociatedAccount, EvidencePacket
from fraud_graph.evidence.cases import evidence_packet_sha256


class AgentService:
    def __init__(
        self,
        state: Any,
        *,
        config: DeepSeekConfig | None = None,
        client: DeepSeekClient | None = None,
        max_tool_steps: int = 6,
    ) -> None:
        self.state = state
        self.config = config or DeepSeekConfig.from_env()
        self.client = client or DeepSeekClient(self.config)
        self.max_tool_steps = max_tool_steps

    def capabilities(self) -> AgentCapabilities:
        return AgentCapabilities(
            external_model_enabled=self.config.external_enabled,
            external_model_ready=self.config.ready,
            provider_model=self.config.model,
            tools=TOOL_NAMES,
            guardrails=[
                "只读证据工具",
                "当前账户范围锁定",
                "证据引用白名单校验",
                "禁止自动冻结、止付和涉诈定性",
                "外部模型失败时确定性降级",
            ],
        )

    async def query(
        self,
        query: AgentQuery,
        *,
        provider_client: DeepSeekClient | None = None,
    ) -> AgentResponse:
        packet = await asyncio.to_thread(self.state.evidence, query.account_id)
        associations = await self._associations(query.account_id)
        knowledge_domain = _resolve_knowledge_domain(query)
        system_context = await self._system_context(packet)
        registry = EvidenceToolRegistry(
            query.account_id,
            packet,
            associations,
            knowledge_domain=knowledge_domain,
            system_context=system_context,
        )
        active_client = provider_client or self.client
        if not active_client.config.ready:
            response = self._fallback(
                packet,
                associations,
                query.question,
                registry,
                "DeepSeek 外部调用未启用或 API Key 未配置",
            )
            log_agent_response(response)
            return response
        try:
            response = await self._query_deepseek(
                query,
                packet,
                registry,
                active_client,
                knowledge_domain,
            )
            reconcile_agent_citations(response, packet, system_context)
            verify_agent_response(response, packet, system_context)
            response.verifier_passed = True
        except (
            DeepSeekProtocolError,
            EvidenceVerificationError,
            ToolRejectedError,
            ValidationError,
            ValueError,
            RuntimeError,
        ) as exc:
            response = self._fallback(
                packet, associations, query.question, registry, str(exc)
            )
        log_agent_response(response)
        return response

    async def _associations(self, account_id: str) -> list[AssociatedAccount]:
        method = getattr(self.state, "associations", None)
        if method is None:
            return []
        return await asyncio.to_thread(method, account_id, 20)

    async def _system_context(self, packet: EvidencePacket) -> dict[str, Any]:
        method = getattr(self.state, "agent_system_context", None)
        if method is not None:
            context = await asyncio.to_thread(method)
            if isinstance(context, dict) and context:
                return context
        return _default_system_context(packet)

    async def _query_deepseek(
        self,
        query: AgentQuery,
        packet: EvidencePacket,
        registry: EvidenceToolRegistry,
        client: DeepSeekClient,
        knowledge_domain: KnowledgeDomain,
    ) -> AgentResponse:
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": user_prompt(
                    query.account_id,
                    query.question,
                    knowledge_domain,
                ),
            },
        ]
        traces: list[AgentToolTrace] = []
        for _ in range(self.max_tool_steps):
            raw = await client.chat(messages, tool_definitions_for(knowledge_domain))
            message = _assistant_message(raw)
            tool_calls = message.get("tool_calls") or []
            if tool_calls:
                messages.append(message)
                for call in tool_calls:
                    call_id, name, arguments = _parse_tool_call(call)
                    try:
                        result, trace = registry.execute(name, arguments)
                    except ToolRejectedError as exc:
                        traces.append(
                            AgentToolTrace(
                                tool=name or "unknown",
                                status="rejected",
                                detail=str(exc),
                            )
                        )
                        raise
                    traces.append(trace)
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call_id,
                            "content": json.dumps(result, ensure_ascii=False),
                        }
                    )
                continue
            draft, final_raw = await _parse_or_repair_final(
                message.get("content"),
                messages,
                client,
            )
            return AgentResponse(
                account_id=query.account_id,
                knowledge_domain=knowledge_domain,
                answer=draft.answer,
                assessment=draft.assessment,
                key_findings=draft.key_findings,
                citations=draft.citations,
                tool_trace=traces,
                limitations=draft.limitations,
                recommended_actions=draft.recommended_actions,
                evidence_packet_sha256=evidence_packet_sha256(packet),
                model_version=packet.score.model_version,
                data_version=packet.score.data_version,
                production_eligible=False,
                verifier_passed=False,
                generation_mode="deepseek",
                provider_model=str(final_raw.get("model") or client.config.model),
                generated_at=datetime.now(UTC),
            )
        raise DeepSeekProtocolError("DeepSeek 工具调用超过最大步数")

    def _fallback(
        self,
        packet: EvidencePacket,
        associations: list[AssociatedAccount],
        question: str,
        registry: EvidenceToolRegistry,
        reason: str,
    ) -> AgentResponse:
        traces: list[AgentToolTrace] = []
        calls = (
            [(tool, {}) for tool in SYSTEM_TOOL_NAMES]
            if registry.knowledge_domain == "system_explanation"
            else [
                ("get_risk_summary", {}),
                ("get_structured_evidence", {}),
                ("get_suspicious_paths", {"limit": 5}),
                ("get_flow_patterns", {}),
                ("get_report_boundary", {}),
            ]
        )
        for tool, arguments in calls:
            _, trace = registry.execute(tool, arguments)
            traces.append(trace)
        if registry.knowledge_domain == "system_explanation":
            response = build_system_fallback_response(
                packet,
                question,
                registry.system_context,
                tool_trace=traces,
                reason=reason,
            )
        else:
            response = build_fallback_response(
                packet,
                associations,
                question,
                tool_trace=traces,
                reason=reason,
            )
        verify_agent_response(response, packet, registry.system_context)
        return response


class DeepSeekProtocolError(RuntimeError):
    pass


def _resolve_knowledge_domain(query: AgentQuery) -> KnowledgeDomain:
    if query.knowledge_domain != "auto":
        return query.knowledge_domain
    normalized = query.question.casefold()
    system_terms = (
        "上线阻断项",
        "判断风险账户的标准",
        "风险账户的标准",
        "风险分数、证据分数",
        "风险分数和证据分数",
        "异常分数",
        "模型使用",
        "模型达到",
        "模型指标",
        "比赛指标",
        "任务四",
        "任务4",
        "类别平衡",
        "特征分组",
        "roc-auc",
        "pr-auc",
        "top5%",
        "top 5%",
    )
    return (
        "system_explanation"
        if any(term in normalized for term in system_terms)
        else "account_investigation"
    )


def _default_system_context(packet: EvidencePacket) -> dict[str, Any]:
    blockers = [
        {"code": code, "name": code}
        for code in packet.score.eligibility_blockers
    ]
    return {
        "model_methodology": {
            "citation": {
                "kind": "system",
                "id": "MODEL_CARD",
                "label": "模型卡与特征清单",
            },
            "selected_model": packet.score.model_version,
            "feature_count": "当前版本记录的",
        },
        "risk_scoring_policy": {
            "citation": {
                "kind": "system",
                "id": "RISK_SCORING_POLICY",
                "label": "风险评分与上线边界",
            },
            "online_blockers": blockers,
        },
        "model_evaluation": {
            "citation": {
                "kind": "system",
                "id": "MODEL_EVALUATION",
                "label": "模型评估结果",
            },
            "test_metrics": {},
        },
        "task4_requirements": {
            "citation": {
                "kind": "system",
                "id": "TASK4_REQUIREMENTS",
                "label": "任务四报告要求与验收",
            },
            "acceptance": {},
        },
    }


def _assistant_message(payload: dict[str, Any]) -> dict[str, Any]:
    try:
        message = payload["choices"][0]["message"]
    except (KeyError, IndexError, TypeError) as exc:
        raise DeepSeekProtocolError("DeepSeek 响应缺少 choices[0].message") from exc
    if not isinstance(message, dict):
        raise DeepSeekProtocolError("DeepSeek assistant message 格式无效")
    return message


def _parse_tool_call(call: Any) -> tuple[str, str, dict[str, Any]]:
    try:
        call_id = str(call["id"])
        function = call["function"]
        name = str(function["name"])
        raw_arguments = function.get("arguments", "{}")
        arguments = json.loads(raw_arguments) if isinstance(raw_arguments, str) else raw_arguments
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise DeepSeekProtocolError("DeepSeek 工具调用格式无效") from exc
    if not isinstance(arguments, dict):
        raise DeepSeekProtocolError("DeepSeek 工具参数必须是 JSON 对象")
    return call_id, name, arguments


async def _parse_or_repair_final(
    content: Any,
    messages: list[dict[str, Any]],
    client: DeepSeekClient,
) -> tuple[AgentDraft, dict[str, Any]]:
    try:
        return _parse_final_json(content), {}
    except (DeepSeekProtocolError, ValidationError):
        repair_messages = [*messages]
        if isinstance(content, str) and content.strip():
            repair_messages.append({"role": "assistant", "content": content})
        repair_messages.append({"role": "user", "content": FORMAT_REPAIR_PROMPT})

    final_raw: dict[str, Any] = {}
    for attempt in range(2):
        final_raw = await client.chat_structured(repair_messages)
        repaired_message = _assistant_message(final_raw)
        repaired_content = repaired_message.get("content")
        try:
            return _parse_final_json(repaired_content), final_raw
        except (DeepSeekProtocolError, ValidationError) as exc:
            if attempt == 1:
                raise DeepSeekProtocolError(
                    "DeepSeek 最终回答在结构化修复后仍不符合协议"
                ) from exc
            if isinstance(repaired_content, str) and repaired_content.strip():
                repair_messages.append(
                    {"role": "assistant", "content": repaired_content}
                )
            repair_messages.append({"role": "user", "content": FORMAT_REPAIR_PROMPT})
    raise DeepSeekProtocolError("DeepSeek 最终回答结构化修复失败")


def _parse_final_json(content: Any) -> AgentDraft:
    if not isinstance(content, str) or not content.strip():
        raise DeepSeekProtocolError("DeepSeek 最终回答为空")
    text = content.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        text = "\n".join(lines[1:-1]).strip()
        if text.startswith("json"):
            text = text[4:].strip()
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise DeepSeekProtocolError("DeepSeek 最终回答不是有效 JSON") from exc
    if not isinstance(payload, dict):
        raise DeepSeekProtocolError("DeepSeek 最终回答必须是 JSON 对象")
    return AgentDraft.model_validate(payload)
