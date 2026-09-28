"""GraphNode composing the inner project-report workflow."""

from __future__ import annotations

import json
from typing import Any, ClassVar

from framework.nodes.graph_node import GraphNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from src.graph.domain_workflow_graph import DomainWorkflowGraph


class ReportComposeNode(GraphNode):
    """Delegate report composition to a cached inner graph."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL
    error_strategy: ClassVar[str] = "propagate"
    propagate_hitl: ClassVar[bool] = False

    def __init__(self, config: dict[str, Any]) -> None:
        super().__init__()
        self._config = config
        self._subgraph = DomainWorkflowGraph(config=self._config)
        self._subgraph.compile()

    def get_subgraph(self) -> DomainWorkflowGraph:
        return self._subgraph

    def extract_input(self, state: dict[str, Any]) -> str:
        return json.dumps(state.get("validated_input", {}), sort_keys=True, ensure_ascii=False)

    def merge_output(self, state: dict[str, Any], sub_result: dict[str, Any]) -> dict[str, Any]:
        return {
            "metrics": sub_result.get("metrics", {}),
            "narrative": sub_result.get("narrative", {}),
            "source_records": sub_result.get("source_records", []),
            "rejected": [*state.get("rejected", []), *sub_result.get("rejected", [])],
            "status": sub_result.get("status", AgentStatus.ERROR.value),
        }
