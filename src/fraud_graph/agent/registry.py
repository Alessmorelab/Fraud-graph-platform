from __future__ import annotations

from typing import Any

ACCOUNT_TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "get_risk_summary",
            "description": "读取当前账户的风险评分、排序、证据等级和模型边界。",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_structured_evidence",
            "description": "读取当前账户证据包中的结构化事实。",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_suspicious_paths",
            "description": "读取当前账户经过时序和金额连续性约束的可疑资金路径。",
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "minimum": 1, "maximum": 20, "default": 5}
                },
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_associated_accounts",
            "description": "读取当前账户的关联账户及其关联证据。",
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "minimum": 1, "maximum": 20, "default": 10}
                },
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_flow_patterns",
            "description": "读取当前账户的资金汇聚、分散、快速中转或双向互转模式。",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_report_boundary",
            "description": "读取模型和报告的局限性、上线阻断项及允许的人工处置边界。",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
]

SYSTEM_TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "get_model_methodology",
            "description": "读取最终模型、特征分组、类别不平衡处理和训练方式。",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_risk_scoring_policy",
            "description": "解释风险账户判断标准、风险分数、排序、Top5%和上线阻断项。",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_model_evaluation",
            "description": "读取测试集指标、评估边界和当前模型局限。",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_task4_requirements",
            "description": "读取任务四要求、报告交付和当前技术验收结果。",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
]

TOOL_DEFINITIONS = [*ACCOUNT_TOOL_DEFINITIONS, *SYSTEM_TOOL_DEFINITIONS]
ACCOUNT_TOOL_NAMES = [item["function"]["name"] for item in ACCOUNT_TOOL_DEFINITIONS]
SYSTEM_TOOL_NAMES = [item["function"]["name"] for item in SYSTEM_TOOL_DEFINITIONS]
TOOL_NAMES = [*ACCOUNT_TOOL_NAMES, *SYSTEM_TOOL_NAMES]


def tool_definitions_for(domain: str) -> list[dict[str, Any]]:
    if domain == "account_investigation":
        return ACCOUNT_TOOL_DEFINITIONS
    if domain == "system_explanation":
        return SYSTEM_TOOL_DEFINITIONS
    return TOOL_DEFINITIONS
