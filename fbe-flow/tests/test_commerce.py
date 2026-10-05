import copy
import json
from datetime import date

import pytest
from fastapi.testclient import TestClient

from fbe_flow.core.errors import Conflict, InvalidInput, NotFound
from fbe_flow.integrations.chz.http import RemoteError
from fbe_flow.modules.connections import connection_context

from .commerce_fixtures import add_link, application, connect, seed_all, uid
from .wb_fixtures import add_codes, drain
from .wb_fixtures import add_link as wb_link


@pytest.fixture
def commerce(tmp_path):
    app, chz, wb, ozon, kit, vault = application(tmp_path)
    with TestClient(app, base_url="http://localhost", headers={"X-FBE-Flow": "1"}) as client:
        seller, chz_connection, wb_connection, ozon_connection, kit_connection = seed_all(
            app.state, client, chz, wb
        )
        yield (
            app.state,
            client,
            seller,
            chz_connection,
            wb_connection,
            ozon_connection,
            kit_connection,
            chz,
            wb,
            ozon,
            kit,
            vault,
        )


def assignment(workspace, key="ozon", codes=None):
    state, _, seller, chz_connection, _, ozon, kit, chz, *_ = workspace
    connection = ozon if key == "ozon" else kit
    add_link(state, seller, chz_connection, connection)
    codes = codes or add_codes(state, seller, chz_connection, chz)
    order = state.commerce.records(seller, connection["id"], "orders")["items"][0]
    item_id = "10001" if key == "ozon" else uid(6001)
    count = next(v["quantity"] for v in order["attributes"]["items"] if v["item_id"] == item_id)
    action = state.commerce.prepare(
        seller,
        connection["id"],
        "codes",
        {
            "order_id": order["id"],
            "item_id": item_id,
            "code_ids": [v["id"] for v in codes[:count]],
        },
    )
    return action, order, codes


def send(workspace, action, *, reconcile=False):
    state, _, seller, *_ = workspace
    job = state.commerce.enqueue(seller, action["id"], reconcile=reconcile)
    drain(state)
    return state.commerce.action(seller, action["id"]), state.operations.get(seller, job["id"])


def test_sync_pagination_and_distinct_product_sku_and_delivery_chunks(commerce):
    state, _, seller, _, _, ozon, kit, _, _, ozon_api, kit_api, _ = commerce
    for connection in (ozon, kit):
        info = state.commerce.overview(seller, connection["id"])
        assert info["counts"]["products"] == 102
        assert info["snapshots"]["sync"]["value"]["coverage"] == "complete"
    products = state.commerce.records(seller, ozon["id"], "products", limit=200)["items"]
    first = next(v for v in products if v["external_id"] == "1")
    assert first["attributes"]["variant"] == "10001"
    assert first["identifiers"]["ozon_sku"] == ["10001"]
    assert state.commerce.overview(seller, ozon["id"])["counts"]["supplies"] == 1
    assert state.commerce.records(seller, ozon["id"], "returns")["items"][0]["id"] == 44
    order = state.commerce.records(seller, kit["id"], "orders")["items"][0]
    assert order["attributes"]["source"]["payment"]["status"] == "PAYMENT_PAID"
    assert [v["chunk_id"] for v in order["attributes"]["items"]] == [1, 2]
    kit_params = [c[2] for c in kit_api.calls if c[1] == "/v1/orders"]
    assert kit_params == [{"page": 1, "per_page": 100}]
    assert next(c[2] for c in kit_api.calls if c[1] == "/v1/warehouses")["status"] == [
        "ACTIVE",
        "ARCHIVED",
    ]
    assert len([c for c in ozon_api.calls if c[1] == "/v3/product/list"]) == 2


