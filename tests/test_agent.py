from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from urllib.error import HTTPError

from fastapi.testclient import TestClient

from fraud_graph.agent.fallback import build_fallback_response
from fraud_graph.agent.orchestrator import AgentService
from fraud_graph.agent.providers.deepseek import DeepSeekClient, DeepSeekConfig
from fraud_graph.agent.schemas import AgentQuery
from fraud_graph.agent.session import AgentSessionStore
from fraud_graph.agent.tools import EvidenceToolRegistry, ToolRejectedError
from fraud_graph.agent.verifier import EvidenceVerificationError, verify_agent_response
from fraud_graph.domain import EvidenceFact, EvidenceGrade, EvidencePacket, RiskScore
from fraud_graph.service.app import create_app


def packet() -> EvidencePacket:
    return EvidencePacket(
        account_id="root-1",
        generated_at=datetime(2026, 1, 1, tzinfo=UTC),
        score=RiskScore(
            account_id="root-1",
            risk_score=0.91,
            risk_rank=1,
            model_version="m1",
            data_version="d1",
            feature_as_of=datetime(2025, 1, 1, tzinfo=UTC),
            has_transactions=True,
        ),
        grade=EvidenceGrade.B,
        facts=[EvidenceFact(code="TXN_COUNT_TOTAL", description="transactions", value=3)],
        paths=[],
        limitations=["No qualifying multi-hop temporal path was found in the configured window."],
        recommended_actions=["Prioritize manual case review."],
    )


class State:
    def evidence(self, account_id: str) -> EvidencePacket:
        if account_id != "root-1":
            raise KeyError(account_id)
        return packet()

    def associations(self, account_id: str, limit: int = 20) -> list[object]:
        return []

    def agent_system_context(self) -> dict[str, object]:
        return system_context()


def system_context() -> dict[str, object]:
    return {
        "model_methodology": {
            "citation": {
                "kind": "system",
                "id": "MODEL_CARD",
                "label": "模型卡与特征清单",
            },
            "selected_model": "hybrid_profile_logistic",
            "feature_count": 73,
        },
        "risk_scoring_policy": {
            "citation": {
                "kind": "system",
                "id": "RISK_SCORING_POLICY",
                "label": "风险评分与上线边界",
            },
            "online_blockers": [
                {"code": "LABEL_TIME_MISSING", "name": "标签发生时间缺失"}
            ],
        },
        "model_evaluation": {
            "citation": {
                "kind": "system",
                "id": "MODEL_EVALUATION",
                "label": "模型评估结果",
            },
            "test_metrics": {
                "roc_auc": 0.9573,
                "pr_auc": 0.1753,
                "recall_at_top_fraction": 0.5,
                "positives": 12,
            },
        },
        "task4_requirements": {
            "citation": {
                "kind": "system",
                "id": "TASK4_REQUIREMENTS",
                "label": "任务四报告要求与验收",
            },
            "acceptance": {
                "report_count": 5,
                "consistency_rate": 1.0,
            },
        },
    }


def test_deterministic_fallback_is_cited_and_refuses_automatic_action() -> None:
    response = build_fallback_response(packet(), [], "是否可以直接冻结或止付？")
    assert response.generation_mode == "deterministic_fallback"
    assert response.verifier_passed is True
    assert any("不能执行或建议自动冻结" in item for item in response.key_findings)
    verify_agent_response(response, packet())


def test_tool_registry_rejects_cross_account_access() -> None:
    registry = EvidenceToolRegistry("root-1", packet(), [])
    try:
        registry.execute("get_risk_summary", {"account_id": "other"})
    except ToolRejectedError as exc:
        assert "跨账户" in str(exc)
    else:
        raise AssertionError("cross-account tool call was accepted")


def test_tool_registry_rejects_cross_domain_access() -> None:
    registry = EvidenceToolRegistry(
        "root-1",
        packet(),
        [],
        knowledge_domain="system_explanation",
        system_context=system_context(),
    )
    try:
        registry.execute("get_risk_summary")
    except ToolRejectedError as exc:
        assert "提问范围" in str(exc)
    else:
        raise AssertionError("cross-domain tool call was accepted")


def test_verifier_rejects_unknown_citation() -> None:
    response = build_fallback_response(packet(), [], "请解释风险")
    response.citations[0].id = "other-account-fact"
    try:
        verify_agent_response(response, packet())
    except EvidenceVerificationError as exc:
        assert "无效" in str(exc)
    else:
        raise AssertionError("unknown citation was accepted")


