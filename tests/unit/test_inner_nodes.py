"""Inner workflow business and security boundary tests."""

import copy
import json

from framework.secrets.context import bound_secrets
from shared.secrets.inmemory_provider import InMemoryProvider
from src.nodes.metrics_compute_node import MetricsComputeNode
from src.nodes.narrative_draft_node import NarrativeDraftNode
from src.nodes.source_content_gate_node import SourceContentGateNode
from shared.utils import audit_logger

from src.nodes import source_fetch_node as source_fetch_module
from src.nodes.source_fetch_node import SourceFetchNode
from tests.helpers import (
    MOCK_SECRET,
    FakeAdapter,
    FakeLLM,
    canonical_record,
    framework_state,
    make_config,
)


def test_fetch_uses_secret_boundary_and_collapses_identical_duplicates() -> None:
    record = canonical_record()
    adapter = FakeAdapter([record, copy.deepcopy(record)])
    config = make_config(adapter=adapter)
    node = SourceFetchNode(config=config, service=config["service"])
    inner_state = framework_state(user_input=json.dumps(framework_state()["validated_input"]), status="pending")
    with bound_secrets(InMemoryProvider({"GITLAB_READ_TOKEN": MOCK_SECRET})):
        result = node(inner_state)
    assert result["status"] == "success"
    assert len(result["source_records"]) == 1
    assert adapter.calls[0]["credential"] == MOCK_SECRET
    assert MOCK_SECRET not in json.dumps(result)


def test_fetch_missing_secret_and_duplicate_conflict_fail_closed() -> None:
    config = make_config(adapter=FakeAdapter())
    node = SourceFetchNode(config=config, service=config["service"])
    inner_state = framework_state(user_input=json.dumps(framework_state()["validated_input"]), status="pending")
    with bound_secrets(InMemoryProvider()):
        missing = node(inner_state)
    assert missing["error_log"] == ["E_SOURCE_CREDENTIAL_UNAVAILABLE"]

    conflicting = canonical_record(text="different source text")
    config = make_config(adapter=FakeAdapter([canonical_record(), conflicting]))
    with bound_secrets(InMemoryProvider({"GITLAB_READ_TOKEN": MOCK_SECRET})):
        conflict = SourceFetchNode(config=config, service=config["service"])(inner_state)
    assert conflict["error_log"] == ["E_DUPLICATE_CONFLICT"]


def test_redaction_count_reports_changes_not_safety(monkeypatch) -> None:
    """`redact_string_leaves` returns `(value, changed, safe)` — `safe` is not `changed`.

    A record that was already clean also comes back `safe=True`, so counting that flag
    made the S-4 audit event claim a redaction on every clean record. The count must
    reflect whether the record actually changed.
    """
    inner_state = framework_state(user_input=json.dumps(framework_state()["validated_input"]), status="pending")
    events: list[tuple[str, dict]] = []
    # execute() imports emit_trace_event at call time, so the binding to replace is the
    # one in the audit-logger module rather than an attribute on the node module.
    monkeypatch.setattr(
        audit_logger,
        "emit_trace_event",
        lambda name, payload, state: events.append((name, payload)),
    )

    # clean record -> nothing redacted
    config = make_config(adapter=FakeAdapter([canonical_record(text="sprint is on track")]))
    with bound_secrets(InMemoryProvider({"GITLAB_READ_TOKEN": MOCK_SECRET})):
        clean = SourceFetchNode(config=config, service=config["service"])(inner_state)
    assert clean["status"] == "success"
    completed = [payload for name, payload in events if name == "source_fetch_completed"]
    assert completed[-1]["redacted_record_count"] == 0

    # record carrying PII -> counted, and the returned text is redacted
    events.clear()
    config = make_config(adapter=FakeAdapter([canonical_record(text="ask taro@example.com about the delay")]))
    with bound_secrets(InMemoryProvider({"GITLAB_READ_TOKEN": MOCK_SECRET})):
        dirty = SourceFetchNode(config=config, service=config["service"])(inner_state)
    assert dirty["status"] == "success"
    completed = [payload for name, payload in events if name == "source_fetch_completed"]
    assert completed[-1]["redacted_record_count"] == 1
    assert "taro@example.com" not in json.dumps(dirty["source_records"])


