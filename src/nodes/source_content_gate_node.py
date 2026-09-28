"""Fetched-content S-2/S-3 security boundary."""

from __future__ import annotations

from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.security.credential_detector import detect_credentials
from shared.security.pii_detector import detect_pii
from src.services.content_safety import iter_string_leaves, quality_failure, redact_sensitive_text


class SourceContentGateNode(FunctionNode):
    """Exclude low-quality records and redact shared-detector findings."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        super().__init__()
        self._config = config or {}

    def _extra_security_gate_input(self, state: dict[str, Any]) -> dict[str, Any]:
        """S-2: inspect the field omitted by FunctionNode's default scan."""

        if state.get("status") == AgentStatus.ERROR.value:
            return state
        records = state.get("source_records")
        if not isinstance(records, list):
            return {
                **state,
                "status": AgentStatus.ERROR.value,
                "error_log": [*state.get("error_log", []), "E_ADAPTER_CONTRACT"],
            }
        accepted: list[dict[str, Any]] = []
        rejected = list(state.get("rejected", []))
        fail_run = self._config.get("quality_gate", {}).get("on_failure") == "fail_run"
        for record in records:
            if not isinstance(record, dict) or not isinstance(record.get("text"), str):
                return {
                    **state,
                    "status": AgentStatus.ERROR.value,
                    "error_log": [*state.get("error_log", []), "E_ADAPTER_CONTRACT"],
                }
            unsafe_metadata = any(
                detect_credentials(value) or detect_pii(value)
                for key, value in record.items()
                if key != "text" and isinstance(value, str)
            )
            if unsafe_metadata:
                rejected.append(
                    {
                        "stage": "source_content_gate",
                        "locator": "record_metadata",
                        "reason_code": "E_SENSITIVE_REDACTION",
                    }
                )
                if fail_run:
                    return {
                        **state,
                        "source_records": accepted,
                        "rejected": rejected,
                        "status": AgentStatus.ERROR.value,
                        "error_log": [*state.get("error_log", []), "E_SENSITIVE_REDACTION"],
                    }
                continue
            reason = quality_failure(record["text"], self._config.get("quality_gate", {}))
            if reason:
                rejected.append(
                    {"stage": "source_content_gate", "locator": record.get("locator", ""), "reason_code": reason}
                )
                if fail_run:
                    return {
                        **state,
                        "source_records": accepted,
                        "rejected": rejected,
                        "status": AgentStatus.ERROR.value,
                        "error_log": [*state.get("error_log", []), reason],
                    }
                continue
            redacted, safe = redact_sensitive_text(record["text"])
            if not safe:
                rejected.append(
                    {
                        "stage": "source_content_gate",
                        "locator": record.get("locator", ""),
                        "reason_code": "E_SENSITIVE_REDACTION",
                    }
                )
                if fail_run:
                    return {
                        **state,
                        "source_records": accepted,
                        "rejected": rejected,
                        "status": AgentStatus.ERROR.value,
                        "error_log": [*state.get("error_log", []), "E_SENSITIVE_REDACTION"],
                    }
                continue
            accepted.append({**record, "text": redacted})
        return {**state, "source_records": accepted, "rejected": rejected}

    def _extra_security_gate_output(self, result: dict[str, Any]) -> dict[str, Any]:
        """S-3: fail closed if a sensitive leaf survived the S-2 redaction."""

        records = result.get("source_records")
        if not isinstance(records, list):
            raise RuntimeError("S-3 source gate: source_records is missing or invalid")
        for record in records:
            if not isinstance(record, dict) or not isinstance(record.get("text"), str):
                raise RuntimeError("S-3 source gate: source record is missing or invalid")
            for _, text in iter_string_leaves(record):
                if detect_credentials(text) or detect_pii(text):
                    raise RuntimeError("S-3 source gate: sensitive source data survived redaction")
        return result

    def execute(self, state: dict[str, Any]) -> dict[str, Any]:
        from shared.utils.audit_logger import emit_trace_event

        if state.get("status") == AgentStatus.ERROR.value:
            emit_trace_event("source_content_gate_failed", {"status": "error"}, state)
            return {"status": AgentStatus.ERROR.value}
        records = list(state.get("source_records", []))
        rejected = list(state.get("rejected", []))
        emit_trace_event(
            "source_content_gate_completed",
            {"accepted_count": len(records), "rejected_count": len(rejected)},
            state,
        )
        return {
            "source_records": records,
            "rejected": rejected,
            "status": AgentStatus.SUCCESS.value,
        }