def test_deepseek_request_body_disables_thinking_by_default() -> None:
    client = DeepSeekClient(
        DeepSeekConfig(api_key="secret", external_enabled=True, model="deepseek-test")
    )
    body = client.build_request_body([], [])
    assert body["model"] == "deepseek-test"
    assert body["thinking"] == {"type": "disabled"}
    assert body["tool_choice"] == "auto"


def test_deepseek_structured_request_forces_json_without_tools() -> None:
    client = DeepSeekClient(
        DeepSeekConfig(api_key="secret", external_enabled=True, model="deepseek-test")
    )
    body = client.build_structured_request_body(
        [{"role": "user", "content": "Return JSON"}]
    )
    assert body["response_format"] == {"type": "json_object"}
    assert body["thinking"] == {"type": "disabled"}
    assert body["temperature"] == 0
    assert "tools" not in body
    assert "tool_choice" not in body


def test_agent_api_works_without_api_key() -> None:
    client = TestClient(create_app(State()))  # type: ignore[arg-type]
    assert "研判助手" in client.get("/").text
    capabilities = client.get("/v1/agent/capabilities")
    assert capabilities.status_code == 200
    assert capabilities.json()["external_model_ready"] is False
    response = client.post(
        "/v1/agent/query",
        json={"account_id": "root-1", "question": "为什么值得优先复核？"},
    )
    assert response.status_code == 200
    assert response.json()["generation_mode"] == "deterministic_fallback"
    assert response.json()["verifier_passed"] is True


def test_agent_api_system_domain_returns_version_grounded_fallback() -> None:
    client = TestClient(create_app(State()))  # type: ignore[arg-type]
    response = client.post(
        "/v1/agent/query",
        json={
            "account_id": "root-1",
            "question": "什么是上线阻断项？",
            "knowledge_domain": "system_explanation",
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["knowledge_domain"] == "system_explanation"
    assert "阻止其直接驱动生产处置" in payload["answer"]
    assert payload["citations"] == [
        {
            "kind": "system",
            "id": "RISK_SCORING_POLICY",
            "label": "风险评分与上线边界",
        }
    ]

    standard = client.post(
        "/v1/agent/query",
        json={
            "account_id": "root-1",
            "question": "我们判断风险账户的标准是什么？",
            "knowledge_domain": "system_explanation",
        },
    ).json()
    assert "固定分数线" in standard["answer"]
    assert "人工复核综合判断" in standard["answer"]


def test_deepseek_system_question_only_exposes_system_tools() -> None:
    requests: list[dict[str, object]] = []

    def transport(
        url: str,
        headers: dict[str, str],
        payload: bytes,
        timeout: float,
    ) -> dict[str, object]:
        request = json.loads(payload)
        requests.append(request)
        if len(requests) == 1:
            tool_names = {
                item["function"]["name"] for item in request.get("tools", [])
            }
            assert "get_risk_scoring_policy" in tool_names
            assert "get_risk_summary" not in tool_names
            return {
                "model": "deepseek-test",
                "choices": [{
                    "message": {
                        "role": "assistant",
                        "tool_calls": [{
                            "id": "call-system",
                            "type": "function",
                            "function": {
                                "name": "get_risk_scoring_policy",
                                "arguments": "{}",
                            },
                        }],
                    }
                }],
            }
        return {
            "model": "deepseek-test",
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": (
                        '{"answer":"风险分数用于复核排序，不等于涉诈认定 '
                        '[系统:RISK_SCORING_POLICY]","assessment":"information_only",'
                        '"key_findings":["Top 5% 是复核容量 '
                        '[系统:RISK_SCORING_POLICY]"],"citations":[{"kind":"system",'
                        '"id":"RISK_SCORING_POLICY","label":"风险评分与上线边界"}],'
                        '"limitations":[],"recommended_actions":[]}'
                    ),
                }
            }],
        }

    config = DeepSeekConfig(api_key="secret", external_enabled=True)
    service = AgentService(
        State(), config=config, client=DeepSeekClient(config, transport=transport)
    )
    response = asyncio.run(
        service.query(
            AgentQuery(
                account_id="root-1",
                question="我们判断风险账户的标准是什么？",
                knowledge_domain="system_explanation",
            )
        )
    )
    assert response.generation_mode == "deepseek"
    assert response.knowledge_domain == "system_explanation"
    assert response.citations[0].kind == "system"
    assert response.tool_trace[0].tool == "get_risk_scoring_policy"