def test_full_suz_introduction_then_ozon_and_kit_use_exact_full_codes(commerce):
    from .test_chz_suz import issued_codes, prepare_application
    from .test_chz_workflows import send as send_chz

    state, client, seller, chz_connection, _, _, _, chz, _, ozon, kit, vault = commerce
    chz_workspace = (state, client, seller, chz_connection, chz, vault, state.signer)
    issued_codes(chz_workspace)
    accepted, _ = send_chz(chz_workspace, prepare_application(chz_workspace))
    assert send_chz(chz_workspace, accepted, poll=True)[0]["state"] == "succeeded"
    codes = state.marking.codes(seller, chz_connection["id"])["items"]
    document = state.marking.prepare(
        seller,
        chz_connection["id"],
        "true",
        {
            "type": "LP_INTRODUCE_GOODS",
            "product_group": chz.group,
            "document": {
                "participant_inn": chz.inn,
                "producer_inn": chz.inn,
                "owner_inn": chz.inn,
                "production_type": "OWN_PRODUCTION",
                "production_date": date.today().isoformat(),
                "products": [{"uit_code": v["code"]} for v in codes],
            },
        },
    )
    accepted, _ = send_chz(chz_workspace, document)
    assert send_chz(chz_workspace, accepted, poll=True)[0]["state"] == "succeeded"
    action, _, _ = assignment(commerce, codes=codes)
    ozon.passed = True
    final, job = send(commerce, action)
    assert final["state"] == "confirmed" and job["status"] == "succeeded"
    expected = action["body"]["sgtins"]
    wire = next(c[3] for c in ozon.calls if c[1].endswith("exemplar/set"))
    assert [v["marks"][0]["mark"] for v in wire["products"][0]["exemplars"]] == expected
    assert all("\x1d" in v for v in expected)
    store_action, _, _ = assignment(commerce, "kit", codes=codes[2:])
    assert send(commerce, store_action)[0]["state"] == "awaiting_manual"
    kit.order["delivery_chunks"][0]["items"][0]["truthful_label"] = store_action["body"]["sgtins"][
        0
    ]
    assert send(commerce, store_action, reconcile=True)[0]["state"] == "confirmed"
    assert not any(c[0] == "POST" for c in kit.calls)


def test_ozon_ack_is_pending_then_read_confirms_without_resending(commerce):
    action, _, _ = assignment(commerce)
    state, _, seller, _, _, _, _, _, _, ozon, *_ = commerce
    other = copy.deepcopy(ozon.exemplars["products"][1])
    final, job = send(commerce, action)
    assert final["state"] == "pending" and final["acknowledged"]
    assert final["result"]["receipt"] == {} and job["status"] == "succeeded"
    assert ozon.exemplars["products"][1] == other
    assert not state.operations.run_once()
    ozon.passed = True
    assert send(commerce, final, reconcile=True)[0]["state"] == "confirmed"
    assert len([c for c in ozon.calls if c[1].endswith("exemplar/set")]) == 1
    assert (
        len(
            state.commerce.available_codes(
                seller, commerce[5]["id"], action["body"]["order_id"], "10001"
            )["items"]
        )
        == 1
    )


@pytest.mark.parametrize("failure", ["timeout", "invalid_json", 409, 500])
def test_ambiguous_ozon_write_retains_reservation_and_never_auto_repeats(commerce, failure):
    action, _, _ = assignment(commerce)
    state, _, seller, _, _, _, _, _, _, ozon, *_ = commerce
    ozon.failure = failure
    final, job = send(commerce, action)
    assert final["state"] == "unknown" and not final["acknowledged"]
    assert job["status"] == "failed"
    assert not state.operations.run_once()
    with pytest.raises(Conflict):
        state.commerce.cancel(seller, action["id"])
    ozon.failure = None
    ozon.passed = True
    final, _ = send(commerce, final, reconcile=True)
    assert final["state"] == ("confirmed" if isinstance(failure, str) else "unknown")
    assert len([c for c in ozon.calls if c[1].endswith("exemplar/set")]) == 1


@pytest.mark.parametrize("failure", [400, 401, 403, 404, 422, 429])
def test_definite_rejection_can_release_codes_without_external_delete(commerce, failure):
    action, _, _ = assignment(commerce)
    state, _, seller, _, _, _, _, _, _, ozon, *_ = commerce
    ozon.failure = failure
    final, _ = send(commerce, action)
    assert final["state"] == "rejected" and final["result"]["definite_rejection"]
    assert state.commerce.cancel(seller, action["id"])["state"] == "cancelled"
    assert (
        state.commerce.available_codes(
            seller, commerce[5]["id"], action["body"]["order_id"], "10001"
        )["total"]
        == 3
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("status", "WRITTEN_OFF"),
        ("ownerInn", "999999999999"),
        ("gtin", "00000000000002"),
        ("productGroup", "foreign"),
        ("packageType", "GROUP"),
    ],
)
def test_fresh_chz_gate_blocks_ozon_write_and_allows_safe_draft_cancel(commerce, field, value):
    action, _, _ = assignment(commerce)
    state, _, seller, _, _, _, _, chz, _, ozon, *_ = commerce
    chz.cises[action["body"]["cis"][0]]["cisInfo"][field] = value
    final, job = send(commerce, action)
    assert final["state"] == "rejected" and final["result"]["before_send"]
    assert job["status"] == "failed" and not any(c[1].endswith("exemplar/set") for c in ozon.calls)
    assert state.commerce.cancel(seller, action["id"])["state"] == "cancelled"


