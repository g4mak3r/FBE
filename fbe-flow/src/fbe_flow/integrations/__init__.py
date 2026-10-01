"""Composition point for future external-system adapters. Foundation ships none."""

from fbe_flow.core.integrations import IntegrationAdapter


def installed_adapters() -> tuple[IntegrationAdapter, ...]:
    return ()
