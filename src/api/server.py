"""Standalone HTTP adapter for ProjectProgressReportAgent."""

from __future__ import annotations

import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel

import yaml

from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from framework.secrets.base import SecretProvider
from framework.secrets.context import bound_secrets
from framework.utils.config_loader import load_config
from shared.secrets import factory as secrets_factory
from shared.services.llm.base_llm import BaseLLM
from src.graph.graph import ProjectProgressReportGraph
from src.services.service import AdapterRegistry, Service, ServiceIdentity

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CONFIG_PATH = _REPO_ROOT / "config" / "config.yaml"
_MANIFEST_PATH = _REPO_ROOT / "config" / "agent.yaml"

agent: ProjectProgressReportGraph | None = None
service: Service | None = None
_secrets_provider: SecretProvider | None = None


def wire_services(
    *,
    config: dict[str, Any] | None = None,
    llm: BaseLLM | None = None,
    adapters: AdapterRegistry | None = None,
    secrets_provider: SecretProvider | None = None,
) -> ProjectProgressReportGraph:
    """Wire manifest identity, injected services, secrets, and the graph."""

    global agent, service, _secrets_provider
    manifest = yaml.safe_load(_MANIFEST_PATH.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise RuntimeError("agent manifest is invalid")
    identity = ServiceIdentity(
        agent_id=str(manifest["id"]),
        name=str(manifest["name"]),
        namespace=str(manifest["namespace"]),
    )
    service = Service(identity=identity, adapters=adapters)
    runtime_config = dict(config if config is not None else load_config(str(_CONFIG_PATH)))
    runtime_config["llm"] = llm
    runtime_config["service"] = service
    _secrets_provider = secrets_provider or secrets_factory(
        namespace=identity.namespace,
        agent_name=identity.name,
    )
    graph = ProjectProgressReportGraph(config=runtime_config)
    graph.compile()
    graph.provision_secrets(_secrets_provider)
    agent = graph
    return graph


@asynccontextmanager
async def _lifespan(app_instance: FastAPI) -> AsyncIterator[None]:
    graph = wire_services()
    app_instance.state.agent = graph
    app_instance.state.service = service
    yield


app = FastAPI(title="ProjectProgressReportAgent", lifespan=_lifespan)


class InvokeRequest(BaseModel):
    input: str
    session_id: str = ""


def _bearer_matches(supplied: str, expected: str) -> bool:
    return secrets.compare_digest(supplied.encode(), f"Bearer {expected}".encode())


def _resolve_standalone_trust(
    current: TrustLevel,
    authorization: str,
    invoke_auth_token: str | None,
    internal_runner_token: str | None,
) -> TrustLevel:
    """Authenticate standalone callers without allowing external-token elevation.

    STG_INTERNAL_RUNNER_TOKEN is a distinct, CI-generated deployment credential.
    It is considered only for an anonymous caller and maps exactly to INTERNAL;
    INVOKE_AUTH_TOKEN remains VERIFIED_EXTERNAL. Middleware-established trust is
    never changed.
    """
    if current is not TrustLevel.ANONYMOUS:
        return current
    if internal_runner_token and _bearer_matches(authorization, internal_runner_token):
        return TrustLevel.INTERNAL
    if invoke_auth_token and _bearer_matches(authorization, invoke_auth_token):
        return TrustLevel.VERIFIED_EXTERNAL
    if internal_runner_token or invoke_auth_token:
        raise HTTPException(status_code=401, detail="Token is invalid or expired.")
    return TrustLevel.ANONYMOUS


@app.post("/invoke")
async def invoke(req: InvokeRequest, request: Request) -> Any:
    graph = getattr(request.app.state, "agent", None)
    if graph is None or _secrets_provider is None:
        raise HTTPException(status_code=503, detail="Agent services are not ready.")
    trust = _resolve_standalone_trust(
        getattr(request.state, "trust_level", TrustLevel.ANONYMOUS),
        request.headers.get("authorization", ""),
        _secrets_provider.get("INVOKE_AUTH_TOKEN"),
        _secrets_provider.get("STG_INTERNAL_RUNNER_TOKEN"),
    )
    with bound_secrets(_secrets_provider):
        ctx = InvocationContext(
            session_id=req.session_id or str(uuid4()),
            caller_trust_level=trust,
            caller_id=getattr(request.state, "caller_id", ""),
        )
        return graph.invoke(req.input, ctx=ctx)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "agent": "ProjectProgressReportAgent"}
