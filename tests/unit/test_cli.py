"""Unit tests for the AGENTIC STAR Marketplace entry adapter."""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

import cli
from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from framework.secrets.base import MissingSecret, NullProvider
from framework.secrets.context import bound_secrets
from src.graph.graph import ProjectProgressReportGraph
from tests.helpers import make_config, valid_narrative

ROOT = Path(__file__).parents[2]


@pytest.fixture(autouse=True)
def marketplace_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    values = {
        "STG_DEFAULT_CALLER_ID": "marketplace-caller",
        "STG_DEFAULT_PROJECT_ID": "project-a",
        "STG_DEFAULT_SOURCE_SET_ID": "gitlab_issues_v1",
        "STG_DEFAULT_OUTPUT_SCHEMA_ID": "sprint_status_v1",
        "AZURE_OPENAI_API_KEY": "azure-key-a",
        "AZURE_OPENAI_ENDPOINT": "https://azure.example.invalid",
        "AZURE_OPENAI_DEPLOYMENT": "deployment-a",
        "GITLAB_READ_TOKEN": "gitlab-read-token",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)


@pytest.fixture
def marketplace_config() -> dict[str, Any]:
    return cast(dict[str, Any], yaml.safe_load((ROOT / "config" / "config.yaml").read_text(encoding="utf-8")))


def test_invoke_rebuilds_context_and_envelope_from_deployment(
    monkeypatch: pytest.MonkeyPatch, marketplace_config: dict[str, Any]
) -> None:
    captured: dict[str, Any] = {}

    def fake_invoke(
        self: ProjectProgressReportGraph,
        user_input: str,
        session_id: str = "",
        ctx: InvocationContext | None = None,
        input_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        del self
        captured.update(user_input=user_input, session_id=session_id, ctx=ctx, input_context=input_context)
        return {"status": "success"}

    monkeypatch.setattr(ProjectProgressReportGraph, "invoke", fake_invoke)
    graph = cli.MarketplaceGraph(config=marketplace_config)
    runner_ctx = InvocationContext(
        caller_id="attacker",
        caller_trust_level=TrustLevel.ANONYMOUS,
        hitl_allowed=True,
    )
    result = graph.invoke(
        '{"period_start":"2026-07-01T00:00:00+09:00","period_end":"2026-08-01T00:00:00+09:00"}',
        session_id="market-session",
        ctx=runner_ctx,
        input_context={
            "conversation_history": ["outside S-2"],
            "project_id": "attacker-project",
            "source_set_id": "spreadsheet_progress_v1",
        },
    )

    assert result == {"status": "success"}
    assert captured["input_context"] == {}
    assert json.loads(captured["user_input"]) == {
        "project_id": "project-a",
        "period_start": "2026-07-01T00:00:00+09:00",
        "period_end": "2026-08-01T00:00:00+09:00",
        "source_set_id": "gitlab_issues_v1",
        "output_schema_id": "sprint_status_v1",
    }
    assert captured["ctx"].caller_id == "marketplace-caller"
    assert captured["ctx"].caller_trust_level is TrustLevel.VERIFIED_EXTERNAL
    assert captured["ctx"].hitl_allowed is False
    assert graph.config["allowed_projects"] == {"marketplace-caller": ["project-a"]}
    assert set(graph.config["source_sets"]) == {"gitlab_issues_v1"}


@pytest.mark.parametrize("env_name", cli._SCOPE_ENV.values())
@pytest.mark.parametrize("invalid", ["", "   ", "bad value", "bad\nvalue"])
def test_scope_rejects_empty_or_whitespace_values(
    monkeypatch: pytest.MonkeyPatch,
    marketplace_config: dict[str, Any],
    env_name: str,
    invalid: str,
) -> None:
    monkeypatch.setenv(env_name, invalid)
    with pytest.raises((ValueError, MissingSecret)) as error:
        cli.MarketplaceGraph(config=marketplace_config)
    assert env_name in str(error.value) or env_name.removeprefix("STG_DEFAULT_") in str(error.value)


@pytest.mark.parametrize("env_name", cli._SCOPE_ENV.values())
@pytest.mark.parametrize("invalid", ["", "   "])
def test_require_clean_rejects_every_empty_or_blank_scope_value(
    monkeypatch: pytest.MonkeyPatch, env_name: str, invalid: str
) -> None:
    monkeypatch.setenv(env_name, invalid)
    with pytest.raises(ValueError, match=env_name):
        cli._require_clean(cli._AllowlistedEnvironmentProvider(), env_name)


@pytest.mark.parametrize("env_name", cli._SCOPE_ENV.values())
def test_scope_rejects_unset_values(
    monkeypatch: pytest.MonkeyPatch, marketplace_config: dict[str, Any], env_name: str
) -> None:
    monkeypatch.delenv(env_name)
    with pytest.raises(MissingSecret) as error:
        cli.MarketplaceGraph(config=marketplace_config)
    assert env_name in str(error.value)


def test_marketplace_input_accepts_period_only_and_rejects_scope_or_mode() -> None:
    scope = {
        "caller_id": "caller",
        "project_id": "project",
        "source_set_id": "gitlab_issues_v1",
        "output_schema_id": "sprint_status_v1",
    }
    valid = '{"period_start":"2026-07-01T00:00:00Z","period_end":"2026-08-01T00:00:00Z"}'
    assert json.loads(cli._marketplace_user_input(valid, scope))["project_id"] == "project"
    for extra in ("project_id", "source_set_id", "output_schema_id", "mode"):
        packet = json.loads(valid)
        packet[extra] = "write" if extra == "mode" else "attacker"
        with pytest.raises(ValueError, match="exactly period_start and period_end"):
            cli._marketplace_user_input(json.dumps(packet), scope)


@pytest.mark.parametrize("env_name", (*cli._AZURE_OPENAI_SECRET_KEYS, "GITLAB_READ_TOKEN"))
def test_provision_secrets_rejects_missing_or_blank_dependency_at_startup(
    monkeypatch: pytest.MonkeyPatch,
    marketplace_config: dict[str, Any],
    env_name: str,
) -> None:
    graph = cli.MarketplaceGraph(config=marketplace_config)
    graph.compile()
    monkeypatch.setenv(env_name, "   ")
    with pytest.raises((ValueError, MissingSecret)) as error:
        graph.provision_secrets(NullProvider())
    assert env_name in str(error.value)


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://example.cognitiveservices.azure.com/openai/responses?api-version=2025-04-01-preview",
        "https://example.services.ai.azure.example/openai",
        "https://example.services.ai.azure.example/v1",
        "https://example.services.ai.azure.example?api-version=2024-10-21",
        "example.services.ai.azure.example",
    ],
    ids=["api-path", "openai-path", "base-path", "query-only", "missing-scheme"],
)
def test_unusable_azure_endpoint_shape_fails_during_startup(
    monkeypatch: pytest.MonkeyPatch,
    marketplace_config: dict[str, Any],
    endpoint: str,
) -> None:
    """AzureOpenAIClient appends its own path, so anything but a bare endpoint breaks.

    The resulting error surfaces inside complete(); a node that swallows it
    degrades silently while the invocation still reports status: success, so the
    adapter has to reject the shape at startup instead.
    """

    graph = cli.MarketplaceGraph(config=marketplace_config)
    graph.compile()
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "gate-test-key")
    monkeypatch.setenv("AZURE_OPENAI_DEPLOYMENT", "gate-test-deployment")
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", endpoint)

    with pytest.raises(ValueError, match="bare"):
        graph.provision_secrets(NullProvider())


