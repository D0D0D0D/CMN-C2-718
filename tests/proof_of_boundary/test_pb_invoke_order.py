"""PB-6 and TC-08: dynamic all-node invoke-order and S-1 denial checks."""

import importlib
import inspect
import pkgutil

import pytest

from framework.nodes.base_node import BaseNode
from framework.schemas.trust_level import TrustLevel
from src.nodes.main_node import ReportComposeNode
from src.nodes.metrics_compute_node import MetricsComputeNode
from src.nodes.narrative_draft_node import NarrativeDraftNode
from src.nodes.post_process_node import SchemaAssembleNode
from src.nodes.pre_process_node import InputValidateNode
from src.nodes.source_content_gate_node import SourceContentGateNode
from src.nodes.source_fetch_node import SourceFetchNode
from tests.helpers import FakeLLM, framework_state, make_config


def _discover_node_classes() -> list[type]:
    package = importlib.import_module("src.nodes")
    discovered = []
    for _, module_name, _ in pkgutil.walk_packages(package.__path__, prefix="src.nodes."):
        module = importlib.import_module(module_name)
        for value in vars(module).values():
            if (
                inspect.isclass(value)
                and issubclass(value, BaseNode)
                and value is not BaseNode
                and value.__module__ == module_name
                and not inspect.isabstract(value)
            ):
                discovered.append(value)
    return sorted(set(discovered), key=lambda item: item.__name__)


def _instance(node_class: type) -> BaseNode:
    config = make_config()
    factories = {
        InputValidateNode: lambda: InputValidateNode(config),
        ReportComposeNode: lambda: ReportComposeNode(config),
        SchemaAssembleNode: lambda: SchemaAssembleNode(config),
        SourceFetchNode: lambda: SourceFetchNode(config, config["service"]),
        SourceContentGateNode: lambda: SourceContentGateNode(config),
        MetricsComputeNode: MetricsComputeNode,
        NarrativeDraftNode: lambda: NarrativeDraftNode(FakeLLM()),
    }
    return factories[node_class]()


@pytest.mark.parametrize("node_class", _discover_node_classes())
def test_tc08_s1_denial_prevents_execute_for_every_node(node_class, monkeypatch) -> None:
    node = _instance(node_class)
    calls = []
    monkeypatch.setattr(node_class, "execute", lambda self, state: calls.append(state) or {"status": "success"})
    result = node(framework_state(caller_trust_level=TrustLevel.ANONYMOUS.value, status="pending"))
    assert result["status"] == "error"
    assert "S-1 trust gate denied" in result["error_log"][0]
    assert calls == []


@pytest.mark.parametrize("node_class", _discover_node_classes())
def test_pb6_call_order_for_every_node(node_class, monkeypatch) -> None:
    node = _instance(node_class)
    order = []

    def input_gate(self, state):
        order.append("security_gate_input")
        return state

    def execute(self, state):
        order.append("execute")
        return {"status": "success"}

    def output_gate(self, result):
        order.append("security_gate_output")
        return result

    monkeypatch.setattr(node_class, "_security_gate_input", input_gate)
    monkeypatch.setattr(node_class, "execute", execute)
    monkeypatch.setattr(node_class, "_security_gate_output", output_gate)
    import framework.nodes.base_node as base_node_module

    monkeypatch.setattr(
        base_node_module,
        "emit_trace_event",
        lambda event_type, payload, state: order.append(f"event:{event_type}"),
    )
    result = node(framework_state(status="pending"))
    assert result["status"] == "success"
    assert order == [
        "event:node_start",
        "security_gate_input",
        "execute",
        "security_gate_output",
        "event:node_complete",
    ]
