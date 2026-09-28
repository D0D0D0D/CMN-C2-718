"""Request-envelope validation node."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from src.services.content_safety import quality_failure

_ENVELOPE_KEYS = {
    "project_id",
    "period_start",
    "period_end",
    "source_set_id",
    "output_schema_id",
}
_OFFSET_RE = re.compile(r"(?:Z|[+-]\d{2}:\d{2})$")


class EnvelopeError(ValueError):
    """Request-envelope validation failure carrying a public error code."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class InputValidateNode(FunctionNode):
    """Validate the request and resolve its period to absolute UTC bounds."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        super().__init__()
        self._config = config or {}

    def _extra_security_gate_input(self, state: dict[str, Any]) -> dict[str, Any]:
        """S-2: independently reject malformed or low-quality envelopes."""

        if state.get("status") == AgentStatus.ERROR.value:
            return state
        try:
            self._parse_and_validate(state.get("user_input", ""))
        except EnvelopeError as exc:
            return {
                **state,
                "status": AgentStatus.ERROR.value,
                "rejected": [{"stage": "pre_process", "locator": "envelope", "reason_code": exc.code}],
                "error_log": [*state.get("error_log", []), exc.code],
            }
        return state

    def execute(self, state: dict[str, Any]) -> dict[str, Any]:
        from shared.utils.audit_logger import emit_trace_event

        try:
            envelope, start_utc, end_utc = self._parse_and_validate(state.get("user_input", ""))
            ctx = InvocationContext.from_state(state)
            allowed_projects = self._config.get("allowed_projects", {})
            permitted = allowed_projects.get(ctx.caller_id, []) if isinstance(allowed_projects, dict) else []
            if envelope["project_id"] not in permitted:
                raise EnvelopeError("E_PROJECT_FORBIDDEN")
        except EnvelopeError as exc:
            emit_trace_event("request_envelope_rejected", {"reason_code": exc.code}, state)
            return {
                "status": AgentStatus.ERROR.value,
                "rejected": [{"stage": "pre_process", "locator": "envelope", "reason_code": exc.code}],
                "error_log": [exc.code],
            }

        validated = {
            "project_id": envelope["project_id"],
            "period_start_utc": start_utc,
            "period_end_utc": end_utc,
            "source_set_id": envelope["source_set_id"],
            "output_schema_id": envelope["output_schema_id"],
        }
        emit_trace_event(
            "request_envelope_validated",
            {"source_set_id": envelope["source_set_id"], "output_schema_id": envelope["output_schema_id"]},
            state,
        )
        return {
            "validated_input": validated,
            "rejected": [],
            "status": AgentStatus.SUCCESS.value,
        }

    def _parse_and_validate(self, raw: object) -> tuple[dict[str, Any], str, str]:
        if not isinstance(raw, str):
            raise EnvelopeError("E_ENVELOPE_TYPE")
        try:
            envelope = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            raise EnvelopeError("E_ENVELOPE_JSON") from None
        if not isinstance(envelope, dict):
            raise EnvelopeError("E_ENVELOPE_TYPE")
        if set(envelope) - _ENVELOPE_KEYS:
            raise EnvelopeError("E_UNKNOWN_KEY")
        if set(envelope) != _ENVELOPE_KEYS:
            missing = _ENVELOPE_KEYS - set(envelope)
            if "source_set_id" in missing:
                raise EnvelopeError("E_SOURCE_SET_UNKNOWN")
            if "output_schema_id" in missing:
                raise EnvelopeError("E_SCHEMA_UNKNOWN")
            raise EnvelopeError("E_ENVELOPE_TYPE")
        if any(not isinstance(envelope[key], str) for key in _ENVELOPE_KEYS):
            raise EnvelopeError("E_ENVELOPE_TYPE")
        project_id = envelope["project_id"]
        if not project_id or len(project_id) > 200:
            raise EnvelopeError("E_PROJECT_ID_INVALID")
        source_sets = self._config.get("source_sets", {})
        if envelope["source_set_id"] not in source_sets:
            raise EnvelopeError("E_SOURCE_SET_UNKNOWN")
        output_schemas = self._config.get("output_schemas", {})
        if envelope["output_schema_id"] not in output_schemas:
            raise EnvelopeError("E_SCHEMA_UNKNOWN")
        for value in envelope.values():
            reason = quality_failure(value, self._config.get("quality_gate", {}))
            if reason:
                raise EnvelopeError(reason)
        start = self._parse_bound(envelope["period_start"])
        end = self._parse_bound(envelope["period_end"])
        if end <= start:
            raise EnvelopeError("E_PERIOD_RANGE")
        return envelope, self._to_utc(start), self._to_utc(end)

    @staticmethod
    def _parse_bound(value: str) -> datetime:
        if not _OFFSET_RE.search(value):
            raise EnvelopeError("E_PERIOD_FORMAT")
        candidate = value[:-1] + "+00:00" if value.endswith("Z") else value
        try:
            parsed = datetime.fromisoformat(candidate)
        except ValueError:
            raise EnvelopeError("E_PERIOD_FORMAT") from None
        if parsed.tzinfo is None:
            raise EnvelopeError("E_PERIOD_FORMAT")
        return parsed

    @staticmethod
    def _to_utc(value: datetime) -> str:
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
