"""Composition point for installed external-system adapters."""

from fbe_flow.core.integrations import IntegrationAdapter
from fbe_flow.integrations.chz import ChzAdapter
from fbe_flow.integrations.kit import KitAdapter
from fbe_flow.integrations.ozon import OzonAdapter
from fbe_flow.integrations.wb import WbAdapter


def installed_adapters(vault, signer) -> tuple[IntegrationAdapter, ...]:
    return (ChzAdapter(vault, signer), WbAdapter(vault), OzonAdapter(vault), KitAdapter(vault))
