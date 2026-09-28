"""Outer AgentBaseGraph for ProjectProgressReportAgent."""

from __future__ import annotations

from typing import Any

from framework.errors import ConfigError
from framework.graph.agent_base_graph import AgentBaseGraph
from src.nodes.main_node import ReportComposeNode
from src.nodes.post_process_node import SchemaAssembleNode
from src.nodes.pre_process_node import InputValidateNode
from src.schemas.state import State


class ProjectProgressReportGraph(AgentBaseGraph):
    """Cat 2 outer graph preserving the standard five-node envelope."""

    @property
    def name(self) -> str:
        return "ProjectProgressReportAgent"

    @property
    def state_schema(self) -> type:
        return State

    def _validate_config(self) -> None:
        super()._validate_config()
        for key in ("source_sets", "output_schemas", "quality_gate", "provenance"):
            if not isinstance(self.config.get(key), dict):
                raise ConfigError(f"[ProjectProgressReportGraph] '{key}' must be a mapping")
        allowed_projects = self.config.get("allowed_projects")
        if not isinstance(allowed_projects, dict):
            raise ConfigError("[ProjectProgressReportGraph] 'allowed_projects' must be a mapping")
        if self.config.get("quality_gate", {}).get("on_failure") not in ("exclude_record", "fail_run"):
            raise ConfigError("[ProjectProgressReportGraph] quality_gate.on_failure is invalid")
        provenance = self.config["provenance"]
        if provenance.get("numeric_tolerance") != 0:
            raise ConfigError("[ProjectProgressReportGraph] provenance.numeric_tolerance must be 0")
        if provenance.get("on_unverified_claim") != "move_to_unresolved":
            raise ConfigError("[ProjectProgressReportGraph] provenance.on_unverified_claim is invalid")

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
        super().register_nodes()
        self._nodes["pre_process"] = InputValidateNode(config=self.config)
        self._nodes["main"] = ReportComposeNode(config=self.config)
        self._nodes["post_process"] = SchemaAssembleNode(config=self.config)

    def get_output(self, state: State) -> dict[str, Any]:
        envelope: dict[str, Any] = super().get_output(state)
        envelope["output"] = {
            "report": state.get("formatted_output") or None,
            "unresolved": state.get("unresolved", []),
            "rejected": state.get("rejected", []),
            "errors": self._caller_visible_errors(state),
        }
        return envelope

    @staticmethod
    def _caller_visible_errors(state: State) -> list[str]:
        """Surface why a run failed.

        ``AgentBaseGraph.get_output()`` returns status/trace_id/node_history but not
        ``error_log``, so a node that fails with a specific reason code reaches the caller as a
        bare ``status: error``.  A failure inside the inner workflow (an unavailable source
        credential, say) was only observable in the S-4 event stream, which the caller does not
        receive.  Two adjustments are required and both are deliberate:

        * de-duplicate -- ``error_log`` is an additive channel, so one failure travelling through
          several nodes appears several times;
        * strip tracebacks -- the framework puts the traceback of an unhandled exception into the
          same channel, and returning it to a caller would expose file paths and frames.  The
          untruncated value stays in state, so debugging is unaffected.
        """
        seen: set[str] = set()
        surfaced: list[str] = []
        for entry in state.get("error_log") or []:
            message = str(entry).split("\nTraceback", 1)[0].strip()
            if message and message not in seen:
                seen.add(message)
                surfaced.append(message)
        return surfaced
