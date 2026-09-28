"""PB-1/PB-3: audit emission and external-service injection boundary."""

import json

from framework.secrets.context import bound_secrets
from shared.secrets.inmemory_provider import InMemoryProvider
from src.nodes.metrics_compute_node import MetricsComputeNode
from src.nodes.narrative_draft_node import NarrativeDraftNode
from src.nodes.post_process_node import SchemaAssembleNode
from src.nodes.pre_process_node import InputValidateNode
from src.nodes.source_content_gate_node import SourceContentGateNode
from src.nodes.source_fetch_node import SourceFetchNode
from tests.helpers import MOCK_SECRET, FakeAdapter, FakeLLM, framework_state, make_config


def test_pb1_each_function_node_execute_emits_domain_event(monkeypatch) -> None:
    events = []
    monkeypatch.setattr(
        "shared.utils.audit_logger.emit_trace_event",
        lambda event_type, payload, state: events.append(event_type),
    )
    config = make_config()
    nodes_and_states = [
        (InputValidateNode(config), framework_state(status="pending")),
        (
            SourceFetchNode(config, config["service"]),
            framework_state(user_input=json.dumps(framework_state()["validated_input"]), status="pending"),
        ),
        (SourceContentGateNode(config), framework_state(status="pending")),
        (MetricsComputeNode(), framework_state(status="pending")),
        (NarrativeDraftNode(FakeLLM()), framework_state(status="pending")),
        (SchemaAssembleNode(config), framework_state(status="pending")),
    ]
    with bound_secrets(InMemoryProvider({"GITLAB_READ_TOKEN": MOCK_SECRET})):
        for node, state in nodes_and_states:
            before = len(events)
            node.execute(state)
            assert len(events) > before, type(node).__name__


def test_pb3_fake_adapter_proves_boundary_without_network_or_secret_leak() -> None:
    adapter = FakeAdapter()
    config = make_config(adapter=adapter)
    node = SourceFetchNode(config, config["service"])
    state = framework_state(user_input=json.dumps(framework_state()["validated_input"]), status="pending")
    with bound_secrets(InMemoryProvider({"GITLAB_READ_TOKEN": MOCK_SECRET})):
        result = node(state)
    assert len(adapter.calls) == 1
    assert adapter.calls[0]["project_id"] == "project-1"
    assert adapter.calls[0]["credential"] == MOCK_SECRET
    assert MOCK_SECRET not in json.dumps(result)
