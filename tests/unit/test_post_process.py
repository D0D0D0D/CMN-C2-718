"""Provenance projection, schema validation, and independent S-3 tests."""

import copy

import pytest

from src.nodes.post_process_node import SchemaAssembleNode
from tests.helpers import canonical_record, framework_state, make_config, valid_narrative


def test_every_prose_element_projects_verified_source_ids() -> None:
    result = SchemaAssembleNode(make_config()).execute(framework_state(status="pending"))
    assert result["status"] == "success"
    narrative = result["formatted_output"]["narrative"]
    assert narrative["summary"]["source_ids"] == ["source-1"]
    assert narrative["risks"][0]["source_ids"] == ["source-1"]
    assert narrative["next_steps"][0]["source_ids"] == ["source-1"]
    assert result["formatted_output"]["metrics"]["velocity"] == 0.5


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        (lambda item: item["evidence"][0].update(source_id="fabricated"), "E_EVIDENCE_SOURCE_UNKNOWN"),
        (lambda item: item["evidence"][0].update(quoted_span="short"), "E_EVIDENCE_SPAN_SHORT"),
        (
            lambda item: item["evidence"][0].update(quoted_span="unrelated evidence span"),
            "E_EVIDENCE_SPAN_MISMATCH",
        ),
        (lambda item: item.update(description="velocity is 999999円."), "E_FIGURE_UNVERIFIED"),
    ],
)
def test_unverified_optional_element_is_removed_and_reported(mutation, reason: str) -> None:
    narrative = valid_narrative()
    mutation(narrative["risks"][0])
    result = SchemaAssembleNode(make_config()).execute(framework_state(narrative=narrative, status="pending"))
    assert result["status"] == "success"
    assert result["formatted_output"]["narrative"]["risks"] == []
    assert result["unresolved"] == [{"field_path": "narrative.risks[0]", "reason_code": reason}]


def test_unverified_summary_fails_without_placeholder() -> None:
    narrative = valid_narrative()
    narrative["summary"]["evidence"][0]["source_id"] = "fabricated"
    result = SchemaAssembleNode(make_config()).execute(framework_state(narrative=narrative, status="pending"))
    assert result["status"] == "error"
    assert result["error_log"] == ["E_SUMMARY_UNVERIFIED"]
    assert "formatted_output" not in result


def test_a_valid_citation_is_not_an_entailment_check() -> None:
    """Pin the documented limit of the provenance check.

    The three checks are: the cited source exists, the quoted span is long enough and is a
    literal substring of that source, and every figure in the prose appears in `metrics`.
    None of them compares the *meaning* of the prose to the span it cites, so a statement
    that contradicts its own citation is accepted as long as it carries no unapproved figure.

    docs/02_design.md states exactly this, and says the guard establishes attributability
    rather than preventing fabrication. This test exists so that the design document cannot
    quietly become false: if entailment checking is ever added, this test fails and the
    documented claim has to be revisited deliberately.
    """
    narrative = valid_narrative()
    # A claim that the cited span flatly contradicts, with no figure in it.
    narrative["risks"][0]["description"] = "the project has already failed."
    result = SchemaAssembleNode(make_config()).execute(framework_state(narrative=narrative, status="pending"))

    assert result["status"] == "success"
    risks = result["formatted_output"]["narrative"]["risks"]
    assert [risk["description"] for risk in risks] == ["the project has already failed."]
    assert risks[0]["source_ids"] == ["source-1"]
    assert result["unresolved"] == []

    # Control: the same element with an unapproved figure IS removed, so the assertion above
    # reflects the absent entailment check and not a verification path that never runs.
    narrative = valid_narrative()
    narrative["risks"][0]["description"] = "the project has already failed 7 times."
    result = SchemaAssembleNode(make_config()).execute(framework_state(narrative=narrative, status="pending"))
    assert result["formatted_output"]["narrative"]["risks"] == []
    assert result["unresolved"] == [{"field_path": "narrative.risks[0]", "reason_code": "E_FIGURE_UNVERIFIED"}]


def test_s3_numeric_allowlist_handles_japanese_and_fullwidth_adjacency() -> None:
    node = SchemaAssembleNode(make_config())
    clean = node.execute(framework_state(status="pending"))
    assert node._extra_security_gate_output(clean) is clean

    injected = copy.deepcopy(clean)
    injected["formatted_output"]["narrative"]["summary"]["text"] = "velocity is ９９９９９９円です."
    with pytest.raises(RuntimeError, match="unapproved figure"):
        node._extra_security_gate_output(injected)

    provisioned = copy.deepcopy(injected)
    provisioned["formatted_output"]["metrics"]["velocity"] = 999999.0
    assert node._extra_security_gate_output(provisioned) is provisioned


def test_s3_recursively_blocks_credentials_and_pii_in_side_fields() -> None:
    node = SchemaAssembleNode(make_config())
    clean = node.execute(framework_state(status="pending"))
    credential = copy.deepcopy(clean)
    credential["formatted_output"]["narrative"]["next_steps"][0]["text"] = "Bearer mock-output-sensitive-123456"
    with pytest.raises(RuntimeError, match="sensitive text"):
        node._extra_security_gate_output(credential)

    pii = copy.deepcopy(clean)
    pii["formatted_output"]["narrative"]["risks"][0]["description"] = "contact mock.user@example.com"
    with pytest.raises(RuntimeError, match="sensitive text"):
        node._extra_security_gate_output(pii)


def test_schema_rejects_fractional_velocity_only_if_contract_regresses() -> None:
    result = SchemaAssembleNode(make_config()).execute(framework_state(status="pending"))
    assert result["formatted_output"]["metrics"]["velocity"] == 0.5


def test_revalidation_runs_after_item_removal() -> None:
    narrative = valid_narrative()
    narrative["risks"][0]["evidence"][0]["source_id"] = "missing"
    result = SchemaAssembleNode(make_config()).execute(
        framework_state(narrative=narrative, source_records=[canonical_record()], status="pending")
    )
    assert result["status"] == "success"
    assert result["formatted_output"]["narrative"]["risks"] == []