def test_ozon_changed_exemplar_blocks_overwrite(commerce):
    action, _, _ = assignment(commerce)
    ozon = commerce[9]
    ozon.exemplars["products"][1]["exemplars"][0]["gtd"] = "changed"
    final, _ = send(commerce, action)
    assert final["state"] == "rejected" and final["result"]["before_send"]
    assert not any(c[1].endswith("exemplar/set") for c in ozon.calls)


def test_ozon_lost_followup_keeps_durable_ack_receipt(commerce):
    action, _, _ = assignment(commerce)
    ozon = commerce[9]
    original = ozon.request

    def request(method, url, **kwargs):
        value = original(method, url, **kwargs)
        if url.endswith("exemplar/set"):
            ozon.read_failure = True
        return value

    ozon.request = request
    final, job = send(commerce, action)
    assert final["state"] == "accepted" and final["acknowledged"]
    assert final["result"]["receipt"] == {} and job["status"] == "failed"
    ozon.read_failure, ozon.passed = False, True
    assert send(commerce, final, reconcile=True)[0]["state"] == "confirmed"
    assert len([c for c in ozon.calls if c[1].endswith("exemplar/set")]) == 1


def test_ozon_ship_requires_all_km_and_fresh_chz_status(commerce):
    action, order, _ = assignment(commerce)
    state, _, seller, _, _, connection, _, chz, _, ozon, *_ = commerce
    with pytest.raises(Conflict):
        state.commerce.prepare(seller, connection["id"], "ship", {"order_id": order["id"]})
    ozon.passed = True
    send(commerce, action)
    ship = state.commerce.prepare(seller, connection["id"], "ship", {"order_id": order["id"]})
    chz.cises[action["body"]["cis"][0]]["cisInfo"]["status"] = "WRITTEN_OFF"
    assert send(commerce, ship)[0]["state"] == "rejected"
    assert not any(c[1].endswith("/ship") for c in ozon.calls)


def test_ozon_ship_is_one_package_and_confirms_by_order_read(commerce):
    action, order, _ = assignment(commerce)
    state, _, seller, _, _, connection, _, _, _, ozon, *_ = commerce
    ozon.passed = True
    send(commerce, action)
    ship = state.commerce.prepare(seller, connection["id"], "ship", {"order_id": order["id"]})
    final, _ = send(commerce, ship)
    assert final["state"] == "confirmed"
    assert final["result"]["source"]["status"] == "awaiting_deliver"
    assert ship["body"]["wire"]["packages"] == [
        {"products": [{"product_id": 10001, "quantity": 2}, {"product_id": 10002, "quantity": 1}]}
    ]


@pytest.mark.parametrize("first", ["wb", "ozon", "kit"])
def test_same_physical_code_cannot_be_assigned_across_channels(commerce, first):
    state, _, seller, chz_connection, wb, ozon, kit, chz, *_ = commerce
    codes = add_codes(state, seller, chz_connection, chz)
    wb_link(state, seller, chz_connection, wb)
    for connection in (ozon, kit):
        add_link(state, seller, chz_connection, connection)
    wb_order = next(
        v
        for v in state.fulfillment.records(seller, wb["id"], "orders")["items"]
        if v["external_id"] == "1001"
    )
    ozon_order = state.commerce.records(seller, ozon["id"], "orders")["items"][0]
    kit_order = state.commerce.records(seller, kit["id"], "orders")["items"][0]
    requests = {
        "wb": lambda: state.fulfillment.prepare(
            seller,
            wb["id"],
            "sgtin",
            {"order_id": wb_order["id"], "code_ids": [codes[0]["id"], codes[1]["id"]]},
        ),
        "ozon": lambda: state.commerce.prepare(
            seller,
            ozon["id"],
            "codes",
            {
                "order_id": ozon_order["id"],
                "item_id": "10001",
                "code_ids": [codes[0]["id"], codes[1]["id"]],
            },
        ),
        "kit": lambda: state.commerce.prepare(
            seller,
            kit["id"],
            "codes",
            {"order_id": kit_order["id"], "item_id": uid(6001), "code_ids": [codes[0]["id"]]},
        ),
    }
    action = requests[first]()
    for key in requests.keys() - {first}:
        with pytest.raises(Conflict):
            requests[key]()
    if first == "wb":
        state.fulfillment.cancel(seller, action["id"])
    else:
        state.commerce.cancel(seller, action["id"])
    assert requests["kit" if first != "kit" else "ozon"]()["state"] == "draft"