def test_invalid_system_citation_forces_system_fallback() -> None:
    def transport(
        url: str,
        headers: dict[str, str],
        payload: bytes,
        timeout: float,
    ) -> dict[str, object]:
        return {
            "model": "deepseek-test",
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": (
                        '{"answer":"未知规则 [系统:OTHER_POLICY]",'
                        '"assessment":"information_only","key_findings":'
                        '["未知规则 [系统:OTHER_POLICY]"],"citations":'
                        '[{"kind":"system","id":"OTHER_POLICY","label":"未知规则"}]}'
                    ),
                }
            }],
        }

    config = DeepSeekConfig(api_key="secret", external_enabled=True)
    service = AgentService(
        State(), config=config, client=DeepSeekClient(config, transport=transport)
    )
    response = asyncio.run(
        service.query(
            AgentQuery(
                account_id="root-1",
                question="什么是上线阻断项？",
                knowledge_domain="system_explanation",
            )
        )
    )
    assert response.generation_mode == "deterministic_fallback"
    assert response.knowledge_domain == "system_explanation"
    assert "无效或跨账户" in (response.fallback_reason or "")
    assert response.citations[0].id == "RISK_SCORING_POLICY"


def test_deepseek_tool_call_loop_uses_injected_transport() -> None:
    calls: list[dict[str, object]] = []

    def transport(
        url: str,
        headers: dict[str, str],
        payload: bytes,
        timeout: float,
    ) -> dict[str, object]:
        calls.append({"url": url, "payload": payload})
        if len(calls) == 1:
            return {
                "model": "deepseek-test",
                "choices": [{
                    "message": {
                        "role": "assistant",
                        "tool_calls": [{
                            "id": "call-1",
                            "type": "function",
                            "function": {"name": "get_risk_summary", "arguments": "{}"},
                        }],
                    }
                }],
            }
        return {
            "model": "deepseek-test",
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": (
                        '{"answer":"风险需要人工复核 [事实:MODEL_RISK_SCORE]",'
                        '"assessment":"manual_review_recommended","citations":'
                        '[{"kind":"fact","id":"MODEL_RISK_SCORE",'
                        '"label":"主模型风险分数"}],"key_findings":'
                        '["风险分数较高 [事实:MODEL_RISK_SCORE]"],"limitations":[],'
                        '"recommended_actions":[]}'
                    ),
                }
            }],
        }

    service = AgentService(
        State(),
        config=DeepSeekConfig(api_key="secret", external_enabled=True),
        client=DeepSeekClient(
            DeepSeekConfig(api_key="secret", external_enabled=True), transport=transport
        ),
    )
    response = asyncio.run(service.query(AgentQuery(account_id="root-1", question="请解释风险")))
    assert response.generation_mode == "deepseek"
    assert response.verifier_passed is True
    assert response.citation_repair_applied is False
    assert len(response.tool_trace) == 1
    assert len(calls) == 2


def test_invalid_final_text_is_repaired_with_structured_output() -> None:
    requests: list[bytes] = []

    def transport(
        url: str,
        headers: dict[str, str],
        payload: bytes,
        timeout: float,
    ) -> dict[str, object]:
        requests.append(payload)
        if len(requests) == 1:
            return {
                "model": "deepseek-test",
                "choices": [{
                    "message": {
                        "role": "assistant",
                        "content": "该账户风险较高，但这不是 JSON。",
                    }
                }],
            }
        return {
            "model": "deepseek-test",
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": (
                        '{"answer":"建议人工复核 [事实:MODEL_RISK_SCORE]",'
                        '"assessment":"manual_review_recommended","citations":'
                        '[{"kind":"fact","id":"MODEL_RISK_SCORE",'
                        '"label":"主模型风险分数"}],"key_findings":'
                        '["风险分数支持优先复核 [事实:MODEL_RISK_SCORE]"],"limitations":[],'
                        '"recommended_actions":["人工核查账户资料"]}'
                    ),
                }
            }],
        }

    config = DeepSeekConfig(api_key="secret", external_enabled=True)
    service = AgentService(
        State(), config=config, client=DeepSeekClient(config, transport=transport)
    )
    response = asyncio.run(
        service.query(AgentQuery(account_id="root-1", question="请解释风险"))
    )

    assert response.generation_mode == "deepseek"
    assert response.verifier_passed is True
    assert len(requests) == 2
    assert b'"response_format": {"type": "json_object"}' in requests[1]


