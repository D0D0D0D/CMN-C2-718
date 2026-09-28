"""AGENTIC STAR Marketplace entry adapter for CMN-C2-718."""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any, cast
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen

from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from framework.secrets.base import SecretProvider
from framework.secrets.context import current_secrets
from shared.services.llm.base_llm import BaseLLM

try:
    from framework.utils.config_loader import load_agent_config
except ImportError:  # AgentCore 1.0.0 / 1.0.1 compatibility
    from framework.utils.config_loader import load_config

    def load_agent_config(agent_dir: Path) -> dict[str, Any]:
        config_path = agent_dir / "config" / "config.yaml"
        return cast(dict[str, Any], load_config(str(config_path))) if config_path.exists() else {}


try:
    from shared.bootstrap import run_agent_marketplace
except ModuleNotFoundError as exc:  # AgentCore 1.0.0 / 1.0.1 compatibility
    if exc.name != "shared.bootstrap":
        raise
    _BOOTSTRAP_IMPORT_ERROR: ModuleNotFoundError | None = exc

    def run_agent_marketplace(*args: Any, **kwargs: Any) -> None:
        del args, kwargs
        raise RuntimeError(
            "The installed AgentCore wheel does not provide Marketplace bootstrap support"
        ) from _BOOTSTRAP_IMPORT_ERROR
else:
    _BOOTSTRAP_IMPORT_ERROR = None


try:
    from shared.secrets.env_provider import EnvProvider
except ModuleNotFoundError as exc:  # AgentCore 1.0.0 / 1.0.1 compatibility
    if exc.name != "shared.secrets.env_provider":
        raise

    class EnvProvider(SecretProvider):  # type: ignore[no-redef]
        """Minimal process-environment provider for pre-1.0.2 wheels."""

        def __init__(self, namespace: str = "", agent_name: str = "") -> None:
            self._namespace = namespace
            self._agent_name = agent_name

        def get(self, key: str, default: str | None = None) -> str | None:
            return os.environ.get(key, default)


from src.graph.graph import ProjectProgressReportGraph

_NAMESPACE = "cmn"
_AGENT_NAME = "CMN-C2-718"
_PROVIDER_AGENT_NAME = "cmn_c2_718"

_AZURE_OPENAI_SECRET_KEYS = (
    "AZURE_OPENAI_API_KEY",
    "AZURE_OPENAI_ENDPOINT",
    "AZURE_OPENAI_DEPLOYMENT",
)
_SCOPE_ENV = {
    "caller_id": "STG_DEFAULT_CALLER_ID",
    "project_id": "STG_DEFAULT_PROJECT_ID",
    "source_set_id": "STG_DEFAULT_SOURCE_SET_ID",
    "output_schema_id": "STG_DEFAULT_OUTPUT_SCHEMA_ID",
}
_PERIOD_KEYS = frozenset(("period_start", "period_end"))
_BASE_ALLOWED_ENV_KEYS = frozenset((*_AZURE_OPENAI_SECRET_KEYS, *_SCOPE_ENV.values()))


class _AllowlistedEnvironmentProvider(EnvProvider):
    """Expose only deployment scope and the selected runtime dependencies."""

    def __init__(self, extra_keys: frozenset[str] = frozenset()) -> None:
        super().__init__(namespace=_NAMESPACE, agent_name=_PROVIDER_AGENT_NAME)
        self._allowed_keys = _BASE_ALLOWED_ENV_KEYS | extra_keys

    def get(self, key: str, default: str | None = None) -> str | None:
        if key not in self._allowed_keys:
            return default
        value = super().get(key)
        return value if value is not None else default


def _require_clean(provider: SecretProvider, key: str) -> str:
    """Resolve one deployment value without trimming or otherwise repairing it."""

    value = cast(str, provider.require(key))
    if not value or any(character.isspace() for character in value):
        raise ValueError(f"Marketplace environment variable {key!r} must be a clean non-empty string")
    return value


