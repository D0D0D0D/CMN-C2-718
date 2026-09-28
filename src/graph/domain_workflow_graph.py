"""Inner fixed project-report workflow graph."""

from __future__ import annotations

from typing import Any

from langgraph.graph import END, START

from framework.errors import ConfigError
from framework.graph.base_graph import BaseGraph
from framework.schemas.agent_status import AgentStatus
from src.nodes.metrics_compute_node import MetricsComputeNode
from src.nodes.narrative_draft_node import NarrativeDraftNode
from src.nodes.source_content_gate_node import SourceContentGateNode
from src.nodes.source_fetch_node import SourceFetchNode
from src.schemas.state import State


class DomainWorkflowGraph(BaseGraph):
    """Custom four-node graph nested inside the outer ``main`` slot."""

    @property
    def name(self) -> str:
        return "project_progress_report_workflow"

    @property
    def state_schema(self) -> type:
        return State

    def _validate_config(self) -> None:
        for key in ("source_sets", "quality_gate", "provenance"):
            if not isinstance(self.config.get(key), dict):
                raise ConfigError(f"[DomainWorkflowGraph] '{key}' must be a mapping")

    def _extra_initial_state(self) -> dict[str, Any]:
        return {
            "validated_input": {},
            "rejected": [],
            "source_records": [],
            "metrics": {},
            "narrative": {},
            "unresolved": [],
            "formatted_output": {},
        }

    def register_nodes(self) -> None:
        service = self.config.get("service")
        self._nodes = {
            "source_fetch": SourceFetchNode(config=self.config, service=service),
            "source_content_gate": SourceContentGateNode(config=self.config),
            "metrics_compute": MetricsComputeNode(),
            "narrative_draft": NarrativeDraftNode(llm=self.config.get("llm")),
        }

    def add_edges(self) -> None:
        self._sg.add_edge(START, "source_fetch")
        self._sg.add_conditional_edges("source_fetch", self._after_source_fetch)
        self._sg.add_conditional_edges("source_content_gate", self._after_source_content_gate)
        self._sg.add_conditional_edges("metrics_compute", self._after_metrics_compute)
        self._sg.add_edge("narrative_draft", END)

    def _after_source_fetch(self, state: State) -> str:
        return END if state.get("status") == AgentStatus.ERROR.value else "source_content_gate"

    def _after_source_content_gate(self, state: State) -> str:
        return END if state.get("status") == AgentStatus.ERROR.value else "metrics_compute"

    def _after_metrics_compute(self, state: State) -> str:
        return END if state.get("status") == AgentStatus.ERROR.value else "narrative_draft"

    def route(self, state: State) -> str:
        return END if state.get("status") == AgentStatus.ERROR.value else "narrative_draft"

    def get_output(self, state: State) -> dict[str, Any]:
        return {
            "metrics": state.get("metrics", {}),
            "narrative": state.get("narrative", {}),
            "source_records": state.get("source_records", []),
            "rejected": state.get("rejected", []),
            "status": state.get("status", AgentStatus.ERROR.value),
            "trace_id": state.get("trace_id", ""),
            "correlation_id": state.get("correlation_id", ""),
            "node_history": state.get("node_history", []),
            "error_log": state.get("error_log", []),
        }
