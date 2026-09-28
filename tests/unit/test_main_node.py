"""GraphNode composition contract tests."""

import json

from src.nodes.main_node import ReportComposeNode
from tests.helpers import make_config


def test_graph_node_caches_subgraph_and_serialises_input() -> None:
    node = ReportComposeNode(make_config())
    assert node.get_subgraph() is node.get_subgraph()
    serialised = node.extract_input({"validated_input": {"z": 1, "a": "値"}})
    assert serialised == '{"a": "値", "z": 1}'
    assert json.loads(serialised) == {"a": "値", "z": 1}
    assert "execute" not in ReportComposeNode.__dict__


def test_merge_output_is_explicit_and_returns_only_changed_keys() -> None:
    node = ReportComposeNode(make_config())
    delta = node.merge_output(
        {"rejected": [{"stage": "pre", "locator": "x", "reason_code": "one"}]},
        {
            "metrics": {"velocity": 0.5},
            "narrative": {"summary": {}},
            "source_records": [{"source_id": "one"}],
            "rejected": [{"stage": "inner", "locator": "y", "reason_code": "two"}],
            "status": "success",
            "unexpected": "must-not-pass",
        },
    )
    assert set(delta) == {"metrics", "narrative", "source_records", "rejected", "status"}
    assert len(delta["rejected"]) == 2
