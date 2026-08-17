from __future__ import annotations

import logging

from fraud_graph.agent.schemas import AgentResponse
from fraud_graph.logging import opaque_log_id

LOGGER = logging.getLogger(__name__)


def log_agent_response(response: AgentResponse) -> None:
    LOGGER.info(
        "agent query completed",
        extra={
            "account_ref": opaque_log_id(response.account_id),
            "generation_mode": response.generation_mode,
            "provider_model": response.provider_model,
            "verifier_passed": response.verifier_passed,
            "citation_count": len(response.citations),
            "tool_count": len(response.tool_trace),
            "evidence_packet_sha256": response.evidence_packet_sha256,
        },
    )