def test_kit_manual_codes_then_confirm_and_own_delivery_never_write_receipts(commerce):
    action, order, _ = assignment(commerce, "kit")
    state, _, seller, _, _, _, connection, _, _, _, kit, _ = commerce
    assert send(commerce, action)[0]["state"] == "awaiting_manual"
    with pytest.raises(Conflict):
        state.commerce.prepare(seller, connection["id"], "confirm", {"order_id": order["id"]})
    kit.order["delivery_chunks"][0]["items"][0]["truthful_label"] = action["body"]["sgtins"][0]
    assert send(commerce, action, reconcile=True)[0]["state"] == "confirmed"
    confirm = state.commerce.prepare(seller, connection["id"], "confirm", {"order_id": order["id"]})
    assert send(commerce, confirm)[0]["state"] == "confirmed"
    complete = state.commerce.prepare(
        seller, connection["id"], "complete", {"order_id": order["id"]}
    )
    assert send(commerce, complete)[0]["state"] == "confirmed"
    assert [(c[1], c[3]) for c in kit.calls if c[0] == "POST"] == [
        (f"/v1/orders/{uid(7000)}/confirm", None),
        (f"/v1/orders/{uid(7000)}/delivery/complete", None),
    ]


def test_kit_multi_unit_truthful_label_string_is_never_guessed(commerce):
    state, _, seller, _, _, _, connection, _, _, _, kit, _ = commerce
    kit.order["delivery_chunks"][0]["items"][0]["quantity"] = 2
    state.commerce.start_sync(seller, connection["id"])
    drain(state)
    action, _, codes = assignment(commerce, "kit")
    assert len(action["body"]["code_ids"]) == 2
    assert send(commerce, action)[0]["state"] == "awaiting_manual"
    kit.order["delivery_chunks"][0]["items"][0]["truthful_label"] = "\n".join(
        action["body"]["sgtins"]
    )
    assert send(commerce, action, reconcile=True)[0]["state"] == "awaiting_manual"
    assert not any(c[0] == "POST" for c in kit.calls)


@pytest.mark.parametrize("service", ["YANDEX_DELIVERY", "CDEK", "OZON", "META_SHIP"])
def test_kit_external_delivery_cannot_be_completed_by_merchant(commerce, service):
    state, _, seller, _, _, _, connection, _, _, _, kit, _ = commerce
    kit.products[uid(1)]["requires_marking"] = False
    kit.order["status"] = "WAIT_FOR_DELIVERY"
    kit.order["delivery_chunks"][0]["delivery_info"]["courier_delivery_service_type"] = service
    order = state.commerce.records(seller, connection["id"], "orders")["items"][0]
    with pytest.raises(InvalidInput):
        state.commerce.prepare(seller, connection["id"], "complete", {"order_id": order["id"]})
    assert not any(c[0] == "POST" for c in kit.calls)


@pytest.mark.parametrize("status", ["PARTIAL_REFUND", "FULL_REFUND", "COMPLETED", "DELIVERED"])
def test_kit_return_is_not_a_new_cancel_command(commerce, status):
    state, _, seller, _, _, _, connection, _, _, _, kit, _ = commerce
    kit.order["status"] = status
    order = state.commerce.records(seller, connection["id"], "orders")["items"][0]
    with pytest.raises(InvalidInput):
        state.commerce.prepare(seller, connection["id"], "cancel", {"order_id": order["id"]})
    assert not any(c[0] == "POST" for c in kit.calls)


