"""PB-2 plus manifest/schema/config executable-contract proofs."""

from __future__ import annotations

import importlib
import json

import pytest
import yaml

from framework.errors import ConfigError
from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from framework.secrets.context import bound_secrets
from shared.secrets.inmemory_provider import InMemoryProvider
from src.graph.graph import ProjectProgressReportGraph
from src.nodes.post_process_node import _FREE_TEXT_PATHS
from tests.helpers import MOCK_SECRET, ROOT, make_config, request_envelope


def test_pb2_full_post_invoke_output_is_json_serialisable_primitives() -> None:
    config = make_config()
    graph = ProjectProgressReportGraph(config=config)
    graph.compile()
    provider = InMemoryProvider({"GITLAB_READ_TOKEN": MOCK_SECRET})
    with bound_secrets(provider):
        output = graph.invoke(
            request_envelope(),
            ctx=InvocationContext(
                caller_id="caller-a",
                caller_trust_level=TrustLevel.VERIFIED_EXTERNAL,
            ),
        )
    encoded = json.dumps(output, ensure_ascii=False, allow_nan=False)
    assert json.loads(encoded) == output


def test_manifest_class_imports_and_has_no_placeholders() -> None:
    manifest_text = (ROOT / "config" / "agent.yaml").read_text(encoding="utf-8")
    assert "{{" not in manifest_text and "}}" not in manifest_text
    manifest = yaml.safe_load(manifest_text)
    module_name, class_name = manifest["class"].rsplit(".", 1)
    graph_class = getattr(importlib.import_module(module_name), class_name)
    assert graph_class is ProjectProgressReportGraph


@pytest.mark.parametrize("key", ["source_sets", "output_schemas", "quality_gate", "provenance"])
def test_required_mapping_config_fails_at_compile(key: str) -> None:
    config = make_config()
    config.pop(key)
    with pytest.raises(ConfigError, match=key):
        ProjectProgressReportGraph(config=config).compile()


def test_s3_review_anchor_covers_every_narrative_schema_prose_field() -> None:
    assert _FREE_TEXT_PATHS == (
        ("summary", "text"),
        ("risks", "*", "description"),
        ("blockers", "*", "description"),
        ("next_steps", "*", "text"),
    )


def test_schema_lint_forbids_person_evaluation_surface() -> None:
    config = make_config()
    schema_config = config["output_schemas"]["sprint_status_v1"]
    assert schema_config["forbid_person_evaluation"] is True
    schema = json.loads((ROOT / schema_config["path"]).read_text(encoding="utf-8"))
    prohibited = {"person", "employee", "individual", "performance_score", "ranking"}

    def property_names(value: object):
        if isinstance(value, dict):
            properties = value.get("properties")
            if isinstance(properties, dict):
                yield from properties
            for child in value.values():
                yield from property_names(child)
        elif isinstance(value, list):
            for child in value:
                yield from property_names(child)

    assert prohibited.isdisjoint(set(property_names(schema)))
