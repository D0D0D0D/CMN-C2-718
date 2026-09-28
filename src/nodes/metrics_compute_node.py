"""Deterministic sprint metric computation."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel


class MetricsComputeNode(FunctionNode):
    """Compute all report figures without an LLM."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def execute(self, state: dict[str, Any]) -> dict[str, Any]:
        from shared.utils.audit_logger import emit_trace_event

        if state.get("status") == AgentStatus.ERROR.value:
            emit_trace_event("metrics_compute_passthrough", {"status": "error"}, state)
            return {"status": AgentStatus.ERROR.value}
        try:
            validated = state["validated_input"]
            start = self._parse(validated["period_start_utc"])
            end = self._parse(validated["period_end_utc"])
            records = sorted(state.get("source_records", []), key=lambda record: record["source_id"])
            events: list[tuple[datetime, str, dict[str, Any]]] = []
            for record in records:
                for event in record["events"]:
                    event_ts = self._parse(event["event_ts"])
                    if start <= event_ts < end:
                        events.append((event_ts, record["source_id"], event))
            events.sort(key=lambda item: (item[0], item[1]))

            opened_count = sum(1 for _, _, event in events if event["kind"] == "opened")
            closed_by_source: dict[str, dict[str, Any]] = {}
            for _, source_id, event in events:
                if event["kind"] == "closed":
                    closed_by_source[source_id] = event
            closed_events = sorted(closed_by_source.items())
            denominator = sum(1 for record in records if record["open_at_period_start"]) + opened_count
            rejected = list(state.get("rejected", []))
            close_rate: float | None
            if denominator == 0:
                close_rate = None
                rejected.append(
                    {"stage": "metrics_compute", "locator": "close_rate", "reason_code": "E_ZERO_DENOMINATOR"}
                )
            else:
                close_rate = float(
                    (Decimal(len(closed_events)) / Decimal(denominator)).quantize(
                        Decimal("0.001"), rounding=ROUND_HALF_UP
                    )
                )

            velocity = Decimal("0")
            estimate_missing_count = 0
            for _, event in closed_events:
                if event["estimate"] is None:
                    estimate_missing_count += 1
                else:
                    velocity += Decimal(str(event["estimate"]))

            touched = {event["milestone_id"] for _, _, event in events if event["milestone_id"] is not None}
            latest_membership: dict[str, str | None] = {}
            for record in records:
                prior = [
                    (self._parse(event["event_ts"]), event["milestone_id"])
                    for event in record["events"]
                    if self._parse(event["event_ts"]) < end
                ]
                latest_membership[record["source_id"]] = max(prior, default=(start, None), key=lambda item: item[0])[1]
            milestone_progress = []
            for milestone_id in sorted(touched):
                total = sum(1 for value in latest_membership.values() if value == milestone_id)
                if total == 0:
                    continue
                closed = len({source_id for source_id, event in closed_events if event["milestone_id"] == milestone_id})
                progress = float((Decimal(closed) / Decimal(total)).quantize(Decimal("0.001"), rounding=ROUND_HALF_UP))
                milestone_progress.append({"milestone_id": milestone_id, "progress": progress})
            metrics = {
                "close_rate": close_rate,
                "velocity": float(velocity.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)),
                "milestone_progress": milestone_progress,
                "estimate_missing_count": estimate_missing_count,
            }
        except (KeyError, TypeError, ValueError, ArithmeticError):
            emit_trace_event("metrics_compute_failed", {"reason_code": "E_METRICS_INPUT"}, state)
            return {"status": AgentStatus.ERROR.value, "error_log": ["E_METRICS_INPUT"]}

        emit_trace_event("metrics_computed", {"metric_count": len(metrics)}, state)
        return {
            "metrics": metrics,
            "rejected": rejected,
            "status": AgentStatus.SUCCESS.value,
        }

    @staticmethod
    def _parse(value: str) -> datetime:
        return datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
