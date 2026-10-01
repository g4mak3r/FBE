from threading import Event

import pytest
from fastapi.testclient import TestClient

from fbe_flow.app import create_app
from fbe_flow.config import AppConfig
from fbe_flow.core.database import Database
from fbe_flow.core.integrations import AdapterRegistry
from fbe_flow.core.models import (
    AccountInfo,
    NormalizedBatch,
    OperationResult,
    OperationSpec,
    Order,
    Product,
    Supply,
    Warehouse,
)
from fbe_flow.integrations.chz.adapter import ChzAdapter
from fbe_flow.modules.connections import Connections
from fbe_flow.modules.sellers import Sellers

from .chz_fixtures import FixtureSigner, MemoryVault, Provider


class FixtureAdapter:
    """A test double only. No sample/demo adapter is shipped to users."""

    key = "test-fixture"
    label = "Test fixture"

    def __init__(self):
        self.contexts = []
        self.called = Event()
        self.result = None

    def describe(self, config):
        if config.get("describe_failure"):
            raise RuntimeError("secret-from-external-response")
        return AccountInfo(
            external_account_id=config["account"],
            operations=(OperationSpec(key=config.get("capability", "fetch"), label="Fetch"),),
        )

    def execute(self, context, operation, payload):
        self.contexts.append(context)
        self.called.set()
        if context.config.get("execution_failure"):
            raise RuntimeError("secret-from-external-response")
        if self.result is not None:
            return self.result
        batch = normalized_batch(context.external_account_id)
        if payload.get("broken_relation"):
            batch = batch.model_copy(
                update={
                    "orders": (
                        Order(
                            external_id="broken",
                            status="source-state",
                            warehouse_external_id="missing",
                        ),
                    )
                }
            )
        return OperationResult(batch=batch)


def normalized_batch(title="Product"):
    return NormalizedBatch(
        warehouses=(Warehouse(external_id="warehouse", name="An account-defined warehouse"),),
        products=(
            Product(
                external_id="product",
                title=title,
                category={"external_id": "dynamic-category"},
                identifiers={"source-identifier": ["arbitrary-identifier"]},
            ),
        ),
        supplies=(
            Supply(
                external_id="supply",
                status="account-defined-state",
                warehouse_external_id="warehouse",
            ),
        ),
        orders=(
            Order(
                external_id="order",
                status="another-source-state",
                warehouse_external_id="warehouse",
                supply_external_id="supply",
            ),
        ),
    )


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "flow.sqlite3")
    database.initialize()
    return database


@pytest.fixture
def setup(db):
    adapter = FixtureAdapter()
    registry = AdapterRegistry([adapter])
    sellers = Sellers(db)
    first = sellers.create("First")
    second = sellers.create("Second")
    connections = Connections(db, registry)
    a = connections.create(first["id"], adapter.key, "First account", {"account": "account-a"})
    b = connections.create(second["id"], adapter.key, "Second account", {"account": "account-b"})
    return first["id"], second["id"], a["id"], b["id"], registry, adapter


@pytest.fixture
def client(tmp_path):
    app = create_app(AppConfig(tmp_path, worker_enabled=False), [FixtureAdapter()])
    with TestClient(app, base_url="http://localhost") as client:
        client.headers["X-FBE-Flow"] = "1"
        yield client


@pytest.fixture
def workspace(tmp_path):
    vault, signer, provider = MemoryVault(), FixtureSigner(), Provider()
    adapter = ChzAdapter(vault, signer, provider)
    app = create_app(
        AppConfig(tmp_path, worker_enabled=False), [adapter], vault=vault, signer=signer
    )
    with TestClient(app, base_url="http://localhost", headers={"X-FBE-Flow": "1"}) as client:
        seller = app.state.sellers.create("First seller")["id"]
        reference = vault.put(seller, {"true_token": "private-token"})
        connection = app.state.connections.create(
            seller,
            "chz",
            "First account",
            {"inn": provider.inn, "credential_ref": reference, "certificate": "A" * 40},
        )
        yield app.state, client, seller, connection, provider, vault, signer