def test_missing_valid_citations_are_reconciled_without_fallback() -> None:
    def transport(
        url: str,
        headers: dict[str, str],
        payload: bytes,
        timeout: float,
    ) -> dict[str, object]:
        return {
            "model": "deepseek-test",
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": (
                        '{"answer":"建议人工复核 [事实:MODEL_RISK_SCORE]",'
                        '"assessment":"manual_review_recommended","key_findings":'
                        '["观察窗口记录 3 笔交易 [事实:TXN_COUNT_TOTAL]"],'
                        '"citations":[{"kind":"fact","id":"MODEL_RISK_SCORE",'
                        '"label":"主模型风险分数"}],"limitations":[],'
                        '"recommended_actions":["人工核查源交易"]}'
                    ),
                }
            }],
        }

    config = DeepSeekConfig(api_key="secret", external_enabled=True)
    service = AgentService(
        State(), config=config, client=DeepSeekClient(config, transport=transport)
    )
    response = asyncio.run(
        service.query(AgentQuery(account_id="root-1", question="请解释风险"))
    )

    assert response.generation_mode == "deepseek"
    assert response.verifier_passed is True
    assert response.citation_repair_applied is True
    assert [item.id for item in response.citations] == [
        "MODEL_RISK_SCORE",
        "TXN_COUNT_TOTAL",
    ]


def test_structured_output_retries_once_before_fallback() -> None:
    requests: list[bytes] = []

    def transport(
        url: str,
        headers: dict[str, str],
        payload: bytes,
        timeout: float,
    ) -> dict[str, object]:
        requests.append(payload)
        return {
            "model": "deepseek-test",
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": "仍然不是 JSON",
                }
            }],
        }

    config = DeepSeekConfig(api_key="secret", external_enabled=True)
    service = AgentService(
        State(), config=config, client=DeepSeekClient(config, transport=transport)
    )
    response = asyncio.run(
        service.query(AgentQuery(account_id="root-1", question="请解释风险"))
    )

    assert response.generation_mode == "deterministic_fallback"
    assert response.verifier_passed is True
    assert response.fallback_reason == "DeepSeek 最终回答在结构化修复后仍不符合协议"
    assert len(requests) == 3


def test_invalid_tool_name_forces_deterministic_fallback() -> None:
    def transport(
        url: str,
        headers: dict[str, str],
        payload: bytes,
        timeout: float,
    ) -> dict[str, object]:
        return {
            "choices": [{
                "message": {
                    "role": "assistant",
                    "tool_calls": [{
                        "id": "call-bad",
                        "type": "function",
                        "function": {"name": "run_sql", "arguments": "{}"},
                    }],
                }
            }],
        }

    config = DeepSeekConfig(api_key="secret", external_enabled=True)
    service = AgentService(
        State(), config=config, client=DeepSeekClient(config, transport=transport)
    )
    response = asyncio.run(service.query(AgentQuery(account_id="root-1", question="查询更多数据")))
    assert response.generation_mode == "deterministic_fallback"
    assert "未授权的工具" in (response.fallback_reason or "")


def test_invalid_deepseek_citation_forces_deterministic_fallback() -> None:
    def transport(
        url: str,
        headers: dict[str, str],
        payload: bytes,
        timeout: float,
    ) -> dict[str, object]:
        return {
            "model": "deepseek-test",
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": (
                        '{"answer":"发现风险 [路径:other-path]",'
                        '"assessment":"manual_review_recommended","key_findings":'
                        '["存在未知路径 [路径:other-path]"],"citations":'
                        '[{"kind":"path","id":"other-path","label":"未知路径"}]}'
                    ),
                }
            }],
        }

    config = DeepSeekConfig(api_key="secret", external_enabled=True)
    service = AgentService(
        State(), config=config, client=DeepSeekClient(config, transport=transport)
    )
    response = asyncio.run(service.query(AgentQuery(account_id="root-1", question="解释风险")))
    assert response.generation_mode == "deterministic_fallback"
    assert "无效或跨账户" in (response.fallback_reason or "")


