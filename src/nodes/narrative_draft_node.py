"""Single-call LLM narrative drafting node."""

from __future__ import annotations

import json
from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.security.credential_detector import detect_credentials
from shared.security.pii_detector import detect_pii
from shared.services.llm.base_llm import BaseLLM
from src.services.content_safety import iter_string_leaves

_DRAFT_SCHEMA = {
    "summary": {"text": "str", "evidence": [{"source_id": "str", "quoted_span": "str"}]},
    "risks": [{"description": "str", "evidence": [{"source_id": "str", "quoted_span": "str"}]}],
    "blockers": [{"description": "str", "evidence": [{"source_id": "str", "quoted_span": "str"}]}],
    "next_steps": [{"text": "str", "evidence": [{"source_id": "str", "quoted_span": "str"}]}],
}


class NarrativeDraftNode(FunctionNode):
    """Draft prose against redacted records and pre-computed metrics."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, llm: BaseLLM | None = None) -> None:
        super().__init__()
        self._llm = llm

    def _extra_security_gate_output(self, result: dict[str, Any]) -> dict[str, Any]:
        narrative = result.get("narrative")
        if not isinstance(narrative, dict):
            raise RuntimeError("S-3 narrative gate: narrative is missing or invalid")
        for _, text in iter_string_leaves(narrative):
            if detect_credentials(text) or detect_pii(text):
                raise RuntimeError("S-3 narrative gate: sensitive prose detected")
        return result

    def execute(self, state: dict[str, Any]) -> dict[str, Any]:
        from shared.utils.audit_logger import emit_trace_event

        if state.get("status") == AgentStatus.ERROR.value:
            emit_trace_event("narrative_draft_passthrough", {"status": "error"}, state)
            return {"status": AgentStatus.ERROR.value}
        if self._llm is None:
            emit_trace_event("narrative_draft_failed", {"reason_code": "E_LLM_UNAVAILABLE"}, state)
            return {"status": AgentStatus.ERROR.value, "error_log": ["E_LLM_UNAVAILABLE"]}
        messages = [
            {
                "role": "system",
                "content": (
                    "Draft a project status narrative. Use only supplied records and metrics. "
                    "Every prose element must include its own evidence. Return JSON only with exactly this schema: "
                    + json.dumps(_DRAFT_SCHEMA, ensure_ascii=False, sort_keys=True)
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "metrics": state.get("metrics", {}),
                        "source_records": [
                            {"source_id": record["source_id"], "text": record["text"]}
                            for record in state.get("source_records", [])
                        ],
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
            },
        ]
        try:
            response = self._llm.complete(messages)
            content = response.get("content") if isinstance(response, dict) else None
            narrative = json.loads(content) if isinstance(content, str) else None
            self._validate_contract(narrative)
        except Exception:
            emit_trace_event("narrative_draft_failed", {"reason_code": "E_LLM_CONTRACT"}, state)
            return {"status": AgentStatus.ERROR.value, "error_log": ["E_LLM_CONTRACT"]}
        emit_trace_event("narrative_drafted", {"element_groups": len(narrative)}, state)
        return {"narrative": narrative, "status": AgentStatus.SUCCESS.value}

    @classmethod
    def _validate_contract(cls, narrative: object) -> None:
        if not isinstance(narrative, dict) or set(narrative) != {"summary", "risks", "blockers", "next_steps"}:
            raise ValueError("E_LLM_CONTRACT")
        cls._validate_element(narrative["summary"], "text")
        for group, text_key in (("risks", "description"), ("blockers", "description"), ("next_steps", "text")):
            if not isinstance(narrative[group], list):
                raise ValueError("E_LLM_CONTRACT")
            for item in narrative[group]:
                cls._validate_element(item, text_key)

    @staticmethod
    def _validate_element(element: object, text_key: str) -> None:
        if not isinstance(element, dict) or set(element) != {text_key, "evidence"}:
            raise ValueError("E_LLM_CONTRACT")
        if (
            not isinstance(element[text_key], str)
            or not isinstance(element["evidence"], list)
            or not element["evidence"]
        ):
            raise ValueError("E_LLM_CONTRACT")
        for evidence in element["evidence"]:
            if (
                not isinstance(evidence, dict)
                or set(evidence) != {"source_id", "quoted_span"}
                or not isinstance(evidence["source_id"], str)
                or not isinstance(evidence["quoted_span"], str)
            ):
                raise ValueError("E_LLM_CONTRACT")