@pytest.mark.parametrize("kind", ["prices", "stocks"])
def test_kit_bulk_decimal_null_and_reserved_quantity_are_preserved(commerce, kind):
    state, _, seller, _, _, _, connection, _, _, _, kit, _ = commerce
    products = state.commerce.records(seller, connection["id"], "products", limit=200)["items"]
    product = next(v for v in products if v["external_id"] == uid(1))
    warehouse = state.commerce.records(seller, connection["id"], "warehouses")["items"][0]
    entry = {"product_id": product["id"]}
    entry.update(
        {"price": "123.45", "manual_discount_price": None}
        if kind == "prices"
        else {"warehouse_id": warehouse["id"], "quantity": 5}
    )
    action = state.commerce.prepare(seller, connection["id"], kind, {"items": [entry]})
    assert send(commerce, action)[0]["state"] == "confirmed"
    source = kit.products[uid(1)]
    stored = state.records.get(seller, "products", product["id"])["attributes"]["source"]
    assert stored == source
    if kind == "prices":
        assert source["pricing"] == {"price": "123.45", "manual_discount_price": None}
    else:
        assert source["stocks"][0]["quantity"] == 5 and source["stocks"][0]["reserved"] == 2


def test_kit_partial_bulk_result_is_retained_without_resending(commerce):
    state, _, seller, _, _, _, connection, _, _, _, kit, _ = commerce
    products = state.commerce.records(seller, connection["id"], "products", limit=200)["items"][:2]
    action = state.commerce.prepare(
        seller,
        connection["id"],
        "prices",
        {
            "items": [{"product_id": p["id"], "price": "321.10"} for p in products],
        },
    )
    kit.partial = True
    final, _ = send(commerce, action)
    assert final["state"] == "partial"
    assert len(final["result"]["matched"]) == len(final["result"]["missing"]) == 1
    assert send(commerce, final, reconcile=True)[0]["state"] == "partial"
    assert len([c for c in kit.calls if c[0] == "POST"]) == 1


@pytest.mark.parametrize("key", ["ozon", "kit"])
def test_connection_secrets_are_protected_and_rotation_rejects_identity_change(commerce, key):
    state, client, seller, _, _, ozon, kit, _, _, ozon_api, kit_api, vault = commerce
    connection = ozon if key == "ozon" else kit
    path = f"/api/sellers/{seller}/commerce/{connection['id']}/credentials"
    old = state.connections.get(seller, connection["id"])["config"]["credential_ref"]
    response = client.put(path, json={"token": "new-private-secret"})
    assert response.status_code == 200 and old not in vault.values
    assert "new-private-secret" not in response.text
    current = state.connections.get(seller, connection["id"])["config"]["credential_ref"]
    if key == "ozon":
        ozon_api.inn = "999999999999"
    else:
        kit_api.store_id = uid(99999)
    assert client.put(path, json={"token": "wrong-account-key"}).status_code == 422
    assert current in vault.values
    assert (
        "config" not in client.get(f"/api/sellers/{seller}/connections/{connection['id']}").json()
    )


@pytest.mark.parametrize("key", ["ozon", "kit"])
def test_read_only_and_foreign_seller_cannot_prepare_actions(commerce, key):
    state, client, seller, *_ = commerce
    seller = state.sellers.create("Read only seller")["id"]
    connection = connect(state, client, seller, key, read_only=True)
    root = f"/api/sellers/{seller}/commerce/{connection['id']}"
    response = client.post(root + "/actions", json={"kind": "cancel", "payload": {"order_id": "x"}})
    assert response.status_code == 422
    other = state.sellers.create("Other")["id"]
    assert (
        client.get(f"/api/sellers/{other}/commerce/{connection['id']}/overview").status_code == 404
    )


def test_generic_queue_and_restart_do_not_bypass_action_state(commerce):
    action, _, _ = assignment(commerce)
    state, _, seller, *_ = commerce
    job = state.operations.enqueue(
        seller, action["connection_id"], "commerce.command", {"action_id": action["id"]}
    )
    drain(state)
    assert state.operations.get(seller, job["id"])["status"] == "failed"
    assert state.commerce.action(seller, action["id"])["state"] == "draft"
    with state.database.connection() as conn:
        state.commerce.change(conn, seller, action["id"], "submitting")
        state.commerce.recover(conn)
    assert state.commerce.action(seller, action["id"])["state"] == "unknown"
    with pytest.raises(Conflict):
        state.commerce.cancel(seller, action["id"])
    with pytest.raises(NotFound):
        state.commerce.action(state.sellers.create("Other")["id"], action["id"])