def test_deepseek_retries_retryable_http_error() -> None:
    attempts = 0

    def transport(
        url: str,
        headers: dict[str, str],
        payload: bytes,
        timeout: float,
    ) -> dict[str, object]:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise HTTPError(url, 503, "unavailable", {}, None)
        return {"choices": [{"message": {"role": "assistant", "content": "{}"}}]}

    config = DeepSeekConfig(api_key="secret", external_enabled=True, max_retries=1)
    client = DeepSeekClient(config, transport=transport)
    result = asyncio.run(client.chat([], []))
    assert attempts == 2
    assert "choices" in result


def test_temporary_key_session_enables_deepseek_without_exposing_key() -> None:
    requests: list[bytes] = []

    def transport(
        url: str,
        headers: dict[str, str],
        payload: bytes,
        timeout: float,
    ) -> dict[str, object]:
        requests.append(payload)
        if b"credential validation request" in payload:
            return {"choices": [{"message": {"role": "assistant", "content": "OK"}}]}
        return {
            "model": "deepseek-v4-flash",
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": (
                        '{"answer":"建议人工复核 [事实:MODEL_RISK_SCORE]",'
                        '"assessment":"manual_review_recommended","citations":'
                        '[{"kind":"fact","id":"MODEL_RISK_SCORE",'
                        '"label":"主模型风险分数"}],"key_findings":'
                        '["风险分数支持优先复核 [事实:MODEL_RISK_SCORE]"],"limitations":[],'
                        '"recommended_actions":[]}'
                    ),
                }
            }],
        }

    base_config = DeepSeekConfig(api_key=None, external_enabled=False)
    store = AgentSessionStore(
        base_config,
        client_factory=lambda config: DeepSeekClient(config, transport=transport),
    )
    state = State()
    client = TestClient(
        create_app(
            state,  # type: ignore[arg-type]
            agent_service=AgentService(state, config=base_config),
            agent_session_store=store,
        )
    )
    secret = "sk-temporary-test-key"
    no_consent = client.post(
        "/v1/agent/session",
        json={
            "api_key": secret,
            "model": "deepseek-v4-flash",
            "data_export_authorized": False,
        },
    )
    assert no_consent.status_code == 400
    assert not requests

    enabled = client.post(
        "/v1/agent/session",
        json={
            "api_key": secret,
            "model": "deepseek-v4-flash",
            "data_export_authorized": True,
        },
    )
    assert enabled.status_code == 200
    assert enabled.json()["active"] is True
    assert secret not in enabled.text
    assert "HttpOnly" in enabled.headers["set-cookie"]
    assert "SameSite=strict" in enabled.headers["set-cookie"]
    assert client.get("/v1/agent/session").json()["active"] is True

    answer = client.post(
        "/v1/agent/query",
        json={"account_id": "root-1", "question": "请解释风险"},
    )
    assert answer.status_code == 200
    assert answer.json()["generation_mode"] == "deepseek"
    assert secret not in answer.text

    disabled = client.delete("/v1/agent/session")
    assert disabled.status_code == 200
    assert disabled.json()["active"] is False
    fallback = client.post(
        "/v1/agent/query",
        json={"account_id": "root-1", "question": "请解释风险"},
    )
    assert fallback.json()["generation_mode"] == "deterministic_fallback"


def test_key_validation_failure_returns_generic_error() -> None:
    def rejected_transport(
        url: str,
        headers: dict[str, str],
        payload: bytes,
        timeout: float,
    ) -> dict[str, object]:
        raise HTTPError(url, 401, "invalid key", {}, None)

    base_config = DeepSeekConfig(api_key=None, external_enabled=False, max_retries=0)
    store = AgentSessionStore(
        base_config,
        client_factory=lambda config: DeepSeekClient(
            config,
            transport=rejected_transport,
        ),
    )
    state = State()
    client = TestClient(
        create_app(
            state,  # type: ignore[arg-type]
            agent_service=AgentService(state, config=base_config),
            agent_session_store=store,
        )
    )
    secret = "sk-invalid-test-key"
    response = client.post(
        "/v1/agent/session",
        json={
            "api_key": secret,
            "model": "deepseek-v4-flash",
            "data_export_authorized": True,
        },
    )
    assert response.status_code == 502
    assert response.json()["detail"].startswith("DeepSeek API Key 验证失败")
    assert secret not in response.text