def test_metadata_pii_is_redacted_in_the_node_return() -> None:
    """No string leaf of a returned record may carry detector-positive PII.

    `locator` is the case that matters and the one the integration test cannot reach:
    it is not put in the model prompt and not projected into the report, so it is only
    observable in this node's return — which is exactly where it enters State. Its shape
    also defeats a whole-string scan: with the address glued into a filename, the
    trailing `_progress.xlsx` is absorbed into the domain and the pattern does not match
    at all. Only per-structural-token scanning catches it.
    """
    marker = "taro@example.com"
    record = canonical_record(text="sprint is on track")
    record["source_id"] = f"issue-{marker}-1"
    record["locator"] = f"tracker/2026Q3_{marker}_progress.xlsx"
    record["events"][0]["milestone_id"] = f"milestone-{marker}"

    config = make_config(adapter=FakeAdapter([record]))
    node = SourceFetchNode(config=config, service=config["service"])
    inner_state = framework_state(user_input=json.dumps(framework_state()["validated_input"]), status="pending")
    with bound_secrets(InMemoryProvider({"GITLAB_READ_TOKEN": MOCK_SECRET})):
        result = node(inner_state)

    assert result["status"] == "success"
    assert marker not in json.dumps(result, ensure_ascii=False)

    returned = result["source_records"][0]
    # named individually so a failure says which field leaked
    assert marker not in returned["locator"]
    assert marker not in returned["source_id"]
    assert marker not in returned["events"][0]["milestone_id"]
    # control: the record survived rather than being dropped, and redaction did fire
    assert "[REDACTED]" in returned["locator"]
    assert returned["text"] == "sprint is on track"


def test_incomplete_redaction_fails_closed(monkeypatch) -> None:
    """`safe=False` means a re-scan still matches. The record must not be returned."""
    monkeypatch.setattr(source_fetch_module, "redact_string_leaves", lambda value: (value, True, False))
    config = make_config(adapter=FakeAdapter([canonical_record()]))
    node = SourceFetchNode(config=config, service=config["service"])
    inner_state = framework_state(user_input=json.dumps(framework_state()["validated_input"]), status="pending")
    with bound_secrets(InMemoryProvider({"GITLAB_READ_TOKEN": MOCK_SECRET})):
        result = node(inner_state)
    assert result["status"] == "error"
    assert result["error_log"] == ["E_SOURCE_REDACTION_INCOMPLETE"]
    assert result["source_records"] == []


def test_source_content_gate_excludes_bad_text_and_redacts_before_output() -> None:
    config = make_config()
    raw_sensitive = "Bearer mock-sensitive-value-123456 clean work status remains."
    records = [canonical_record(text=raw_sensitive), canonical_record(text="\ufffd" * 20)]
    records[1]["source_id"] = "source-2"
    records[1]["locator"] = "tracker/item/2"
    result = SourceContentGateNode(config)(framework_state(source_records=records, status="pending"))
    assert result["status"] == "success"
    assert len(result["source_records"]) == 1
    assert "mock-sensitive-value" not in result["source_records"][0]["text"]
    assert result["rejected"][0]["reason_code"] == "E_QUALITY_REPLACEMENT_RATIO"


def test_metrics_fractional_golden_zero_denominator_and_order_independence() -> None:
    node = MetricsComputeNode()
    golden = node.execute(framework_state(status="pending"))
    assert golden["metrics"] == {
        "close_rate": 1.0,
        "velocity": 0.5,
        "milestone_progress": [{"milestone_id": "milestone-1", "progress": 1.0}],
        "estimate_missing_count": 0,
    }
    second = canonical_record(estimate=1.25)
    second["source_id"] = "source-2"
    second["locator"] = "tracker/item/2"
    state_a = framework_state(source_records=[canonical_record(), second], status="pending")
    state_b = framework_state(source_records=[second, canonical_record()], status="pending")
    assert node.execute(state_a)["metrics"] == node.execute(state_b)["metrics"]

    empty = node.execute(framework_state(source_records=[], status="pending"))
    assert empty["metrics"]["close_rate"] is None
    assert empty["metrics"]["velocity"] == 0.0


def test_llm_contract_and_absence_failures() -> None:
    assert NarrativeDraftNode().execute(framework_state(status="pending"))["error_log"] == ["E_LLM_UNAVAILABLE"]
    malformed = NarrativeDraftNode(FakeLLM(content="not-json")).execute(framework_state(status="pending"))
    assert malformed["error_log"] == ["E_LLM_CONTRACT"]
    unknown = FakeLLM()
    unknown.narrative["unknown"] = "x"
    assert NarrativeDraftNode(unknown).execute(framework_state(status="pending"))["error_log"] == ["E_LLM_CONTRACT"]
    valid = NarrativeDraftNode(FakeLLM()).execute(framework_state(status="pending"))
    assert valid["status"] == "success"