def _require_azure_endpoint(provider: SecretProvider) -> str:
    """Reject ``AZURE_OPENAI_ENDPOINT`` values that are not a bare Azure resource endpoint.

    ``AzureOpenAIClient`` appends its own ``/openai/v1/`` path, so anything
    beyond a scheme and host silently produces a broken request URL, and the
    failure surfaces inside ``complete()``. A node that swallows that exception
    degrades quietly while the invocation still reports ``status: success``,
    which is why this has to be a startup check. The value is reported here,
    never repaired.
    """

    endpoint = _require_clean(provider, "AZURE_OPENAI_ENDPOINT")
    parts = urlsplit(endpoint)
    try:
        port = parts.port
    except ValueError:
        port = -1
    if (
        parts.scheme not in ("http", "https")
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or port == -1
        or parts.path.strip("/")
        or parts.query
        or parts.fragment
        or any(character.isspace() for character in endpoint)
    ):
        raise ValueError(
            "Marketplace environment variable 'AZURE_OPENAI_ENDPOINT' must be a bare Azure "
            "resource endpoint -- scheme and host only, with no path (such as "
            "'/openai'), query or fragment"
        )
    return endpoint


def _deployment_scope(provider: SecretProvider, config: dict[str, Any]) -> dict[str, str]:
    """Resolve and validate the complete adapter-owned authorization scope."""

    scope = {key: _require_clean(provider, env_name) for key, env_name in _SCOPE_ENV.items()}
    source_sets = config.get("source_sets")
    output_schemas = config.get("output_schemas")
    if not isinstance(source_sets, dict) or scope["source_set_id"] not in source_sets:
        raise ValueError("STG_DEFAULT_SOURCE_SET_ID must name a configured source set")
    if not isinstance(output_schemas, dict) or scope["output_schema_id"] not in output_schemas:
        raise ValueError("STG_DEFAULT_OUTPUT_SCHEMA_ID must name a configured output schema")
    return scope


def _selected_source_secret(config: dict[str, Any], source_set_id: str) -> str:
    source_sets = config.get("source_sets")
    source = source_sets.get(source_set_id) if isinstance(source_sets, dict) else None
    secret_name = source.get("secret_name") if isinstance(source, dict) else None
    if not isinstance(secret_name, str) or not secret_name or any(character.isspace() for character in secret_name):
        raise ValueError("The selected Marketplace source must declare a clean secret_name")
    return secret_name


class _AzureOpenAIClientCompat(BaseLLM):
    """Azure chat client used when AgentCore 1.0.1 lacks the optional client."""

    def __init__(self, config: dict[str, str]) -> None:
        self._config = dict(config)

    def complete(self, messages: list[Any]) -> dict[str, Any]:
        endpoint = self._config["azure_endpoint"].rstrip("/")
        deployment = quote(self._config["azure_deployment"], safe="")
        url = f"{endpoint}/openai/deployments/{deployment}/chat/completions?api-version=2024-10-21"
        request = Request(
            url,
            data=json.dumps({"messages": messages}).encode("utf-8"),
            headers={"api-key": self._config["api_key"], "content-type": "application/json"},
            method="POST",
        )
        with urlopen(request, timeout=30) as response:  # noqa: S310 - operator-owned Azure endpoint
            payload = json.loads(response.read().decode("utf-8"))
        choices = payload.get("choices", []) if isinstance(payload, dict) else []
        message = choices[0].get("message", {}) if choices else {}
        return {
            "content": message.get("content", "") if isinstance(message, dict) else "",
            "tool_calls": message.get("tool_calls", []) if isinstance(message, dict) else [],
            "model": payload.get("model", self._config["azure_deployment"]),
            "usage": payload.get("usage", {}),
        }

    def stream(self, messages: list[Any]) -> Iterator[str]:
        content = self.complete(messages).get("content", "")
        if content:
            yield str(content)

    def bind_tools(self, tools: list[Any]) -> "_AzureOpenAIClientCompat":
        if tools:
            raise NotImplementedError("Azure compatibility client does not support tool binding")
        return self


class _AzureMarketplaceLLM(BaseLLM):
    """Resolve Azure values and construct a fresh client for every operation."""

    @staticmethod
    def _new_client() -> BaseLLM:
        provider = current_secrets()
        values = {key: _require_clean(provider, key) for key in _AZURE_OPENAI_SECRET_KEYS}

        # Azure is the sole optional, invocation-time import. Framework,
        # security, graph, and state imports remain eager for worker threads.
        try:
            from shared.services.llm.azure_openai_client import AzureOpenAIClient
        except ModuleNotFoundError as exc:  # AgentCore 1.0.0 / 1.0.1 compatibility
            if exc.name != "shared.services.llm.azure_openai_client":
                raise
            AzureOpenAIClient = _AzureOpenAIClientCompat

        return cast(
            BaseLLM,
            AzureOpenAIClient(
                {
                    "api_key": values["AZURE_OPENAI_API_KEY"],
                    "azure_endpoint": values["AZURE_OPENAI_ENDPOINT"],
                    "azure_deployment": values["AZURE_OPENAI_DEPLOYMENT"],
                }
            ),
        )

    def complete(self, messages: list[Any]) -> dict[str, Any]:
        return cast(dict[str, Any], self._new_client().complete(messages))

    def stream(self, messages: list[Any]) -> Iterator[str]:
        yield from self._new_client().stream(messages)

    def bind_tools(self, tools: list[Any]) -> BaseLLM:
        return self._new_client().bind_tools(tools)


