from __future__ import annotations

from typing import Any

FACT_PRESENTATION = {
    "MODEL_RISK_SCORE": ("主模型风险分数", "任务二最终模型输出的账户风险概率"),
    "TXN_COUNT_TOTAL": ("观察窗口交易笔数", "观察窗口内发生的交易总笔数"),
    "COUNTERPARTY_COUNT_TOTAL": ("交易对手数量", "观察窗口内有资金往来的不同账户数"),
    "SAME_DAY_IN_OUT_DAYS": (
        "同日收付活跃天数",
        "同一天内同时发生资金流入和流出的天数",
    ),
    "SAME_DAY_TURNOVER_RATIO_AVG": (
        "同日资金周转比例",
        "同日流入资金被流出资金覆盖的平均比例",
    ),
    "CITED_DIRECT_TRANSACTIONS": ("直接交易关系", "由源交易记录支持的一跳账户关系"),
    "SUPPORTED_MULTI_HOP_PATHS": (
        "多跳时序资金链",
        "由源交易支持且时间顺序连续的多跳路径",
    ),
    "FLOW_PATTERNS": (
        "异常资金流向模式",
        "识别出的资金汇聚、分散、快速中转或双向互转模式",
    ),
}

BLOCKER_LABELS = {
    "LABEL_TIME_MISSING": "标签发生时间缺失",
    "RETROSPECTIVE_STATIC_TARGET": "采用静态回溯目标",
    "TRANSDUCTIVE_GRAPH_EVALUATION": "图特征评估包含传导信息",
    "MODEL_APPROVAL_REQUIRED": "模型尚待审批",
    "ACTIVE_POSITIVES_TOO_FEW": "有交易正样本过少",
    "PROFILE_SHORTCUT_DEPENDENCY": "存在画像捷径依赖",
    "NON_TEMPORAL_DIAGNOSTIC_EVALUATION": "当前为非时序诊断评估",
    "REFERENCE_MODEL_NOT_APPROVED": "参考模型尚待审批",
    "NOT_APPROVED": "模型尚未审批",
}

PATTERN_LABELS = {
    "FAN_IN": "资金汇聚",
    "FAN_OUT": "资金分散",
    "PASS_THROUGH": "快速中转",
    "RECIPROCAL": "双向互转",
}

PATH_EVIDENCE_LABELS = {
    "DIRECT_TRANSACTION": "源交易支持的直接关系",
    "TEMPORAL_CHAIN": "时间顺序连续",
    "RAPID_SUCCESSION": "短时连续转账",
    "AMOUNT_CONTINUITY": "金额具有连续性",
}

LIMITATION_TRANSLATIONS = {
    "Only direct transaction relationships were found; no qualifying multi-hop temporal "
    "path was established.": "仅发现直接交易关系，尚未形成满足约束的多跳时序资金链。",
    "No qualifying multi-hop temporal path was found in the configured window.": (
        "当前观察窗口内未发现满足约束的多跳时序资金链。"
    ),
    "No transaction was observed in the configured window; no funding path can be asserted.": (
        "当前观察窗口内未发现交易，因此不能认定存在资金链路。"
    ),
    "Model output is decision support and does not authorize automatic freezing.": (
        "模型结果仅用于辅助研判，不构成自动止付或冻结的依据。"
    ),
    "The referenced model is not production-eligible and must not drive an operational "
    "decision.": "当前模型尚不具备生产决策资格，不得直接驱动业务处置。",
    "No transaction evidence.": "当前没有可核验的交易证据。",
    "No transactions.": "当前观察窗口内未发现交易。",
}

ACTION_TRANSLATIONS = {
    "Prioritize manual case review.": "优先进入人工案例复核队列。",
    "Verify the cited transactions and related accounts against authorized internal data.": (
        "结合已授权的内部数据，核验报告引用的交易和关联账户。"
    ),
    "Escalate through the bank's existing approval workflow when corroborated.": (
        "取得其他证据印证后，按银行现有审批流程升级处置。"
    ),
    "Review behavioral anomalies and expand the observation window.": (
        "复核行为异常，并视需要扩大交易观察窗口。"
    ),
    "Check additional authorized channels before escalation.": (
        "升级处置前，通过其他已授权渠道补充核验。"
    ),
    "Treat the score as unsupported by observed transaction evidence.": (
        "当前风险分数缺少已观察交易证据支持，应谨慎使用。"
    ),
    "Obtain additional transaction or channel evidence before case escalation.": (
        "升级为案件前，应补充交易或其他渠道证据。"
    ),
    "Human review only.": "仅供人工复核。",
    "Review.": "建议进行人工复核。",
}


def fact_label(code: str) -> str:
    return FACT_PRESENTATION.get(code, (code, ""))[0]


def fact_description(code: str, fallback: str) -> str:
    return FACT_PRESENTATION.get(code, (code, fallback))[1] or fallback


def blocker_label(code: str) -> str:
    return BLOCKER_LABELS.get(code, code)


def pattern_label(pattern_type: str) -> str:
    return PATTERN_LABELS.get(pattern_type, pattern_type)


def path_evidence_label(code: str) -> str:
    return PATH_EVIDENCE_LABELS.get(code, code)


def training_mode_label(mode: str) -> str:
    return {
        "temporal": "时序建模",
        "retrospective_static": "静态回溯建模",
        "unknown": "未知",
    }.get(mode, mode)


def feature_set_label(feature_set: str) -> str:
    return {
        "behavior": "行为特征",
        "profile": "账户画像",
        "combined": "行为、图结构、快照、异常分数与账户画像融合",
        "unknown": "未知",
    }.get(feature_set, feature_set)


def localize_limitation(value: str) -> str:
    if value.startswith("Model eligibility blockers:"):
        return "模型仍存在上述上线阻断项，当前不得用于生产决策。"
    return LIMITATION_TRANSLATIONS.get(value, value)


def localize_action(value: str) -> str:
    return ACTION_TRANSLATIONS.get(value, value)


def format_fact_value(code: str, value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, int):
        return f"{value:,}"
    if isinstance(value, float):
        if value.is_integer():
            return f"{int(value):,}"
        return f"{value:.6f}" if code == "MODEL_RISK_SCORE" else f"{value:.3f}"
    return str(value)