@pytest.mark.parametrize(
    "kind,payload",
    [
        ("codes", {"order_id": [], "item_id": {}, "code_ids": []}),
        ("confirm", {"order_id": {"bad": True}}),
        ("prices", {"items": [{"product_id": []}]}),
        ("stocks", {"items": [{"product_id": "missing", "warehouse_id": []}]}),
    ],
)
def test_invalid_action_payload_does_not_raise_server_error(commerce, kind, payload):
    _, client, seller, _, _, _, connection, *_ = commerce
    response = client.post(
        f"/api/sellers/{seller}/commerce/{connection['id']}/actions",
        json={"kind": kind, "payload": payload},
    )
    assert response.status_code in {404, 422}


def test_marketplaces_and_custom_store_name_are_persistent_and_seller_scoped(commerce):
    state, client, seller, *_ = commerce
    root = f"/api/sellers/{seller}/settings"
    assert client.put(root + "/store.name", json={"value": "Мой <магазин>"}).status_code == 200
    assert client.put(root + "/marketplace.selected", json={"value": "ozon"}).status_code == 200
    page = client.get(f"/sellers/{seller}/marketplaces")
    assert 'data-adapter="ozon"' in page.text and "Мой &lt;магазин&gt;" in page.text
    assert client.get(f"/sellers/{seller}/marketplaces?channel=wb").status_code == 200
    assert client.get(f"/sellers/{seller}/wb").status_code == 200
    assert client.get(f"/sellers/{seller}/store").status_code == 200
    other = state.sellers.create("Other")["id"]
    assert "Мой &lt;магазин&gt;" not in client.get(f"/sellers/{other}/store").text
    assert client.get(root).json()["store.name"] == "Мой <магазин>"
    for value in ("", " ", "x" * 61, "new\nname", 123):
        assert client.put(root + "/store.name", json={"value": value}).status_code == 422


@pytest.mark.parametrize("key", ["ozon", "kit"])
def test_failed_pagination_never_claims_full_coverage(commerce, key):
    state, _, seller, _, _, ozon, kit, _, _, ozon_api, kit_api, _ = commerce
    connection = ozon if key == "ozon" else kit
    if key == "ozon":
        ozon_api.product_stall = True
    else:
        kit_api.stall_phase = "variants"
    state.commerce.start_sync(seller, connection["id"])
    drain(state)
    snapshot = state.commerce.overview(seller, connection["id"])["snapshots"]["sync"]["value"]
    assert snapshot["state"] == "failed" and snapshot["coverage"] == "partial"


def test_adapter_rate_limits_and_transport_secret_redaction(commerce):
    state, _, seller, _, _, ozon, kit, *_ = commerce
    for connection, interval in ((ozon, 0.6), (kit, 0.35)):
        value = state.connections.get(seller, connection["id"])
        adapter, config = state.commerce.adapter(value)
        assert adapter.interval == interval

        secret = adapter.secret(config)

        def fail(*args, secret=secret, **kwargs):
            raise RemoteError(403, "http_403", {"secret": secret})

        adapter.http.request = fail
        with pytest.raises(RemoteError) as error:
            adapter._call(config, "GET", "/fake")
        assert adapter.secret(config) not in json.dumps(error.value.details)
        assert adapter.config(connection_context(value)).account_id == config.account_id


