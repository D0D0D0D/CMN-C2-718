"""Startup wiring and provider-neutral LLM injection boundary."""

import inspect

from fastapi.testclient import TestClient

from shared.secrets.inmemory_provider import InMemoryProvider
from src.services.service import AdapterRegistry
from tests.helpers import FakeAdapter, FakeLLM, make_config


def test_testclient_runs_startup_and_manifest_identity_reaches_service() -> None:
    import src.api.server as server

    with TestClient(server.app) as client:
        assert client.get("/health").status_code == 200
        assert server.agent is not None
        assert server.service is not None
        assert server.service.identity.agent_id == "CMN-C2-718"
        assert server.service.identity.name == "ProjectProgressReportAgent"
        assert server.service.identity.namespace == "cmn"


def test_wire_services_threads_injected_llm_to_inner_narrative_node() -> None:
    import src.api.server as server

    llm = FakeLLM()
    graph = server.wire_services(
        config=make_config(),
        llm=llm,
        adapters=AdapterRegistry({"gitlab_issues_v1": FakeAdapter()}),
        secrets_provider=InMemoryProvider(),
    )
    inner = graph._nodes["main"].get_subgraph()
    assert inner._nodes["narrative_draft"]._llm is llm
    server_source = inspect.getsource(server)
    assert "AnthropicClient" not in server_source
    assert "OpenAIClient" not in server_source
    assert "BedrockClient" not in server_source


def test_standalone_trust_levels_do_not_cross_promote() -> None:
    import src.api.server as server
    from framework.schemas.trust_level import TrustLevel

    assert (
        server._resolve_standalone_trust(TrustLevel.ANONYMOUS, "Bearer external", "external", "runner")
        is TrustLevel.VERIFIED_EXTERNAL
    )
    assert (
        server._resolve_standalone_trust(TrustLevel.ANONYMOUS, "Bearer runner", "external", "runner")
        is TrustLevel.INTERNAL
    )
