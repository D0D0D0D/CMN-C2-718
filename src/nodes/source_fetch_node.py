"""Canonical source-record fetch node."""

from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.invocation_context import InvocationContext
from src.services.content_safety import redact_string_leaves
from framework.schemas.trust_level import TrustLevel
from src.services.service import AdapterRegistry, Service, ServiceIdentity

_RECORD_KEYS = {"source_id", "locator", "fetched_at", "text", "events", "open_at_period_start"}
_EVENT_KEYS = {"event_ts", "kind", "estimate", "milestone_id"}


class SensitiveResidueError(RuntimeError):
    """Redaction ran and the result still matches a detector — fail closed."""


class SourceFetchNode(FunctionNode):
    """Fetch through the configured read-only adapter and enforce its contract."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, config: dict[str, Any] | None = None, service: Service | None = None) -> None:
        super().__init__()
        self._config = config or {}
        self._service = (
            service
            or self._config.get("service")
            or Service(ServiceIdentity("CMN-C2-718", "ProjectProgressReportAgent", "cmn"), AdapterRegistry())
        )

    def execute(self, state: dict[str, Any]) -> dict[str, Any]:
        from shared.utils.audit_logger import emit_trace_event

        if state.get("status") == AgentStatus.ERROR.value:
            emit_trace_event("source_fetch_passthrough", {"status": "error"}, state)
            return {"status": AgentStatus.ERROR.value}
        try:
            validated = json.loads(state.get("user_input", ""))
            if not isinstance(validated, dict):
                raise ValueError("E_ADAPTER_CONTRACT: validated input is not an object")
            source_set_id = validated["source_set_id"]
            source_config = self._config["source_sets"][source_set_id]
            ctx = InvocationContext.from_state(state)
            secret_name = source_config.get("secret_name")
            if not isinstance(secret_name, str) or not secret_name:
                raise ValueError("E_ADAPTER_CONTRACT: source secret_name is invalid")
            try:
                credential = ctx.secrets.require(secret_name)
            except (KeyError, ValueError):
                emit_trace_event(
                    "source_fetch_rejected",
                    {"reason_code": "E_SOURCE_CREDENTIAL_UNAVAILABLE", "source_set_id": source_set_id},
                    state,
                )
                return {
                    "source_records": [],
                    "rejected": [],
                    "status": AgentStatus.ERROR.value,
                    "error_log": ["E_SOURCE_CREDENTIAL_UNAVAILABLE"],
                }
            adapter = self._service.adapters.resolve(source_set_id, source_config)
            raw_records = adapter.fetch(
                project_id=validated["project_id"],
                period_start_utc=validated["period_start_utc"],
                period_end_utc=validated["period_end_utc"],
                base_url=source_config.get("base_url", ""),
                credential=credential,
            )
            records, redacted_count = self._canonicalise(raw_records)
            # Redaction is owned by the node that first places the text into its return
            # dict, not by a downstream node: the S-3 gate scans this node's own result
            # (agentcore 1.0.1 recurses into dict/list via detect_credentials_in_value),
            # so a record still carrying a credential is blocked here and never reaches a
            # later gate.
        except SensitiveResidueError:
            emit_trace_event("source_fetch_rejected", {"reason_code": "E_SOURCE_REDACTION_INCOMPLETE"}, state)
            return {
                "source_records": [],
                "rejected": [],
                "status": AgentStatus.ERROR.value,
                "error_log": ["E_SOURCE_REDACTION_INCOMPLETE"],
            }
        except ValueError as exc:
            code = "E_DUPLICATE_CONFLICT" if str(exc).startswith("E_DUPLICATE_CONFLICT") else "E_ADAPTER_CONTRACT"
            emit_trace_event("source_fetch_rejected", {"reason_code": code}, state)
            return {
                "source_records": [],
                "rejected": [],
                "status": AgentStatus.ERROR.value,
                "error_log": [code],
            }
        except Exception:
            emit_trace_event("source_fetch_failed", {"reason_code": "E_SOURCE_FETCH"}, state)
            return {
                "source_records": [],
                "rejected": [],
                "status": AgentStatus.ERROR.value,
                "error_log": ["E_SOURCE_FETCH"],
            }

        emit_trace_event(
            "source_fetch_completed",
            {"source_set_id": source_set_id, "record_count": len(records), "redacted_record_count": redacted_count},
            state,
        )
        return {
            "validated_input": validated,
            "source_records": records,
            "rejected": [],
            "status": AgentStatus.SUCCESS.value,
        }

    @staticmethod
    def _redact_records(records: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
        """Redact credentials/PII across the whole record before this node returns it.

        **Every string leaf, not just `text`.** A source system puts text wherever it
        likes: a locator can be a spreadsheet filename carrying a person's name, an
        identifier can be minted from an address, a milestone label is free text.
        Redacting `text` alone left all of those in `source_records`, and therefore in
        State, where a later node inspecting them is already too late — this node's own
        return value is the boundary, and the framework's S-3 gate scans it for
        credentials only, not PII.

        `redact_string_leaves` returns `(value, changed, safe)`. `changed` and `safe`
        answer different questions: an already-clean record is `safe` but not `changed`.
        Counting `safe` would make the audit event claim a redaction on every clean
        record. `safe=False` means the pass ran and a leaf still matches a detector,
        which fails the run rather than returning known sensitive content.
        """
        redacted = 0
        cleaned = []
        for record in records:
            cleaned_record, changed, safe = redact_string_leaves(record)
            if not safe:
                raise SensitiveResidueError("E_SOURCE_REDACTION_INCOMPLETE")
            if changed:
                redacted += 1
            cleaned.append(cleaned_record)
        return cleaned, redacted

    def _canonicalise(self, raw_records: object) -> tuple[list[dict[str, Any]], int]:
        """Validate, then redact, then de-duplicate — in that order.

        The order is the point. Redaction rewrites `source_id` along with every other
        string leaf, so two records whose identifiers differ only inside redacted text
        collapse to the same identifier. De-duplicating *after* redaction turns that
        collapse into `E_DUPLICATE_CONFLICT`; de-duplicating first would let the second
        record silently overwrite the first in every `source_id`-keyed lookup downstream.
        """

        if not isinstance(raw_records, list):
            raise ValueError("E_ADAPTER_CONTRACT: adapter output is not a list")
        for record in raw_records:
            self._validate_record(record)
        redacted_records, redacted_count = self._redact_records(raw_records)

        unique: dict[str, tuple[str, dict[str, Any]]] = {}
        for record in redacted_records:
            canonical_bytes = json.dumps(record, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
            source_id = record["source_id"]
            if source_id in unique and unique[source_id][0] != canonical_bytes:
                raise ValueError("E_DUPLICATE_CONFLICT")
            unique.setdefault(source_id, (canonical_bytes, record))
        return [unique[source_id][1] for source_id in sorted(unique)], redacted_count

    def _validate_record(self, record: object) -> None:
        if not isinstance(record, dict) or set(record) != _RECORD_KEYS:
            raise ValueError("E_ADAPTER_CONTRACT: record shape")
        if not all(isinstance(record[key], str) and record[key] for key in ("source_id", "locator", "fetched_at")):
            raise ValueError("E_ADAPTER_CONTRACT: record string field")
        if not isinstance(record["text"], str) or not isinstance(record["open_at_period_start"], bool):
            raise ValueError("E_ADAPTER_CONTRACT: record field type")
        self._parse_utc(record["fetched_at"])
        if not isinstance(record["events"], list):
            raise ValueError("E_ADAPTER_CONTRACT: events")
        for event in record["events"]:
            if not isinstance(event, dict) or set(event) != _EVENT_KEYS:
                raise ValueError("E_ADAPTER_CONTRACT: event shape")
            self._parse_utc(event["event_ts"])
            if event["kind"] not in ("opened", "closed"):
                raise ValueError("E_ADAPTER_CONTRACT: event kind")
            estimate = event["estimate"]
            if estimate is not None and (
                isinstance(estimate, bool)
                or not isinstance(estimate, (int, float))
                or not math.isfinite(float(estimate))
            ):
                raise ValueError("E_ADAPTER_CONTRACT: estimate")
            if event["milestone_id"] is not None and not isinstance(event["milestone_id"], str):
                raise ValueError("E_ADAPTER_CONTRACT: milestone_id")

    @staticmethod
    def _parse_utc(value: object) -> datetime:
        if not isinstance(value, str):
            raise ValueError("E_ADAPTER_CONTRACT: timestamp")
        candidate = value[:-1] + "+00:00" if value.endswith("Z") else value
        try:
            parsed = datetime.fromisoformat(candidate)
        except ValueError:
            raise ValueError("E_ADAPTER_CONTRACT: timestamp") from None
        if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
            raise ValueError("E_ADAPTER_CONTRACT: timestamp must be UTC")
        return parsed