def test_stage4_migration_preserves_wb_reservations_full_codes_and_running_jobs(commerce):
    import sqlite3

    state, _, seller, chz_connection, wb, _, _, chz, *_ = commerce
    codes = add_codes(state, seller, chz_connection, chz)
    wb_link(state, seller, chz_connection, wb)
    order = next(
        v
        for v in state.fulfillment.records(seller, wb["id"], "orders")["items"]
        if v["external_id"] == "1001"
    )
    action = state.fulfillment.prepare(
        seller,
        wb["id"],
        "sgtin",
        {
            "order_id": order["id"],
            "code_ids": [v["id"] for v in codes[:2]],
        },
    )
    job = state.fulfillment.enqueue(seller, action["id"])
    with state.database.connection() as conn:
        for name in ("wb_reserve_code", "wb_release_code", "wb_assignment_immutable"):
            conn.execute(f"DROP TRIGGER {name}")
        for name in (
            "catalog_imports",
            "catalog_checks",
            "catalog_schemas",
            "catalog_rules",
            "catalog_events",
            "catalog_units",
            "catalog_batches",
            "catalog_files",
            "catalog_document_products",
            "catalog_document_events",
            "catalog_documents",
            "catalog_links",
            "catalog_identifiers",
            "catalog_products",
            "commerce_targets",
            "commerce_events",
            "commerce_actions",
            "commerce_links",
            "commerce_objects",
            "commerce_snapshots",
            "code_reservations",
        ):
            conn.execute(f"DROP TABLE {name}")
        conn.execute("PRAGMA user_version=5")
        conn.execute("UPDATE operations SET status='running' WHERE id=?", (job["id"],))
    state.database.initialize()
    with state.database.connection() as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 7
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        rows = conn.execute("SELECT code,origin,action_id FROM code_reservations").fetchall()
        assert {r[0] for r in rows} == set(action["body"]["cis"])
        assert all(r[1:] == ("wb", action["id"]) for r in rows)
        assert (
            conn.execute("SELECT status FROM operations WHERE id=?", (job["id"],)).fetchone()[0]
            == "running"
        )
    assert (
        state.fulfillment.action(seller, action["id"])["body"]["sgtins"] == action["body"]["sgtins"]
    )
    backup = state.database.path.parent / "flow.before-v7.sqlite3"
    with sqlite3.connect(backup) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 5
        assert conn.execute("SELECT count(*) FROM wb_code_assignments").fetchone()[0] == 2
    state.database.initialize()


def test_ozon_confirms_each_exemplar_not_only_flat_code_set(commerce):
    action, _, _ = assignment(commerce)
    ozon = commerce[9]
    final, _ = send(commerce, action)
    examples = ozon.exemplars["products"][0]["exemplars"]
    examples[0]["marks"].extend(examples[1].pop("marks"))
    ozon.passed = True
    assert send(commerce, final, reconcile=True)[0]["state"] == "conflict"


def test_ozon_second_sku_keeps_first_sku_codes_and_group_checks_independent(commerce):
    action, order, codes = assignment(commerce)
    state, _, seller, chz_connection, _, connection, _, _, _, ozon, *_ = commerce
    ozon.passed = True
    assert send(commerce, action)[0]["state"] == "confirmed"
    first_exemplars = copy.deepcopy(ozon.exemplars["products"][0])
    product = next(
        v
        for v in state.commerce.records(seller, connection["id"], "products", limit=200)["items"]
        if v["external_id"] == "2"
    )
    nk = state.marking.products(seller, chz_connection["id"])["items"][0]
    state.commerce.link(
        seller,
        connection["id"],
        {
            "product_id": product["id"],
            "chz_connection_id": chz_connection["id"],
            "chz_product_id": nk["id"],
            "gtin": "00000000000001",
            "product_group": "account-group",
        },
    )
    second = state.commerce.prepare(
        seller,
        connection["id"],
        "codes",
        {
            "order_id": order["id"],
            "item_id": "10002",
            "code_ids": [codes[2]["id"]],
        },
    )
    assert send(commerce, second)[0]["state"] == "confirmed"
    assert ozon.exemplars["products"][0] == first_exemplars


@pytest.mark.parametrize("phase", ["warehouses", "orders"])
def test_kit_repeated_pages_are_incomplete_for_every_collection(commerce, phase):
    state, _, seller, _, _, _, connection, _, _, _, kit, _ = commerce
    if phase == "orders":
        kit.orders = [{**copy.deepcopy(kit.order), "id": uid(10000 + n)} for n in range(102)]
    else:
        kit.warehouses = [
            {"id": uid(11000 + n), "title": str(n), "status": "ACTIVE"} for n in range(102)
        ]
    kit.stall_phase = phase
    state.commerce.start_sync(seller, connection["id"])
    drain(state)
    value = state.commerce.overview(seller, connection["id"])["snapshots"]["sync"]["value"]
    assert value["state"] == "failed" and value["coverage"] == "partial"


