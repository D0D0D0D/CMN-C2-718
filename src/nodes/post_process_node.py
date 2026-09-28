"""Target-schema assembly, provenance verification, and S-3 output gate."""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Iterator
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.security.credential_detector import detect_credentials
from shared.security.pii_detector import detect_pii
from src.services.content_safety import iter_string_leaves

_FREE_TEXT_PATHS = (
    ("summary", "text"),
    ("risks", "*", "description"),
    ("blockers", "*", "description"),
    ("next_steps", "*", "text"),
)
_NUMBER_RE = re.compile(r"[-+]?(?:\d+(?:\.\d+)?|\.\d+)%?")


class SchemaAssembleNode(FunctionNode):
    """Assemble, verify, and validate the final sprint-status document."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        super().__init__()
        self._config = config or {}

    def _extra_security_gate_output(self, result: dict[str, Any]) -> dict[str, Any]:
        """S-3: independently walk the nested document and fail closed."""

        document = result.get("formatted_output")
        if not isinstance(document, dict):
            raise RuntimeError("S-3 output gate: formatted_output is missing or invalid")
        for path, text in iter_string_leaves(document):
            if detect_credentials(text) or detect_pii(text):
                raise RuntimeError(f"S-3 output gate: sensitive text at {self._format_path(path)}")
        metrics = document.get("metrics")
        narrative = document.get("narrative")
        if not isinstance(metrics, dict) or not isinstance(narrative, dict):
            raise RuntimeError("S-3 output gate: metrics or narrative is missing")
        allowed = self._metric_numbers(metrics)
        for path, text in self._iter_prose(narrative):
            if not self._figures_allowed(text, allowed):
                raise RuntimeError(f"S-3 output gate: unapproved figure at {self._format_path(path)}")
        return result

    def execute(self, state: dict[str, Any]) -> dict[str, Any]:
        from shared.utils.audit_logger import emit_trace_event

        if state.get("status") == AgentStatus.ERROR.value:
            emit_trace_event("schema_assemble_passthrough", {"status": "error"}, state)
            return {"status": AgentStatus.ERROR.value}
        try:
            validated = state["validated_input"]
            metrics = state["metrics"]
            narrative = state["narrative"]
            schema = self._load_schema(validated["output_schema_id"])
            projected = self._project_document(validated, metrics, narrative)
            self._validate_instance(projected, schema, schema, "$", check_format=True)
        except Exception:
            emit_trace_event("schema_assemble_failed", {"reason_code": "E_SCHEMA_VALIDATION"}, state)
            return {"status": AgentStatus.ERROR.value, "error_log": ["E_SCHEMA_VALIDATION"]}

        source_by_id = {record["source_id"]: record for record in state.get("source_records", [])}
        unresolved = list(state.get("unresolved", []))
        verified_narrative: dict[str, Any] = {}

        summary, summary_reason = self._verify_element(narrative["summary"], "text", source_by_id, metrics)
        if summary_reason:
            unresolved.append({"field_path": "narrative.summary", "reason_code": summary_reason})
            emit_trace_event("schema_assemble_failed", {"reason_code": "E_SUMMARY_UNVERIFIED"}, state)
            return {
                "unresolved": unresolved,
                "status": AgentStatus.ERROR.value,
                "error_log": ["E_SUMMARY_UNVERIFIED"],
            }
        verified_narrative["summary"] = summary

        for group, text_key in (("risks", "description"), ("blockers", "description"), ("next_steps", "text")):
            verified_items = []
            for index, element in enumerate(narrative[group]):
                verified, reason = self._verify_element(element, text_key, source_by_id, metrics)
                if reason:
                    unresolved.append({"field_path": f"narrative.{group}[{index}]", "reason_code": reason})
                else:
                    verified_items.append(verified)
            verified_narrative[group] = verified_items

        final_document = {**projected, "narrative": verified_narrative}
        try:
            self._validate_instance(final_document, schema, schema, "$", check_format=True)
        except Exception:
            emit_trace_event("schema_assemble_failed", {"reason_code": "E_SCHEMA_REVALIDATION"}, state)
            return {
                "unresolved": unresolved,
                "status": AgentStatus.ERROR.value,
                "error_log": ["E_SCHEMA_REVALIDATION"],
            }
        emit_trace_event(
            "schema_assembled",
            {"unresolved_count": len(unresolved), "schema_id": validated["output_schema_id"]},
            state,
        )
        return {
            "formatted_output": final_document,
            "unresolved": unresolved,
            "status": AgentStatus.SUCCESS.value,
        }

    def _load_schema(self, schema_id: str) -> dict[str, Any]:
        schemas = self._config.get("output_schemas", {})
        if schema_id not in schemas:
            raise ValueError("E_SCHEMA_UNKNOWN")
        relative_path = schemas[schema_id].get("path")
        if not isinstance(relative_path, str):
            raise ValueError("E_SCHEMA_VALIDATION")
        repo_root = Path(__file__).resolve().parents[2]
        schema_path = (repo_root / relative_path).resolve()
        if repo_root not in schema_path.parents:
            raise ValueError("E_SCHEMA_VALIDATION")
        loaded = json.loads(schema_path.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            raise ValueError("E_SCHEMA_VALIDATION")
        return loaded

    def _project_document(
        self,
        validated: dict[str, Any],
        metrics: dict[str, Any],
        narrative: dict[str, Any],
    ) -> dict[str, Any]:
        version = self._config["output_schemas"][validated["output_schema_id"]]["version"]
        projected_narrative = {
            "summary": self._project_element(narrative["summary"], "text"),
            "risks": [self._project_element(item, "description") for item in narrative["risks"]],
            "blockers": [self._project_element(item, "description") for item in narrative["blockers"]],
            "next_steps": [self._project_element(item, "text") for item in narrative["next_steps"]],
        }
        return {
            "report_meta": {
                "project_id": validated["project_id"],
                "period_start_utc": validated["period_start_utc"],
                "period_end_utc": validated["period_end_utc"],
                "source_set_id": validated["source_set_id"],
                "schema_version": version,
            },
            "metrics": metrics,
            "narrative": projected_narrative,
        }

    @staticmethod
    def _project_element(element: dict[str, Any], text_key: str) -> dict[str, Any]:
        return {
            text_key: element[text_key],
            "source_ids": list(dict.fromkeys(item["source_id"] for item in element["evidence"])),
        }

    def _verify_element(
        self,
        element: dict[str, Any],
        text_key: str,
        source_by_id: dict[str, dict[str, Any]],
        metrics: dict[str, Any],
    ) -> tuple[dict[str, Any] | None, str | None]:
        minimum = self._config.get("provenance", {}).get("min_quoted_span_chars")
        if not isinstance(minimum, int) or minimum < 1:
            return None, "E_PROVENANCE_CONFIG"
        evidence = element.get("evidence")
        if not isinstance(evidence, list) or not evidence:
            return None, "E_EVIDENCE_MISSING"
        source_ids: list[str] = []
        for claim in evidence:
            source_id = claim.get("source_id") if isinstance(claim, dict) else None
            quoted_span = claim.get("quoted_span") if isinstance(claim, dict) else None
            if source_id not in source_by_id:
                return None, "E_EVIDENCE_SOURCE_UNKNOWN"
            normalised_span = self._normalise(quoted_span) if isinstance(quoted_span, str) else ""
            if len(normalised_span.strip()) < minimum:
                return None, "E_EVIDENCE_SPAN_SHORT"
            if normalised_span not in self._normalise(source_by_id[source_id]["text"]):
                return None, "E_EVIDENCE_SPAN_MISMATCH"
            source_ids.append(source_id)
        text = element.get(text_key)
        if not isinstance(text, str) or not self._figures_allowed(text, self._metric_numbers(metrics)):
            return None, "E_FIGURE_UNVERIFIED"
        return {text_key: text, "source_ids": list(dict.fromkeys(source_ids))}, None

    @staticmethod
    def _normalise(value: str) -> str:
        return unicodedata.normalize("NFKC", value).casefold()

    @classmethod
    def _figures_allowed(cls, text: str, allowed: set[Decimal]) -> bool:
        normalised = unicodedata.normalize("NFKC", text).casefold()
        for match in _NUMBER_RE.finditer(normalised):
            token = match.group()
            is_percent = token.endswith("%")
            try:
                value = Decimal(token[:-1] if is_percent else token)
            except InvalidOperation:
                return False
            candidates = {value}
            if is_percent:
                candidates.add(value / Decimal(100))
            if not candidates.intersection(allowed):
                return False
        return True

    @classmethod
    def _metric_numbers(cls, metrics: object) -> set[Decimal]:
        numbers: set[Decimal] = set()
        if isinstance(metrics, dict):
            for value in metrics.values():
                numbers.update(cls._metric_numbers(value))
        elif isinstance(metrics, list):
            for value in metrics:
                numbers.update(cls._metric_numbers(value))
        elif isinstance(metrics, (int, float)) and not isinstance(metrics, bool):
            numbers.add(Decimal(str(metrics)))
        return numbers

    @classmethod
    def _iter_prose(cls, narrative: object, path: tuple[object, ...] = ()) -> Iterator[tuple[tuple[object, ...], str]]:
        if isinstance(narrative, dict):
            for key, value in narrative.items():
                if key == "source_ids":
                    continue
                yield from cls._iter_prose(value, (*path, key))
        elif isinstance(narrative, list):
            for index, value in enumerate(narrative):
                yield from cls._iter_prose(value, (*path, index))
        elif isinstance(narrative, str):
            yield path, narrative

    @staticmethod
    def _format_path(path: tuple[object, ...]) -> str:
        return ".".join(str(part) for part in path)

    @classmethod
    def _validate_instance(
        cls,
        instance: object,
        schema: dict[str, Any],
        root: dict[str, Any],
        path: str,
        *,
        check_format: bool,
    ) -> None:
        if "$ref" in schema:
            target: object = root
            for part in schema["$ref"].removeprefix("#/").split("/"):
                target = target[part]  # type: ignore[index]
            cls._validate_instance(instance, target, root, path, check_format=check_format)  # type: ignore[arg-type]
            return
        expected = schema.get("type")
        allowed_types = expected if isinstance(expected, list) else [expected] if expected else []
        if allowed_types and not any(cls._matches_type(instance, item) for item in allowed_types):
            raise ValueError(f"{path}: type")
        if "const" in schema and instance != schema["const"]:
            raise ValueError(f"{path}: const")
        if isinstance(instance, dict):
            required = schema.get("required", [])
            if any(key not in instance for key in required):
                raise ValueError(f"{path}: required")
            properties = schema.get("properties", {})
            if schema.get("additionalProperties") is False and set(instance) - set(properties):
                raise ValueError(f"{path}: additionalProperties")
            for key, value in instance.items():
                if key in properties:
                    cls._validate_instance(value, properties[key], root, f"{path}.{key}", check_format=check_format)
        elif isinstance(instance, list):
            if len(instance) < schema.get("minItems", 0):
                raise ValueError(f"{path}: minItems")
            item_schema = schema.get("items")
            if isinstance(item_schema, dict):
                for index, value in enumerate(instance):
                    cls._validate_instance(value, item_schema, root, f"{path}[{index}]", check_format=check_format)
        elif isinstance(instance, str):
            if len(instance) < schema.get("minLength", 0) or len(instance) > schema.get("maxLength", len(instance)):
                raise ValueError(f"{path}: length")
            if check_format and schema.get("format") == "date-time":
                datetime.fromisoformat(instance[:-1] + "+00:00" if instance.endswith("Z") else instance)
        elif isinstance(instance, (int, float)) and not isinstance(instance, bool):
            if "minimum" in schema and instance < schema["minimum"]:
                raise ValueError(f"{path}: minimum")
            if "maximum" in schema and instance > schema["maximum"]:
                raise ValueError(f"{path}: maximum")

    @staticmethod
    def _matches_type(instance: object, expected: str) -> bool:
        return {
            "object": isinstance(instance, dict),
            "array": isinstance(instance, list),
            "string": isinstance(instance, str),
            "number": isinstance(instance, (int, float)) and not isinstance(instance, bool),
            "integer": isinstance(instance, int) and not isinstance(instance, bool),
            "boolean": isinstance(instance, bool),
            "null": instance is None,
        }.get(expected, False)
