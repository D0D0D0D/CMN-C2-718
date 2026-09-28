"""Runtime service wiring and adapter resolution."""

from __future__ import annotations

from dataclasses import dataclass
from importlib import import_module
from typing import Any


@dataclass(frozen=True)
class ServiceIdentity:
    """Non-secret manifest identity passed to runtime services."""

    agent_id: str
    name: str
    namespace: str


class AdapterRegistry:
    """Resolve configured read-only adapters, with a fake-injection seam."""

    def __init__(self, adapters: dict[str, Any] | None = None) -> None:
        self._adapters = dict(adapters or {})

    def resolve(self, source_set_id: str, source_config: dict[str, Any]) -> Any:
        if source_set_id in self._adapters:
            return self._adapters[source_set_id]
        class_path = source_config.get("adapter")
        if not isinstance(class_path, str) or "." not in class_path:
            raise ValueError("E_ADAPTER_CONTRACT: adapter path is missing or invalid")
        module_name, class_name = class_path.rsplit(".", 1)
        adapter_class = getattr(import_module(module_name), class_name)
        adapter = adapter_class()
        if not callable(getattr(adapter, "fetch", None)):
            raise ValueError("E_ADAPTER_CONTRACT: adapter does not implement fetch()")
        return adapter


class Service:
    """Immutable-per-startup service bundle shared by graph nodes."""

    def __init__(self, identity: ServiceIdentity, adapters: AdapterRegistry | None = None) -> None:
        self.identity = identity
        self.adapters = adapters or AdapterRegistry()
