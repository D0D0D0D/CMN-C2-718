"""End-to-end graph tests with injected LLM and source adapter fakes."""

import json

from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from framework.secrets.context import bound_secrets
from shared.secrets.inmemory_provider import InMemoryProvider
from src.graph.graph import ProjectProgressReportGraph
from tests.helpers import (
    MOCK_SECRET,
    FakeAdapter,
    FakeLLM,
    canonical_record,
    make_config,
    request_envelope,
    valid_narrative,
)


def invoke_graph(config: dict, raw: str | None = None) -> dict:
    graph = ProjectProgressReportGraph(config=config)
    graph.compile()
    provider = InMemoryProvider({"GITLAB_READ_TOKEN": MOCK_SECRET})
    graph.provision_secrets(provider)
    ctx = InvocationContext(
        caller_id="caller-a",
        caller_trust_level=TrustLevel.VERIFIED_EXTERNAL,
        session_id="integration-session",
    )
    with bound_secrets(provider):
        return graph.invoke(raw or request_envelope(), ctx=ctx)


def test_full_graph_returns_report_and_preserves_standard_envelope() -> None:
    adapter = FakeAdapter()
    llm = FakeLLM()
    output = invoke_graph(make_config(adapter=adapter, llm=llm))
    assert output["status"] == "success"
    assert set(("output", "status", "trace_id", "correlation_id", "node_history")) <= set(output)
    assert output["output"]["report"]["metrics"]["velocity"] == 0.5
    assert output["output"]["unresolved"] == []
    assert output["output"]["rejected"] == []
    assert len(adapter.calls) == 1
    assert len(llm.messages) == 1


def test_unknown_source_and_forbidden_project_never_invoke_adapter() -> None:
    adapter = FakeAdapter()
    config = make_config(adapter=adapter)
    unknown = invoke_graph(config, request_envelope(source_set_id="unknown"))
    forbidden = invoke_graph(config, request_envelope(project_id="other-project"))
    assert unknown["status"] == "error"
    assert unknown["output"]["rejected"][0]["reason_code"] == "E_SOURCE_SET_UNKNOWN"
    assert forbidden["output"]["rejected"][0]["reason_code"] == "E_PROJECT_FORBIDDEN"
    assert adapter.calls == []


def test_bad_fetched_record_is_excluded_before_llm() -> None:
    bad = canonical_record(text="\ufffd" * 30)
    bad["source_id"] = "source-bad"
    bad["locator"] = "tracker/item/bad"
    adapter = FakeAdapter([canonical_record(), bad])
    llm = FakeLLM()
    output = invoke_graph(make_config(adapter=adapter, llm=llm))
    assert output["status"] == "success"
    prompt = json.dumps(llm.messages, ensure_ascii=False)
    assert "\ufffd" not in prompt
    assert output["output"]["rejected"][0]["reason_code"] == "E_QUALITY_REPLACEMENT_RATIO"


def test_raw_source_credential_is_redacted_before_llm_prompt() -> None:
    raw_marker = "mock-sensitive-value-123456"
    record = canonical_record(text=f"Bearer {raw_marker} clean work status remains.")
    narrative = valid_narrative()
    for element, text_key in (
        (narrative["summary"], "text"),
        (narrative["risks"][0], "description"),
        (narrative["next_steps"][0], "text"),
    ):
        element[text_key] = "clean work status remains."
        element["evidence"][0]["quoted_span"] = "clean work status remains"
    llm = FakeLLM(narrative=narrative)
    output = invoke_graph(make_config(adapter=FakeAdapter([record]), llm=llm))
    assert output["status"] == "success"
    prompt = json.dumps(llm.messages, ensure_ascii=False)
    assert raw_marker not in prompt
    assert "[REDACTED]" in prompt


def test_source_metadata_pii_is_redacted_at_fetch_not_downstream() -> None:
    """PII outside `text` must not reach State or the model.

    `source_fetch` is the boundary: it is the first node that puts fetched content into
    a return dict, and the framework gate on that dict scans for credentials only. A
    downstream node that inspects metadata is therefore one node too late — by then the
    value is already in State.

    The locator here is the shape that actually defeats a prose-oriented detector: the
    address is glued into a filename, so the trailing `_progress.xlsx` is absorbed into
    the domain and a whole-string scan finds nothing. `redact_string_leaves` feeds the
    shared detector one structural token at a time, which is what catches it.
    """
    marker = "taro@example.com"
    record = canonical_record(text="clean work status remains.")
    record["source_id"] = f"issue-{marker}-1"
    record["locator"] = f"tracker/2026Q3_{marker}_progress.xlsx"
    record["events"][0]["milestone_id"] = f"milestone-{marker}"

    narrative = valid_narrative()
    for element, text_key in (
        (narrative["summary"], "text"),
        (narrative["risks"][0], "description"),
        (narrative["next_steps"][0], "text"),
    ):
        element[text_key] = "clean work status remains."
        element["evidence"][0]["quoted_span"] = "clean work status remains"
        # the redacted identifier is what the record actually carries by the time the
        # model sees it, so that is what a citation has to name
        element["evidence"][0]["source_id"] = "[REDACTED]-1"

    llm = FakeLLM(narrative=narrative)
    output = invoke_graph(make_config(adapter=FakeAdapter([record]), llm=llm))

    assert output["status"] == "success"
    assert marker not in json.dumps(llm.messages, ensure_ascii=False), "PII reached the model"
    assert marker not in json.dumps(output, ensure_ascii=False), "PII reached the caller"

    # Control: the run really did exercise the path — the redaction fired and the
    # report was produced, so the assertions above are not passing on an empty run.
    assert "[REDACTED]" in json.dumps(llm.messages, ensure_ascii=False)
    assert output["output"]["report"] is not None


def test_unverified_optional_claim_surfaces_to_caller() -> None:
    narrative = valid_narrative()
    narrative["risks"][0]["evidence"][0]["source_id"] = "missing"
    output = invoke_graph(make_config(llm=FakeLLM(narrative=narrative)))
    assert output["status"] == "success"
    assert output["output"]["report"]["narrative"]["risks"] == []
    assert output["output"]["unresolved"][0]["reason_code"] == "E_EVIDENCE_SOURCE_UNKNOWN"


def test_adapter_order_does_not_change_report_metrics() -> None:
    second = canonical_record(estimate=1.25)
    second["source_id"] = "source-2"
    second["locator"] = "tracker/item/2"
    narrative = valid_narrative()
    narrative["summary"]["text"] = "delivery remains on track."
    result_a = invoke_graph(
        make_config(adapter=FakeAdapter([canonical_record(), second]), llm=FakeLLM(narrative=narrative))
    )
    result_b = invoke_graph(
        make_config(adapter=FakeAdapter([second, canonical_record()]), llm=FakeLLM(narrative=narrative))
    )
    assert result_a["output"]["report"]["metrics"] == result_b["output"]["report"]["metrics"]
