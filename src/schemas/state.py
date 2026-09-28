"""State contract for ProjectProgressReportAgent."""

from typing import Any

from framework.schemas.agent_state import AgentState


class State(AgentState):
    """Flat, checkpoint-safe state used by the outer and inner graphs."""

    validated_input: dict[str, Any]
    rejected: list[dict[str, Any]]
    source_records: list[dict[str, Any]]
    metrics: dict[str, Any]
    narrative: dict[str, Any]
    unresolved: list[dict[str, Any]]
    formatted_output: dict[str, Any]