@pytest.mark.parametrize("env_name", (*cli._AZURE_OPENAI_SECRET_KEYS, "GITLAB_READ_TOKEN"))
def test_provision_secrets_rejects_unset_dependency_at_startup(
    monkeypatch: pytest.MonkeyPatch,
    marketplace_config: dict[str, Any],
    env_name: str,
) -> None:
    graph = cli.MarketplaceGraph(config=marketplace_config)
    graph.compile()
    monkeypatch.delenv(env_name)
    with pytest.raises(MissingSecret, match=env_name):
        graph.provision_secrets(NullProvider())


def test_allowlisted_provider_hides_undeclared_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("UNRELATED_SECRET", "must-not-leak")
    assert cli._AllowlistedEnvironmentProvider().get("UNRELATED_SECRET") is None


def test_azure_llm_resolves_values_and_constructs_a_fresh_client_per_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    constructed: list[dict[str, str]] = []

    class RecordingAzureClient:
        def __init__(self, config: dict[str, str]) -> None:
            constructed.append(dict(config))
            self.config = config

        def complete(self, messages: list[Any]) -> dict[str, Any]:
            del messages
            return {"content": self.config["api_key"]}

    fake_module = types.ModuleType("shared.services.llm.azure_openai_client")
    fake_module.AzureOpenAIClient = RecordingAzureClient  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, fake_module.__name__, fake_module)

    llm = cli._AzureMarketplaceLLM()
    with bound_secrets(cli._AllowlistedEnvironmentProvider()):
        assert llm.complete([])["content"] == "azure-key-a"
        monkeypatch.setenv("AZURE_OPENAI_API_KEY", "azure-key-b")
        assert llm.complete([])["content"] == "azure-key-b"

    assert [config["api_key"] for config in constructed] == ["azure-key-a", "azure-key-b"]
    assert not hasattr(llm, "_client")
    assert not hasattr(llm, "_cached_client")


def test_marketplace_graph_invokes_real_workflow_with_deployment_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class NarrativeAzureClient:
        def __init__(self, config: dict[str, str]) -> None:
            assert config["api_key"] == "azure-key-a"

        def complete(self, messages: list[Any]) -> dict[str, Any]:
            assert messages
            return {
                "content": json.dumps(valid_narrative()),
                "tool_calls": [],
                "model": "azure-test",
                "usage": {},
            }

    fake_module = types.ModuleType("shared.services.llm.azure_openai_client")
    fake_module.AzureOpenAIClient = NarrativeAzureClient  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, fake_module.__name__, fake_module)

    graph = cli.MarketplaceGraph(config=make_config())
    graph.compile()
    graph.provision_secrets(NullProvider())
    provider = cli._AllowlistedEnvironmentProvider(frozenset(("GITLAB_READ_TOKEN",)))
    with bound_secrets(provider):
        result = graph.invoke('{"period_start":"2026-07-01T00:00:00+09:00","period_end":"2026-08-01T00:00:00+09:00"}')

    assert result["status"] == "success"
    assert result["output"]["report"]["report_meta"]["project_id"] == "project-a"


def test_main_uses_sdk_compatible_config_loader(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_runner(graph_class: type, **kwargs: Any) -> None:
        captured.update(graph_class=graph_class, **kwargs)

    monkeypatch.setattr(cli, "run_agent_marketplace", fake_runner)
    cli.main()
    assert captured["graph_class"] is cli.MarketplaceGraph
    assert captured["agent_name"] == "CMN-C2-718"
    assert captured["namespace"] == "cmn"
    assert "source_sets" in captured["config"]
