import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest
from pydantic import ValidationError

from fbe_flow.core.errors import Conflict, InvalidInput, NotFound
from fbe_flow.core.instance import single_instance
from fbe_flow.core.integrations import AdapterRegistry
from fbe_flow.core.models import NormalizedBatch, Order, Product, Supply
from fbe_flow.modules.connections import Connections
from fbe_flow.modules.operations import Operations
from fbe_flow.modules.records import KINDS, Records
from fbe_flow.modules.sellers import Sellers
from fbe_flow.modules.settings import Settings

from .conftest import FixtureAdapter, normalized_batch


def test_migration_is_idempotent_and_foreign_keys_always_enabled(db):
    seller = Sellers(db).create("Preserved")
    db.initialize()
    assert Sellers(db).get(seller["id"])["name"] == "Preserved"
    with db.connection() as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 6
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_newer_schema_is_rejected_without_downgrade(db):
    with db.connection() as conn:
        conn.execute("PRAGMA user_version = 99")
    with pytest.raises(RuntimeError, match="newer"):
        db.initialize()


def test_single_instance_and_lock_release(tmp_path):
    with single_instance(tmp_path), pytest.raises(RuntimeError, match="already"):
        with single_instance(tmp_path):
            pass
    with single_instance(tmp_path):
        pass


@pytest.mark.parametrize("kind", KINDS)
def test_identical_external_ids_are_isolated_and_upserts_keep_identity(db, setup, kind):
    first, second, a, b, _, _ = setup
    records = Records(db)
    records.apply(first, a, normalized_batch("First product"))
    records.apply(second, b, normalized_batch("Second product"))
    own = records.list(first, kind)
    other = records.list(second, kind)
    assert len(own) == len(other) == 1
    assert own[0]["id"] != other[0]["id"]
    with pytest.raises(NotFound):
        records.get(first, kind, other[0]["id"])
    records.apply(first, a, normalized_batch("Changed"))
    assert records.list(first, kind)[0]["id"] == own[0]["id"]
    assert len(records.list(first, kind)) == 1
    assert records.list(second, "products")[0]["title"] == "Second product"


@pytest.mark.parametrize("kind", KINDS)
def test_database_blocks_foreign_seller_connection_even_if_service_is_bypassed(db, setup, kind):
    first, second, a, _, _, _ = setup
    Records(db).apply(first, a, normalized_batch())
    with pytest.raises(sqlite3.IntegrityError), db.connection() as conn:
        conn.execute(f"UPDATE {kind} SET seller_id = ? WHERE seller_id = ?", (second, first))
    assert Records(db).list(second, kind) == []


def test_operations_foreign_connection_is_rejected_by_database(db, setup):
    first, second, a, _, registry, _ = setup
    job = Operations(db, registry).enqueue(first, a, "fetch", {})
    with pytest.raises(sqlite3.IntegrityError), db.connection() as conn:
        conn.execute("UPDATE operations SET seller_id = ? WHERE id = ?", (second, job["id"]))


@pytest.mark.parametrize(
    "model,kind,relation",
    [
        (Supply, "supplies", "warehouse_external_id"),
        (Order, "orders", "warehouse_external_id"),
        (Order, "orders", "supply_external_id"),
    ],
)
def test_relations_cannot_reference_another_seller_or_connection(db, setup, model, kind, relation):
    first, second, a, b, registry, _ = setup
    records = Records(db)
    records.apply(second, b, normalized_batch())
    other_connection = Connections(db, registry).create(
        first, "test-fixture", "Another account", {"account": "other"}
    )
    records.apply(first, other_connection["id"], normalized_batch())
    external_id = "supply" if relation == "supply_external_id" else "warehouse"
    item = model(external_id="foreign-reference", status="arbitrary", **{relation: external_id})
    with pytest.raises(sqlite3.IntegrityError):
        records.apply(first, a, NormalizedBatch(**{kind: (item,)}))
    assert records.list(first, kind)[0]["connection_id"] == other_connection["id"]


def test_whole_batch_rolls_back_when_relation_is_missing(db, setup):
    first, _, a, _, _, _ = setup
    batch = NormalizedBatch(
        products=(Product(external_id="new", title="Must roll back"),),
        orders=(Order(external_id="bad", status="open", warehouse_external_id="missing"),),
    )
    with pytest.raises(sqlite3.IntegrityError):
        Records(db).apply(first, a, batch)
    assert Records(db).list(first, "products") == []


def test_normalized_contract_rejects_seller_override_and_empty_identity():
    with pytest.raises(ValidationError):
        Product(external_id="a", title="a", seller_id="foreign")
    with pytest.raises(ValidationError):
        Product(external_id="  ", title="a")


def test_duplicate_id_batch_rolls_back(db, setup):
    first, _, a, _, _, _ = setup
    item = Product(external_id="same", title="First")
    with pytest.raises(InvalidInput):
        Records(db).apply(first, a, NormalizedBatch(products=(item, item)))
    assert Records(db).list(first, "products") == []


