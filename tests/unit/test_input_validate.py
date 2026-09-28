"""Envelope allowlist, period, and source-authorization tests."""

import json
import os
import time

import pytest

from src.nodes.pre_process_node import InputValidateNode
from tests.helpers import framework_state, make_config, request_envelope


def test_valid_envelope_is_canonical_and_timezone_independent() -> None:
    node = InputValidateNode(make_config())
    state = framework_state(status="pending")
    original_tz = os.environ.get("TZ")
    results = []
    try:
        for zone in ("UTC", "Asia/Tokyo"):
            os.environ["TZ"] = zone
            if hasattr(time, "tzset"):
                time.tzset()
            results.append(node.execute(state)["validated_input"])
    finally:
        if original_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = original_tz
        if hasattr(time, "tzset"):
            time.tzset()
    assert results[0] == results[1]
    assert results[0]["period_start_utc"] == "2026-06-30T15:00:00Z"


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        ("not-json", "E_ENVELOPE_JSON"),
        (json.dumps([]), "E_ENVELOPE_TYPE"),
        (request_envelope(extra="x"), "E_UNKNOWN_KEY"),
        (request_envelope(project_id=3), "E_ENVELOPE_TYPE"),
        (request_envelope(source_set_id="unknown"), "E_SOURCE_SET_UNKNOWN"),
        (request_envelope(output_schema_id="unknown"), "E_SCHEMA_UNKNOWN"),
        (request_envelope(period_start="2026-07-01T00:00:00"), "E_PERIOD_FORMAT"),
        (request_envelope(period_end="2026-06-01T00:00:00Z"), "E_PERIOD_RANGE"),
        (request_envelope(project_id="other-project"), "E_PROJECT_FORBIDDEN"),
    ],
)
def test_invalid_envelopes_fail_with_stable_codes(raw: str, code: str) -> None:
    result = InputValidateNode(make_config()).execute(framework_state(user_input=raw, status="pending"))
    assert result["status"] == "error"
    assert result["error_log"] == [code]


def test_s2_provisioned_control_and_rejection() -> None:
    node = InputValidateNode(make_config())
    clean = node(framework_state(status="pending"))
    blocked = node(framework_state(user_input=request_envelope(extra="bypass"), status="pending"))
    assert clean["status"] == "success"
    assert blocked["status"] == "error"
    assert "E_UNKNOWN_KEY" in blocked["error_log"]