@pytest.mark.parametrize("kind", ["ozon", "kit"])
def test_cancel_is_explicit_journaled_and_does_not_release_transferred_codes(commerce, kind):
    state, _, seller, _, _, ozon_connection, kit_connection, _, _, ozon, kit, _ = commerce
    connection = ozon_connection if kind == "ozon" else kit_connection
    action, order, _ = assignment(commerce, kind)
    if kind == "ozon":
        ozon.passed = True
    send(commerce, action)
    payload = {"order_id": order["id"]}
    if kind == "ozon":
        payload.update(cancel_reason_id=101)
    cancel = state.commerce.prepare(seller, connection["id"], "cancel", payload)
    assert cancel["state"] == "draft"
    assert send(commerce, cancel)[0]["state"] == "confirmed"
    with state.database.connection() as conn:
        assert conn.execute(
            "SELECT count(*) FROM code_reservations WHERE action_id=?", (action["id"],)
        ).fetchone()[0] == len(action["body"]["cis"])
    assert (ozon.order["status"] if kind == "ozon" else kit.order["status"]) in {
        "cancelled",
        "CANCELLATION_IN_PROGRESS",
    }


@pytest.mark.parametrize("failure", ["timeout", "invalid_json", 500])
def test_kit_lost_confirm_response_can_only_be_reconciled(commerce, failure):
    state, _, seller, _, _, _, connection, _, _, _, kit, _ = commerce
    kit.products[uid(1)]["requires_marking"] = False
    order = state.commerce.records(seller, connection["id"], "orders")["items"][0]
    action = state.commerce.prepare(seller, connection["id"], "confirm", {"order_id": order["id"]})
    kit.failure = failure
    final, job = send(commerce, action)
    assert final["state"] == "unknown" and job["status"] == "failed"
    assert not state.operations.run_once()
    kit.failure = None
    final, _ = send(commerce, final, reconcile=True)
    assert final["state"] == ("unknown" if failure == 500 else "confirmed")
    assert len([c for c in kit.calls if c[0] == "POST"]) == 1


@pytest.mark.parametrize("price", ["1e100000000", "NaN", "-1", "0", "1.234", 12, True])
def test_price_validation_prevents_unbounded_decimal_expansion(commerce, price):
    state, _, seller, _, _, _, connection, *_ = commerce
    product = state.commerce.records(seller, connection["id"], "products")["items"][0]
    with pytest.raises(InvalidInput):
        state.commerce.prepare(
            seller,
            connection["id"],
            "prices",
            {
                "items": [{"product_id": product["id"], "price": price}],
            },
        )


def test_kit_changed_price_blocks_write_after_preview(commerce):
    state, _, seller, _, _, _, connection, _, _, _, kit, _ = commerce
    product = next(
        v
        for v in state.commerce.records(seller, connection["id"], "products", limit=200)["items"]
        if v["external_id"] == uid(1)
    )
    action = state.commerce.prepare(
        seller,
        connection["id"],
        "prices",
        {
            "items": [{"product_id": product["id"], "price": "500.00"}],
        },
    )
    kit.products[uid(1)]["pricing"]["price"] = "200.00"
    final, _ = send(commerce, action)
    assert final["state"] == "rejected" and final["result"]["before_send"]
    assert not any(c[0] == "POST" for c in kit.calls)


@pytest.mark.parametrize(
    "field,value",
    [
        ("quantity", 2),
        ("is_product_variant_deleted", True),
        ("truthful_label", "a-different-existing-code"),
    ],
)
def test_kit_fresh_item_changes_block_manual_code_preparation(commerce, field, value):
    action, _, _ = assignment(commerce, "kit")
    kit = commerce[10]
    kit.order["delivery_chunks"][0]["items"][0][field] = value
    final, job = send(commerce, action)
    assert final["state"] == "rejected" and final["result"]["before_send"]
    assert job["status"] == "failed" and not any(c[0] == "POST" for c in kit.calls)


def test_kit_fresh_marking_requirement_cannot_be_hidden_by_cached_catalog(commerce):
    state, _, seller, _, _, _, connection, _, _, _, kit, _ = commerce
    kit.products[uid(2)]["requires_marking"] = True
    order = state.commerce.records(seller, connection["id"], "orders")["items"][0]
    with pytest.raises(Conflict):
        state.commerce.prepare(seller, connection["id"], "confirm", {"order_id": order["id"]})