def test_settings_remain_seller_scoped(db, setup):
    first, second, *_ = setup
    settings = Settings(db)
    settings.set(first, "same-key", {"nested": [1, True, None]})
    settings.set(second, "same-key", "different")
    assert settings.list(first) == {"same-key": {"nested": [1, True, None]}}
    assert settings.list(second) == {"same-key": "different"}
    with pytest.raises(NotFound):
        settings.set("missing", "key", 1)


def test_adapter_registration_and_discovered_capabilities(db):
    assert AdapterRegistry().descriptors() == []
    with pytest.raises(ValueError):
        AdapterRegistry([FixtureAdapter(), FixtureAdapter()])
    seller = Sellers(db).create("Any seller")["id"]
    registry = AdapterRegistry([FixtureAdapter()])
    connections = Connections(db, registry)
    with pytest.raises(InvalidInput):
        connections.create(seller, "uninstalled", "A", {})
    connection = connections.create(
        seller, "test-fixture", "A", {"account": "A", "capability": "custom-action"}
    )
    assert connection["operations"][0]["key"] == "custom-action"
    with pytest.raises(InvalidInput):
        Operations(db, registry).enqueue(seller, connection["id"], "fetch", {})


def test_connection_validation_is_safe_and_does_not_persist_partial_data(db, setup):
    first, _, _, _, registry, _ = setup
    with pytest.raises(InvalidInput, match="Не удалось") as error:
        Connections(db, registry).create(first, "test-fixture", "Bad", {"describe_failure": True})
    assert "secret" not in str(error.value)
    assert len(Connections(db, registry).list(first)) == 1


def test_duplicate_connection_identity_conflicts_per_seller(db, setup):
    first, second, _, _, registry, _ = setup
    with pytest.raises(Conflict):
        Connections(db, registry).create(
            first, "test-fixture", "Duplicate", {"account": "account-a"}
        )
    Connections(db, registry).create(second, "test-fixture", "Allowed", {"account": "account-a"})


def test_record_kind_cannot_inject_sql(db, setup):
    first, *_ = setup
    with pytest.raises(InvalidInput):
        Records(db).list(first, "products; DROP TABLE sellers")
    assert len(Sellers(db).list()) == 2


def test_queue_success_is_scoped_and_deduplicated(db, setup):
    first, second, a, _, registry, adapter = setup
    operations = Operations(db, registry)
    job = operations.enqueue(first, a, "fetch", {})
    with pytest.raises(Conflict):
        operations.enqueue(first, a, "fetch", {})
    with pytest.raises(NotFound):
        operations.enqueue(second, a, "fetch", {})
    assert operations.run_once()
    finished = operations.get(first, job["id"])
    assert finished["status"] == "succeeded"
    assert finished["result"] == {"counts": dict.fromkeys(KINDS, 1), "data": {}}
    assert finished["started_at"] and finished["finished_at"]
    assert adapter.contexts[0].seller_id == first
    assert adapter.contexts[0].connection_id == a
    assert Records(db).list(second, "products") == []
    assert not operations.run_once()
    # Explicit repeats are allowed after completion and use idempotent normalized upserts.
    operations.enqueue(first, a, "fetch", {})
    operations.run_once()
    assert len(Records(db).list(first, "products")) == 1


def test_concurrent_claims_never_return_the_same_job(db, setup):
    first, second, a, b, registry, _ = setup
    operations = Operations(db, registry)
    one = operations.enqueue(first, a, "fetch", {})
    two = operations.enqueue(second, b, "fetch", {})
    with ThreadPoolExecutor(max_workers=2) as executor:
        claims = list(executor.map(lambda _: operations.claim(), range(2)))
    assert {job["id"] for job in claims} == {one["id"], two["id"]}
    assert operations.claim() is None


def test_execution_failure_does_not_store_external_error_text(db, setup, caplog):
    first, _, _, _, registry, _ = setup
    connection = Connections(db, registry).create(
        first, "test-fixture", "Failure", {"account": "fail", "execution_failure": True}
    )
    operations = Operations(db, registry)
    job = operations.enqueue(first, connection["id"], "fetch", {})
    operations.run_once()
    result = operations.get(first, job["id"])
    assert result["status"] == "failed"
    assert result["error_code"] == "execution_failed"
    assert result["result"] is None
    assert "secret-from-external-response" not in str(result) + caplog.text


def test_failed_batch_rolls_back_and_marks_operation_failed(db, setup):
    first, _, a, _, registry, _ = setup
    operations = Operations(db, registry)
    job = operations.enqueue(first, a, "fetch", {"broken_relation": True})
    operations.run_once()
    assert operations.get(first, job["id"])["status"] == "failed"
    assert all(Records(db).list(first, kind) == [] for kind in KINDS)


def test_restart_interrupts_running_without_replaying_and_preserves_queue(db, setup):
    first, second, a, b, registry, adapter = setup
    operations = Operations(db, registry)
    job = operations.enqueue(first, a, "fetch", {})
    operations.claim()
    queued = operations.enqueue(second, b, "fetch", {})
    operations.recover_interrupted()
    assert operations.get(first, job["id"])["status"] == "interrupted"
    assert operations.get(second, queued["id"])["status"] == "queued"
    assert adapter.contexts == []
    operations.run_once()
    assert [context.seller_id for context in adapter.contexts] == [second]
