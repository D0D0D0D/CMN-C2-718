"""Test-only fakes and fixtures for the project report workflow."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Iterator

import yaml

from shared.services.llm.base_llm import BaseLLM
from src.services.service import AdapterRegistry, Service, ServiceIdentity

ROOT = Path(__file__).parents[1]
MOCK_SECRET = "mock-source-read-value"
SOURCE_TEXT = "work item closed successfully with estimate 0.5 points."
QUOTED_SPAN = "work item closed successfully"


def canonical_record(*, text: str = SOURCE_TEXT, estimate: float | None = 0.5) -> dict:
    return {
        "source_id": "source-1",
        "locator": "tracker/item/1",
        "fetched_at": "2026-08-01T00:00:00Z",
        "text": text,
        "events": [
            {
                "event_ts": "2026-07-15T12:00:00Z",
                "kind": "closed",
                "estimate": estimate,
                "milestone_id": "milestone-1",
            }
        ],
        "open_at_period_start": True,
    }


def valid_narrative() -> dict:
    evidence = [{"source_id": "source-1", "quoted_span": QUOTED_SPAN}]
    return {
        "summary": {"text": "velocity is 0.5 points.", "evidence": copy.deepcopy(evidence)},
        "risks": [{"description": "delivery risk remains.", "evidence": copy.deepcopy(evidence)}],
        "blockers": [],
        "next_steps": [{"text": "continue remediation work.", "evidence": copy.deepcopy(evidence)}],
    }


class FakeAdapter:
    def __init__(self, records: list[dict] | None = None) -> None:
        self.records = copy.deepcopy(records if records is not None else [canonical_record()])
        self.calls: list[dict] = []

    def fetch(self, **kwargs) -> list[dict]:
        self.calls.append(kwargs)
        return copy.deepcopy(self.records)


class FakeLLM(BaseLLM):
    def __init__(self, narrative: dict | None = None, content: str | None = None) -> None:
        self.narrative = copy.deepcopy(narrative if narrative is not None else valid_narrative())
        self.content = content
        self.messages: list[list] = []

    def complete(self, messages: list) -> dict:
        self.messages.append(copy.deepcopy(messages))
        content = self.content if self.content is not None else json.dumps(self.narrative)
        return {"content": content, "tool_calls": [], "model": "mock-model", "usage": {}}

    def stream(self, messages: list) -> Iterator[str]:
        yield self.complete(messages)["content"]

    def bind_tools(self, tools: list) -> "FakeLLM":
        return self


def make_config(
    *,
    adapter: FakeAdapter | None = None,
    llm: BaseLLM | None = None,
    caller_id: str = "caller-a",
    project_id: str = "project-1",
) -> dict:
    config = yaml.safe_load((ROOT / "config" / "config.yaml").read_text(encoding="utf-8"))
    config = copy.deepcopy(config)
    config["allowed_projects"] = {caller_id: [project_id]}
    adapter = adapter or FakeAdapter()
    config["service"] = Service(
        ServiceIdentity("CMN-C2-718", "ProjectProgressReportAgent", "cmn"),
        AdapterRegistry({"gitlab_issues_v1": adapter}),
    )
    config["llm"] = llm if llm is not None else FakeLLM()
    return config


def request_envelope(**overrides) -> str:
    envelope = {
        "project_id": "project-1",
        "period_start": "2026-07-01T00:00:00+09:00",
        "period_end": "2026-08-01T00:00:00+09:00",
        "source_set_id": "gitlab_issues_v1",
        "output_schema_id": "sprint_status_v1",
    }
    envelope.update(overrides)
    return json.dumps(envelope, ensure_ascii=False)


def framework_state(**overrides) -> dict:
    state = {
        "user_input": request_envelope(),
        "input_context": {},
        "session_id": "test-session",
        "correlation_id": "test-correlation",
        "trace_id": "test-trace",
        "thread_id": "test-thread",
        "caller_id": "caller-a",
        "caller_trust_level": "VERIFIED_EXTERNAL",
        "status": "success",
        "node_history": [],
        "error_log": [],
        "execution_time": {},
        "rejected": [],
        "source_records": [canonical_record()],
        "validated_input": {
            "project_id": "project-1",
            "period_start_utc": "2026-06-30T15:00:00Z",
            "period_end_utc": "2026-07-31T15:00:00Z",
            "source_set_id": "gitlab_issues_v1",
            "output_schema_id": "sprint_status_v1",
        },
        "metrics": {
            "close_rate": 1.0,
            "velocity": 0.5,
            "milestone_progress": [{"milestone_id": "milestone-1", "progress": 1.0}],
            "estimate_missing_count": 0,
        },
        "narrative": valid_narrative(),
        "unresolved": [],
        "formatted_output": {},
    }
    state.update(overrides)
    return state
