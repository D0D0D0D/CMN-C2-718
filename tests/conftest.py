"""Fixtures consumed by the live-LLM check run during development.

That harness builds the graph from `agent_config` and invokes it with `case_payload`. This agent
accepts a strict JSON envelope rather than free prose, and it authorises the caller against
`allowed_projects`, so the generic defaults ("process one main use case") are rejected before the
LLM is ever reached. Supplying the fixtures here lets the live check exercise the real path.

The adapter is a fake (no network in tests); the LLM is NOT faked here — the harness injects its
own live client over `config["llm"]`.
"""

import pytest

from tests.helpers import FakeAdapter, make_config, request_envelope

LIVE_CALLER_ID = "caller-a"
LIVE_PROJECT_ID = "project-1"


@pytest.fixture
def agent_config() -> dict:
    """Production config.yaml + a fake source adapter + caller authorisation for the live caller."""
    config = make_config(adapter=FakeAdapter(), caller_id=LIVE_CALLER_ID, project_id=LIVE_PROJECT_ID)
    config.pop("llm", None)  # the live check injects its own client
    return config


@pytest.fixture
def case_payload() -> str:
    """A valid request envelope for the main use case (one project, one period)."""
    return request_envelope(project_id=LIVE_PROJECT_ID)


@pytest.fixture
def live_caller_id() -> str:
    return LIVE_CALLER_ID
