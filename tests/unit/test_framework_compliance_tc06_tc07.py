"""TC-01 through TC-08 plus template-node structural compliance."""

from __future__ import annotations

import ast
import inspect
import pathlib

import pytest

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from src.schemas.state import State

ROOT = pathlib.Path(__file__).parents[2]


def test_tc01_state_is_flat_typed_dict_contract() -> None:
    assert issubclass(State, dict)
    source = (ROOT / "src" / "schemas" / "state.py").read_text()
    assert "BaseModel" not in source
    assert "@dataclass" not in source
    assert {
        "validated_input",
        "rejected",
        "source_records",
        "metrics",
        "narrative",
        "unresolved",
        "formatted_output",
    } <= set(State.__annotations__)


def test_tc02_invalid_input_is_refused_with_a_reason() -> None:
    """TC-02 — an invalid input must be refused, and the refusal must carry a reason.

    The earlier version of this test asserted that defining a FunctionNode with a
    credential-shaped class attribute raises ``SecurityViolationError``.  That scan does not
    exist in the installed wheel (1.0.1): ``framework.nodes.base_node`` only re-exports
    ``SecurityViolationError`` for callers, and ``__init_subclass__`` checks the @final gate
    overrides and ``required_trust_level`` — nothing else.  The test therefore passed against
    1.0.0 and failed against the wheel CI actually installs.

    This agent's S-2 hook refuses by returning ``status: error`` with a reason code rather
    than raising (raising from the input gate would bypass the reason channel the caller
    reads), so TC-02 is asserted against that contract: a malformed envelope is refused, and
    the caller can tell why.
    """
    from tests.helpers import FakeAdapter, make_config
    from framework.schemas.invocation_context import InvocationContext
    from src.graph.graph import ProjectProgressReportGraph

    graph = ProjectProgressReportGraph(config=make_config(adapter=FakeAdapter()))
    graph.compile()
    ctx = InvocationContext(caller_id="caller-a", caller_trust_level=TrustLevel.VERIFIED_EXTERNAL)
    result = graph.invoke('{"project_id": "project-1", "unexpected_key": "x"}', ctx=ctx)

    assert result["status"] == AgentStatus.ERROR.value
    reasons = " ".join(
        [entry.get("reason_code", "") for entry in result["output"]["rejected"]] + result["output"]["errors"]
    )
    assert "E_" in reasons, "an invalid input was refused without telling the caller why"


def test_tc03_tc04_state_and_node_sources_have_no_credential_context_bypass() -> None:
    state_source = (ROOT / "src" / "schemas" / "state.py").read_text()
    for forbidden in ("jwt", "api_key", "password", "credential", "InvocationContext"):
        assert forbidden.casefold() not in state_source.casefold()
    for path in (ROOT / "src" / "nodes").glob("*.py"):
        tree = ast.parse(path.read_text(), filename=str(path))
        for call in (node for node in ast.walk(tree) if isinstance(node, ast.Call)):
            if isinstance(call.func, ast.Name) and call.func.id == "InvocationContext":
                pytest.fail(f"direct InvocationContext construction in {path}")


def test_tc05_tc11_execute_has_domain_event_but_no_lifecycle_duplicates() -> None:
    failures = []
    for path in (ROOT / "src" / "nodes").glob("*_node.py"):
        tree = ast.parse(path.read_text(), filename=str(path))
        for class_node in (node for node in tree.body if isinstance(node, ast.ClassDef)):
            execute = next(
                (node for node in class_node.body if isinstance(node, ast.FunctionDef) and node.name == "execute"),
                None,
            )
            if execute is None:
                continue
            text = ast.unparse(execute)
            if any(name in text for name in ("node_start", "node_complete", "node_error")):
                failures.append(f"{class_node.name}: duplicate lifecycle event")
            if "from shared.utils.audit_logger import emit_trace_event" not in text or "emit_trace_event(" not in text:
                failures.append(f"{class_node.name}: missing domain trace event")
    assert not failures, failures


class TestFunctionNodeFinalSecurityGates:
    def test_tc06_security_gate_input_cannot_be_overridden(self) -> None:
        with pytest.raises(TypeError, match="_security_gate_input"):

            class _InvalidInputGateOverride(FunctionNode):
                required_trust_level = TrustLevel.ANONYMOUS

                def _security_gate_input(self, state):
                    return state

                def execute(self, state):
                    return {"status": AgentStatus.SUCCESS.value}

    def test_tc07_security_gate_output_cannot_be_overridden(self) -> None:
        with pytest.raises(TypeError, match="_security_gate_output"):

            class _InvalidOutputGateOverride(FunctionNode):
                required_trust_level = TrustLevel.ANONYMOUS

                def _security_gate_output(self, result):
                    return result

                def execute(self, state):
                    return {"status": AgentStatus.SUCCESS.value}


def test_tc08_all_domain_nodes_declare_verified_external() -> None:
    from framework.nodes.base_node import BaseNode
    import src.nodes as nodes

    discovered = {
        value
        for value in vars(nodes).values()
        if inspect.isclass(value) and issubclass(value, BaseNode) and value is not BaseNode
    }
    assert discovered
    for node_class in discovered:
        assert "required_trust_level" in node_class.__dict__
        assert node_class.required_trust_level is TrustLevel.VERIFIED_EXTERNAL