def _marketplace_user_input(user_input: str, scope: dict[str, str]) -> str:
    """Build the exact read-only report envelope from period plus deployment scope."""

    if not isinstance(user_input, str) or not user_input.strip():
        raise ValueError("Marketplace input must be a non-empty JSON object")
    try:
        supplied = json.loads(user_input)
    except json.JSONDecodeError as exc:
        raise ValueError("Marketplace input must be a JSON object") from exc
    if not isinstance(supplied, dict) or set(supplied) != _PERIOD_KEYS:
        raise ValueError("Marketplace input must contain exactly period_start and period_end")
    if any(not isinstance(supplied[key], str) or not supplied[key] for key in _PERIOD_KEYS):
        raise ValueError("Marketplace period values must be non-empty strings")
    packet = {
        "project_id": scope["project_id"],
        "period_start": supplied["period_start"],
        "period_end": supplied["period_end"],
        "source_set_id": scope["source_set_id"],
        "output_schema_id": scope["output_schema_id"],
    }
    return json.dumps(packet, ensure_ascii=False, sort_keys=True)


class MarketplaceGraph(ProjectProgressReportGraph):
    """Marketplace-only deployment scope, read posture, and Azure wiring."""

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        source = dict(config if config is not None else load_agent_config(Path(__file__).resolve().parent))
        scope = _deployment_scope(_AllowlistedEnvironmentProvider(), source)
        source_sets = source["source_sets"]
        output_schemas = source["output_schemas"]
        marketplace_config = {
            **source,
            "source_sets": {scope["source_set_id"]: source_sets[scope["source_set_id"]]},
            "output_schemas": {scope["output_schema_id"]: output_schemas[scope["output_schema_id"]]},
            "allowed_projects": {scope["caller_id"]: [scope["project_id"]]},
            "llm": _AzureMarketplaceLLM(),
            "memory_enabled": False,
            "hitl": {"enabled": False},
        }
        super().__init__(config=marketplace_config)

    def invoke(
        self,
        user_input: str,
        session_id: str = "",
        ctx: InvocationContext | None = None,
        input_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        # conversation_history is runner-owned free text outside this AG's S-2
        # scan. Discard it and construct every accepted field independently.
        del input_context
        scope = _deployment_scope(_AllowlistedEnvironmentProvider(), self.config)
        base_ctx = ctx if ctx is not None else InvocationContext(session_id=session_id or "")
        marketplace_ctx = replace(
            base_ctx,
            caller_id=scope["caller_id"],
            caller_trust_level=TrustLevel.VERIFIED_EXTERNAL,
            hitl_allowed=False,
        )
        return cast(
            dict[str, Any],
            super().invoke(
                _marketplace_user_input(user_input, scope),
                session_id=session_id,
                ctx=marketplace_ctx,
                input_context={},
            ),
        )

    def provision_secrets(self, provider: SecretProvider) -> None:
        # Ignore broader/dotenv-capable runner providers and validate every
        # selected dependency before the Marketplace process starts serving.
        del provider
        scope = _deployment_scope(_AllowlistedEnvironmentProvider(), self.config)
        source_secret = _selected_source_secret(self.config, scope["source_set_id"])
        environment_provider = _AllowlistedEnvironmentProvider(frozenset((source_secret,)))
        for key in (*_AZURE_OPENAI_SECRET_KEYS, source_secret):
            _require_clean(environment_provider, key)
        _require_azure_endpoint(environment_provider)
        super().provision_secrets(environment_provider)


def main() -> None:
    run_agent_marketplace(
        MarketplaceGraph,
        agent_name=_AGENT_NAME,
        namespace=_NAMESPACE,
        config=load_agent_config(Path(__file__).resolve().parent),
    )


if __name__ == "__main__":
    main()
