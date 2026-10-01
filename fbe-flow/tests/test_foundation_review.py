import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from fbe_flow.core.errors import Conflict, InvalidInput, NotFound
from fbe_flow.core.integrations import AdapterRegistry
from fbe_flow.core.models import NormalizedBatch, OperationResult, Order, Product
from fbe_flow.modules.connections import Connections
from fbe_flow.modules.operations import Operations
from fbe_flow.modules.records import KINDS, Records

from .conftest import FixtureAdapter, normalized_batch


def test_source_products_preserve_identifiers_without_guessing_cross_system_identity(db, setup):
    first, second, a, b, _, adapter = setup
    other_adapter = FixtureAdapter()
    other_adapter.key = "second-source"
    registry = AdapterRegistry([adapter, other_adapter])
    other_source = Connections(db, registry).create(
        first, other_adapter.key, "Other source", {"account": "other-source"}
    )["id"]
    records = Records(db)
    identifiers = {"gtin": ["00012345600012"], "seller-sku": ["same-sku"]}
    for seller, connection, title in (
        (first, a, "Source A"),
        (first, other_source, "Source B"),
        (second, b, "Other seller"),
    ):
        records.apply(
            seller,
            connection,
            NormalizedBatch(
                products=(
                    Product(
                        external_id="same-external-id",
                        title=title,
                        identifiers=identifiers,
                        category={"external_id": title},
                        attributes={"source": title},
                    ),
                )
            ),
        )
    own = records.list(first, "products")
    foreign = records.list(second, "products")[0]
    assert len(own) == 2
    assert len({item["id"] for item in [*own, foreign]}) == 3
    assert {item["attributes"]["source"] for item in own} == {"Source A", "Source B"}
    assert all(item["identifiers"] == identifiers for item in own)
    before = next(item for item in own if item["connection_id"] == a)
    records.apply(
        first,
        a,
        NormalizedBatch(
            products=(
                Product(
                    external_id="same-external-id",
                    title="Reimported without GTIN",
                    identifiers={"source-id": ["one", "two"]},
                ),
            )
        ),
    )
    assert records.get(first, "products", before["id"])["identifiers"] == {
        "source-id": ["one", "two"]
    }
    other = next(item for item in own if item["connection_id"] == other_source)
    assert records.get(first, "products", other["id"]) == other
    assert records.get(second, "products", foreign["id"]) == foreign


@pytest.mark.parametrize(
    "result,counts",
    [
        (OperationResult(data={"external_id": "created-by-source", "status": "accepted"}), None),
        (
            OperationResult(batch=normalized_batch(), data={"receipt": "source-receipt"}),
            dict.fromkeys(KINDS, 1),
        ),
        (OperationResult(), None),
        (OperationResult(batch=NormalizedBatch()), dict.fromkeys(KINDS, 0)),
    ],
)
def test_operation_result_supports_commands_sync_and_mixed_outcomes(db, setup, result, counts):
    first, second, a, _, registry, adapter = setup
    adapter.result = result
    operations = Operations(db, registry)
    job = operations.enqueue(first, a, "fetch", {})
    operations.run_once()
    finished = operations.get(first, job["id"])
    assert finished["status"] == "succeeded"
    assert finished["result"] == {"counts": counts, "data": result.data}
    assert finished["finished_at"]
    for kind in KINDS:
        assert len(Records(db).list(first, kind)) == (counts[kind] if counts is not None else 0)
        assert Records(db).list(second, kind) == []


def test_batch_failure_rolls_back_changes_and_command_receipt_together(db, setup):
    first, _, a, _, registry, adapter = setup
    records = Records(db)
    records.apply(first, a, normalized_batch("Before"))
    before = records.list(first, "products")[0]
    adapter.result = OperationResult(
        batch=NormalizedBatch(
            products=(Product(external_id="product", title="Must roll back"),),
            orders=(
                Order(external_id="bad", status="source-state", warehouse_external_id="missing"),
            ),
        ),
        data={"receipt": "must-not-be-persisted"},
    )
    operations = Operations(db, registry)
    job = operations.enqueue(first, a, "fetch", {})
    operations.run_once()
    assert records.get(first, "products", before["id"]) == before
    failed = operations.get(first, job["id"])
    assert failed["status"] == "failed"
    assert failed["result"] is None


@pytest.mark.parametrize(
    "result",
    [
        normalized_batch(),
        {"data": {"number": float("nan")}},
        {"data": {}, "seller_id": "foreign"},
    ],
)
def test_invalid_adapter_result_fails_without_persisting_data(db, setup, result):
    first, _, a, _, registry, adapter = setup
    adapter.result = result
    operations = Operations(db, registry)
    job = operations.enqueue(first, a, "fetch", {})
    operations.run_once()
    failed = operations.get(first, job["id"])
    assert failed["status"] == "failed"
    assert failed["result"] is None
    assert all(Records(db).list(first, kind) == [] for kind in KINDS)


def test_scopes_allow_distinct_targets_but_block_queued_and_running_duplicates(db, setup):
    first, second, a, b, registry, _ = setup
    operations = Operations(db, registry)
    one = operations.enqueue(first, a, "fetch", {"target": "A"}, scope_key="A")
    two = operations.enqueue(first, a, "fetch", {"target": "B"}, scope_key="B")
    for scope in ("A", "B"):
        with pytest.raises(Conflict):
            operations.enqueue(first, a, "fetch", {"different": "payload"}, scope_key=scope)
    assert operations.get(first, one["id"])["scope_key"] == "A"
    claimed = operations.claim()
    for job in (one, two):
        with pytest.raises(Conflict):
            operations.enqueue(first, a, "fetch", {}, scope_key=job["scope_key"])
    foreign = operations.enqueue(second, b, "fetch", {}, scope_key="A")
    with pytest.raises(NotFound):
        operations.get(first, foreign["id"])
    with pytest.raises(NotFound):
        operations.enqueue(second, a, "fetch", {}, scope_key="C")
    operations.recover_interrupted()
    operations.enqueue(first, a, "fetch", {}, scope_key=claimed["scope_key"])


def test_default_scope_keeps_connection_dedup_and_does_not_hash_payload(db, setup):
    first, _, a, _, registry, _ = setup
    operations = Operations(db, registry)
    job = operations.enqueue(first, a, "fetch", {"target": "A"})
    assert job["scope_key"] is None
    with pytest.raises(Conflict):
        operations.enqueue(first, a, "fetch", {"target": "B"})
    with pytest.raises(InvalidInput):
        operations.enqueue(first, a, "fetch", {}, scope_key="  ")
    operations.run_once()
    operations.enqueue(first, a, "fetch", {})


def test_scope_unique_index_handles_concurrent_enqueues(db, setup):
    first, _, a, _, registry, _ = setup
    operations = Operations(db, registry)

    def enqueue(scope):
        try:
            return operations.enqueue(first, a, "fetch", {}, scope_key=scope)["scope_key"]
        except Conflict:
            return "duplicate"

    with ThreadPoolExecutor(max_workers=3) as executor:
        outcomes = list(executor.map(enqueue, ("A", "A", "B")))
    assert sorted(outcomes) == ["A", "B", "duplicate"]
    assert len(operations.list(first)) == 2
    with pytest.raises(sqlite3.IntegrityError), db.connection() as conn:
        conn.execute("UPDATE operations SET scope_key = 'A' WHERE scope_key = 'B'")
