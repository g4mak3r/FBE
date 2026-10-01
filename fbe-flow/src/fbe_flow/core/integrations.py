from collections.abc import Iterable
from typing import Protocol

from pydantic import JsonValue

from fbe_flow.core.errors import InvalidInput
from fbe_flow.core.models import AccountInfo, ConnectionContext, OperationResult


class IntegrationAdapter(Protocol):
    key: str
    label: str

    def describe(self, config: dict[str, JsonValue]) -> AccountInfo:
        """Validate non-secret configuration; discover the actual account's operations."""
        ...

    def execute(
        self, context: ConnectionContext, operation: str, payload: dict[str, JsonValue]
    ) -> OperationResult:
        """Use bounded I/O timeouts. Return an outcome and optional records; never mutate the DB."""
        ...


class AdapterRegistry:
    def __init__(self, adapters: Iterable[IntegrationAdapter] = ()):
        self._adapters: dict[str, IntegrationAdapter] = {}
        for adapter in adapters:
            if not adapter.key.strip() or adapter.key in self._adapters:
                raise ValueError("Adapter keys must be non-empty and unique")
            self._adapters[adapter.key] = adapter

    def get(self, key: str) -> IntegrationAdapter:
        try:
            return self._adapters[key]
        except KeyError as exc:
            raise InvalidInput("Адаптер не зарегистрирован") from exc

    def descriptors(self) -> list[dict[str, str]]:
        return [{"key": item.key, "label": item.label} for item in self._adapters.values()]
