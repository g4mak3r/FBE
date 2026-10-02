import pytest
from fastapi.testclient import TestClient

from fbe_flow.core.errors import Conflict, InvalidInput
from fbe_flow.integrations.chz.http import RemoteError
from fbe_flow.integrations.wb.adapter import metadata, token_claims

from .wb_fixtures import add_codes, add_link, application, drain, seed, token


def test_full_suz_application_introduction_then_wb_handoff(wb_workspace):
    from datetime import date

    from .test_chz_suz import issued_codes, prepare_application
    from .test_chz_workflows import send as send_chz

    state, client, seller, chz_connection, connection, chz, wb, vault = wb_workspace
    chz_workspace = (state, client, seller, chz_connection, chz, vault, state.signer)
    _, block = issued_codes(chz_workspace)
    accepted, _ = send_chz(chz_workspace, prepare_application(chz_workspace))
    applied, _ = send_chz(chz_workspace, accepted, poll=True)
    assert applied["state"] == "succeeded"
    codes = state.marking.codes(seller, chz_connection["id"])["items"]
    introduction = state.marking.prepare(
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
    accepted, _ = send_chz(chz_workspace, introduction)
    introduced, _ = send_chz(chz_workspace, accepted, poll=True)
    assert introduced["state"] == "succeeded"
    add_link(state, seller, chz_connection, connection)
    order = next(
        v
        for v in state.fulfillment.records(seller, connection["id"], "orders")["items"]
        if v["external_id"] == "1001"
    )
    action = state.fulfillment.prepare(
        seller,
        connection["id"],
        "sgtin",
        {"order_id": order["id"], "code_ids": [v["id"] for v in codes]},
    )
    final, _ = send(wb_workspace, action)
    assert final["state"] == "confirmed"
    raw = state.marking.code_export(seller, chz_connection["id"], block["external_id"])["codes"]
    assert set(wb.meta[1001][0]["value"]) == set(raw)
    assert all("\x1d" in v for v in raw)


def test_deliver_rechecks_chz_even_when_wb_metadata_was_filled(wb_workspace):
    state, _, seller, _, connection, chz, wb, _ = wb_workspace
    action, _, _ = transfer(wb_workspace)
    final, _ = send(wb_workspace, action)
    assert final["state"] == "confirmed"
    drain(state)
    chz.cises[action["body"]["cis"][0]]["cisInfo"]["status"] = "WRITTEN_OFF"
    supply = state.fulfillment.records(seller, connection["id"], "supplies")["items"][0]
    delivery = state.fulfillment.prepare(
        seller, connection["id"], "supply_deliver", {"supply_id": supply["id"]}
    )
    result, job = send(wb_workspace, delivery)
    assert result["state"] == "rejected" and job["status"] == "failed"
    assert not any(v[1].endswith("/deliver") for v in wb.calls)


def test_manual_retry_after_409_requires_fresh_checks_and_same_exact_codes(wb_workspace):
    state, _, seller, _, _, _, wb, _ = wb_workspace
    action, _, _ = transfer(wb_workspace)
    wb.failure = 409
    result, _ = send(wb_workspace, action)
    assert result["state"] == "unknown"
    assert not state.operations.run_once()
    wb.failure = None
    state.fulfillment.retry_codes(seller, action["id"])
    state.operations.run_once()
    assert state.fulfillment.action(seller, action["id"])["state"] == "confirmed"
    writes = [v for v in wb.calls if v[0] == "PUT"]
    assert len(writes) == 2 and writes[0][3] == writes[1][3]


def test_partial_supply_keeps_success_and_allows_separate_missing_action(wb_workspace):
    state, _, seller, _, connection, _, wb, _ = wb_workspace
    supply = state.fulfillment.records(seller, connection["id"], "supplies")["items"][0]
    orders = state.fulfillment.records(seller, connection["id"], "orders")["items"]
    action = state.fulfillment.prepare(
        seller,
        connection["id"],
        "supply_add",
        {
            "supply_id": supply["id"],
            "order_ids": [v["id"] for v in sorted(orders, key=lambda v: v["external_id"])],
        },
    )
    wb.partial_add = True
    result, _ = send(wb_workspace, action)
    assert (
        result["state"] == "partial"
        and result["result"]["present"] == [1001]
        and result["result"]["missing"] == [1002]
    )
    drain(state)
    wb.partial_add = False
    missing = next(v for v in orders if v["external_id"] == "1002")
    followup = state.fulfillment.prepare(
        seller,
        connection["id"],
        "supply_add",
        {"supply_id": supply["id"], "order_ids": [missing["id"]]},
    )
    assert send(wb_workspace, followup)[0]["state"] == "confirmed"
    assert state.fulfillment.action(seller, action["id"])["result"]["present"] == [1001]


def test_live_supply_details_include_orders_outside_cached_history(wb_workspace):
    state, _, seller, _, connection, _, wb, _ = wb_workspace
    wb.orders[9999] = {**wb.orders[1001], "id": 9999}
    wb.statuses[9999] = "confirm"
    wb.meta[9999] = []
    supply = state.fulfillment.records(seller, connection["id"], "supplies")["items"][0]
    details = state.fulfillment.supply_details(seller, connection["id"], supply["id"])
    historical = next(v for v in details["orders"] if v["external_id"] == "9999")
    assert historical["cached"] is None and historical["status"] == "confirm"


def test_generic_queue_does_not_bypass_wb_action_journal(wb_workspace):
    state, _, seller, _, connection, _, wb, _ = wb_workspace
    action, _, _ = transfer(wb_workspace)
    job = state.operations.enqueue(
        seller, connection["id"], "wb.command", {"action_id": action["id"]}
    )
    state.operations.run_once()
    assert state.operations.get(seller, job["id"])["status"] == "failed"
    assert state.fulfillment.action(seller, action["id"])["state"] == "draft"
    assert not any(v[0] == "PUT" for v in wb.calls)


def test_wb_token_rotation_keeps_identity_and_removes_old_secret(wb_workspace):
    state, client, seller, _, connection, _, _, vault = wb_workspace
    root = f"/api/sellers/{seller}/wb/{connection['id']}/credentials"
    old = state.connections.get(seller, connection["id"])["config"]["credential_ref"]
    response = client.put(root, json={"token": token(exp=2000000000)})
    assert response.status_code == 200, response.text
    assert old not in vault.values
    current = state.connections.get(seller, connection["id"])["config"]["credential_ref"]
    assert current in vault.values
    response = client.put(root, json={"token": token(account="wrong-account")})
    assert response.status_code == 422 and current in vault.values
    state.fulfillment.start_sync(seller, connection["id"])
    refs = set(vault.values)
    assert client.put(root, json={"token": token(exp=2000000001)}).status_code == 409
    assert set(vault.values) == refs


def test_read_only_token_syncs_but_does_not_permit_writes(wb_workspace):
    state, client, seller, _, _, _, _, _ = wb_workspace
    response = client.post(
        f"/api/sellers/{seller}/wb/connections",
        json={
            "name": "Read only",
            "token": token(mask=18 | (1 << 30), account="read-only-account"),
        },
    )
    assert response.status_code == 201
    connection = response.json()
    state.fulfillment.start_sync(seller, connection["id"])
    drain(state)
    assert state.fulfillment.overview(seller, connection["id"])["counts"]["products"] == 102
    assert "wb.command" not in {v["key"] for v in connection["operations"]}
    with pytest.raises(InvalidInput):
        state.fulfillment.prepare(seller, connection["id"], "supply_create", {"name": "Forbidden"})


def test_failed_connect_removes_secret_and_http_shape_validation(wb_workspace):
    _, client, seller, _, connection, _, wb, vault = wb_workspace
    refs = set(vault.values)
    wb.account["tin"] = ""
    secret = token(exp=2000000001)
    response = client.post(
        f"/api/sellers/{seller}/wb/connections", json={"name": "Broken", "token": secret}
    )
    assert response.status_code == 502 and secret not in response.text and set(vault.values) == refs
    root = f"/api/sellers/{seller}/wb/{connection['id']}"
    for payload in (
        {},
        {"order_id": "missing", "code_ids": [1]},
        {"order_id": "missing", "code_ids": []},
    ):
        assert (
            client.post(root + "/actions", json={"kind": "sgtin", "payload": payload}).status_code
            == 422
        )
    assert client.get(root + "/records/products?limit=201").status_code == 422


def test_sync_stalled_cursor_fails_preserving_committed_pages(wb_workspace):
    state, _, seller, _, connection, _, wb, _ = wb_workspace
    wb.product_stall = True
    state.fulfillment.start_sync(seller, connection["id"])
    drain(state)
    assert (
        state.fulfillment.overview(seller, connection["id"])["snapshots"]["sync"]["value"]["state"]
        == "failed"
    )
    assert state.fulfillment.records(seller, connection["id"], "products")["total"] == 102


def test_full_code_cannot_be_reserved_across_duplicate_seller_workspaces(wb_workspace):
    state, client, seller, _, connection, chz, wb, _ = wb_workspace
    action, _, _ = transfer(wb_workspace)
    other, other_chz, other_wb = seed(state, client, chz, wb)
    add_link(state, other, other_chz, other_wb)
    codes = add_codes(state, other, other_chz, chz)
    order = next(
        v
        for v in state.fulfillment.records(other, other_wb["id"], "orders")["items"]
        if v["external_id"] == "1001"
    )
    reserved = next(v for v in codes if v["code"] == action["body"]["cis"][0])
    with pytest.raises(Conflict):
        state.fulfillment.prepare(
            other, other_wb["id"], "sgtin", {"order_id": order["id"], "code_ids": [reserved["id"]]}
        )


@pytest.fixture
def wb_workspace(tmp_path):
    app, chz, wb, vault = application(tmp_path)
    with TestClient(app, base_url="http://localhost", headers={"X-FBE-Flow": "1"}) as client:
        seller, chz_connection, connection = seed(app.state, client, chz, wb)
        yield app.state, client, seller, chz_connection, connection, chz, wb, vault


def transfer(workspace):
    state, _, seller, chz_connection, connection, chz, *_ = workspace
    add_link(state, seller, chz_connection, connection)
    codes = add_codes(state, seller, chz_connection, chz)
    order = next(
        v
        for v in state.fulfillment.records(seller, connection["id"], "orders")["items"]
        if v["external_id"] == "1001"
    )
    action = state.fulfillment.prepare(
        seller, connection["id"], "sgtin", {"order_id": order["id"], "code_ids": [codes[0]["id"]]}
    )
    return action, codes, order


def send(workspace, action):
    state, _, seller, *_ = workspace
    job = state.fulfillment.enqueue(seller, action["id"])
    state.operations.run_once()
    return state.fulfillment.action(seller, action["id"]), state.operations.get(seller, job["id"])


def test_sync_pages_identity_cache_and_secret_boundary(wb_workspace):
    state, client, seller, _, connection, _, wb, vault = wb_workspace
    root = f"/api/sellers/{seller}/wb/{connection['id']}"
    assert client.get(root + "/records/products").json()["total"] == 102
    assert len(client.get(root + "/records/products?offset=100").json()["items"]) == 2
    ids = {
        v["external_id"]: v["id"]
        for v in state.fulfillment.records(seller, connection["id"], "products", limit=200)["items"]
    }
    state.fulfillment.start_sync(seller, connection["id"])
    with pytest.raises(Conflict):
        state.fulfillment.start_sync(seller, connection["id"])
    drain(state)
    assert {
        v["external_id"]: v["id"]
        for v in state.fulfillment.records(seller, connection["id"], "products", limit=200)["items"]
    } == ids
    assert (
        state.fulfillment.overview(seller, connection["id"])["snapshots"]["sync"]["value"]["state"]
        == "succeeded"
    )
    token_value = next(v["wb_token"] for v in vault.values.values() if "wb_token" in v)
    for path in (
        f"/api/sellers/{seller}/connections",
        root + "/overview",
        root + "/actions",
        f"/sellers/{seller}/wb",
    ):
        assert token_value not in client.get(path).text
    assert any(
        v[3]["settings"]["filter"]["withPhoto"] == -1
        for v in wb.calls
        if v[1] == "/content/v2/get/cards/list"
    )
    with state.database.connection() as conn:
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert token_value not in "".join(
            r[0] for r in conn.execute("SELECT config_json FROM connections").fetchall()
        )


def test_transfer_preserves_full_km_and_requires_remote_confirmation(wb_workspace):
    state, _, seller, _, connection, chz, wb, _ = wb_workspace
    action, codes, order = transfer(wb_workspace)
    assert not [v for v in wb.calls if v[0] == "PUT"]
    assert (
        len(state.fulfillment.available_codes(seller, connection["id"], order["id"])["items"])
        == len(codes) - 1
    )
    final, job = send(wb_workspace, action)
    assert final["state"] == "confirmed" and final["acknowledged"]
    assert job["status"] == "succeeded"
    request = next(v for v in wb.calls if v[0] == "PUT")
    assert request[1] == "/api/v3/orders/1001/meta/sgtin"
    assert request[3] == {"sgtins": action["body"]["sgtins"]}
    assert "\x1d" in request[3]["sgtins"][0]
    assert final["result"]["decision"] == "filled"
    assert chz.cises[action["body"]["cis"][0]]["cisInfo"]["status"] == "INTRODUCED"
    with pytest.raises(Conflict):
        state.fulfillment.enqueue(seller, action["id"])
    drain(state)
    assert len([v for v in wb.calls if v[0] == "PUT"]) == 1


@pytest.mark.parametrize(
    "change",
    [
        "not_introduced",
        "owner",
        "group",
        "package",
        "status_ex",
        "not_confirm",
        "different_wb",
        "unavailable",
        "account",
        "missing_code_response",
    ],
)
def test_preflight_blocks_bad_codes_or_wrong_wb_state(wb_workspace, change):
    _, _, _, _, _, chz, wb, _ = wb_workspace
    action, _, _ = transfer(wb_workspace)
    info = chz.cises[action["body"]["cis"][0]]["cisInfo"]
    if change == "not_introduced":
        info["status"] = "APPLIED"
    if change == "owner":
        info["ownerInn"] = "999999999999"
    if change == "group":
        info["productGroup"] = "other-group"
    if change == "package":
        info["packageType"] = "GROUP"
    if change == "status_ex":
        info["statusEx"] = "BLOCKED"
    if change == "not_confirm":
        wb.statuses[1001] = "complete"
    if change == "different_wb":
        wb.meta[1001][0]["value"] = ["OTHER-CODE"]
    if change == "unavailable":
        wb.meta[1001] = []
    if change == "account":
        wb.account["sid"] = "other-account"
        wb_workspace[0].registry.get("wb")._accounts.clear()
    if change == "missing_code_response":
        info["requestedCis"] = "different-code"
    final, job = send(wb_workspace, action)
    assert final["state"] == "rejected" and not final["acknowledged"]
    assert job["status"] == "failed"
    assert not any(v[0] == "PUT" for v in wb.calls)


@pytest.mark.parametrize(
    "decision,state",
    [
        ("pending", "pending"),
        ("required", "pending"),
        ("sgtinNotFound", "rejected"),
        ("newDecision", "rejected"),
        (None, "pending"),
    ],
)
def test_metadata_decision_separate_from_successful_http(wb_workspace, decision, state):
    app_state, _, seller, *_ = wb_workspace
    wb = wb_workspace[6]
    action, _, _ = transfer(wb_workspace)
    wb.decision_after_send = decision
    final, _ = send(wb_workspace, action)
    assert final["state"] == state and final["acknowledged"]
    with pytest.raises(Conflict):
        app_state.fulfillment.cancel(seller, action["id"])
    if state == "pending":
        wb.meta[1001][0]["decision"] = "filled"
        app_state.fulfillment.enqueue(seller, action["id"], reconcile=True)
        app_state.operations.run_once()
        assert app_state.fulfillment.action(seller, action["id"])["state"] == "confirmed"
    assert len([v for v in wb.calls if v[0] == "PUT"]) == 1


@pytest.mark.parametrize("failure", ["timeout", "invalid_json", 500, 409])
def test_lost_receipt_reconciles_without_repeating_write(wb_workspace, failure):
    state, _, seller, _, _, _, wb, _ = wb_workspace
    action, _, _ = transfer(wb_workspace)
    wb.failure = failure
    final, job = send(wb_workspace, action)
    assert final["state"] == "unknown" and job["status"] == "failed"
    with pytest.raises(Conflict):
        state.fulfillment.enqueue(seller, action["id"])
    with pytest.raises(Conflict):
        state.fulfillment.cancel(seller, action["id"])
    wb.failure = None
    if failure in {500, 409}:
        wb.meta[1001][0].update(value=action["body"]["sgtins"], decision="filled")
    state.fulfillment.enqueue(seller, action["id"], reconcile=True)
    state.operations.run_once()
    assert state.fulfillment.action(seller, action["id"])["state"] == "confirmed"
    assert len([v for v in wb.calls if v[0] == "PUT"]) == 1


@pytest.mark.parametrize("status", [400, 401, 402, 403, 404, 429])
def test_definite_rejection_can_release_reservation(wb_workspace, status):
    state, _, seller, _, connection, _, wb, _ = wb_workspace
    action, codes, order = transfer(wb_workspace)
    wb.failure = status
    final, _ = send(wb_workspace, action)
    assert final["state"] == "rejected" and final["result"]["definite_rejection"]
    state.fulfillment.cancel(seller, action["id"])
    assert len(
        state.fulfillment.available_codes(seller, connection["id"], order["id"])["items"]
    ) == len(codes)


def test_reservation_duplicate_parallel_preparation_and_cancel(wb_workspace):
    state, _, seller, _, connection, *_ = wb_workspace
    action, codes, order = transfer(wb_workspace)
    with pytest.raises(Conflict):
        state.fulfillment.prepare(
            seller,
            connection["id"],
            "sgtin",
            {"order_id": order["id"], "code_ids": [codes[0]["id"]]},
        )
    wb_workspace[6].statuses[1002] = "confirm"
    state.fulfillment.start_sync(seller, connection["id"])
    drain(state)
    other = next(
        v
        for v in state.fulfillment.records(seller, connection["id"], "orders")["items"]
        if v["external_id"] == "1002"
    )
    with pytest.raises(Conflict):
        state.fulfillment.prepare(
            seller,
            connection["id"],
            "sgtin",
            {"order_id": other["id"], "code_ids": [codes[0]["id"]]},
        )
    state.fulfillment.cancel(seller, action["id"])
    assert (
        state.fulfillment.prepare(
            seller,
            connection["id"],
            "sgtin",
            {"order_id": order["id"], "code_ids": [codes[0]["id"]]},
        )["state"]
        == "draft"
    )


def test_accepted_ack_survives_followup_read_failure_and_restart(wb_workspace):
    state, _, seller, _, _, _, wb, _ = wb_workspace
    action, _, _ = transfer(wb_workspace)
    wb.fail_read_after_send = True
    final, job = send(wb_workspace, action)
    assert final["state"] == "accepted" and final["acknowledged"] and job["status"] == "failed"
    state.operations.recover_interrupted()
    wb.fail_read_after_send = False
    with state.database.connection() as conn:
        conn.execute(
            "UPDATE wb_actions SET next_check_at='2000-01-01T00:00:00Z' WHERE id=?", (action["id"],)
        )
    state.operations.run_once()
    assert state.fulfillment.action(seller, action["id"])["state"] == "confirmed"
    assert len([v for v in wb.calls if v[0] == "PUT"]) == 1


def test_restart_submitting_is_unknown_and_not_replayed(wb_workspace):
    state, _, seller, *_ = wb_workspace
    action, _, _ = transfer(wb_workspace)
    job = state.fulfillment.enqueue(seller, action["id"])
    with state.database.connection() as conn:
        conn.execute("UPDATE operations SET status='running' WHERE id=?", (job["id"],))
        conn.execute("UPDATE wb_actions SET state='submitting' WHERE id=?", (action["id"],))
    state.operations.recover_interrupted()
    assert state.fulfillment.action(seller, action["id"])["state"] == "unknown"
    assert not state.operations.run_once()


@pytest.mark.parametrize("kind", ["supply_create", "supply_add", "supply_deliver", "supply_delete"])
def test_supply_commands_and_recovery(wb_workspace, kind):
    state, _, seller, _, connection, _, wb, _ = wb_workspace
    supply = state.fulfillment.records(seller, connection["id"], "supplies")["items"][0]
    order = next(
        v
        for v in state.fulfillment.records(seller, connection["id"], "orders")["items"]
        if v["external_id"] == "1002"
    )
    if kind == "supply_create":
        payload = {"name": "Prepared supply"}
    elif kind == "supply_add":
        payload = {"supply_id": supply["id"], "order_ids": [order["id"]]}
    else:
        payload = {"supply_id": supply["id"]}
    if kind == "supply_delete":
        wb.orders[1001]["supplyId"] = None
    if kind == "supply_deliver":
        wb.meta[1001][0].update(value=["existing"], decision="filled")
    action = state.fulfillment.prepare(seller, connection["id"], kind, payload)
    wb.failure = "timeout"
    final, _ = send(wb_workspace, action)
    assert final["state"] == "unknown"
    wb.failure = None
    state.fulfillment.enqueue(seller, action["id"], reconcile=True)
    state.operations.run_once()
    assert state.fulfillment.action(seller, action["id"])["state"] == "confirmed"
    mutations = [
        v
        for v in wb.calls
        if v[0] in {"PATCH", "DELETE"} or (v[0] == "POST" and v[1] == "/api/v3/supplies")
    ]
    assert len(mutations) == 1
    if kind == "supply_add":
        assert mutations[0][1] == "/api/marketplace/v3/supplies/WB-GI-1/orders"


@pytest.mark.parametrize(
    "field,value",
    [("warehouseId", 888), ("cargoType", 2), ("crossBorderType", 2), ("options", {"isB2B": True})],
)
def test_supply_composition_rejects_incompatible_orders(wb_workspace, field, value):
    state, _, seller, _, connection, _, wb, _ = wb_workspace
    wb.orders[1002][field] = value
    state.fulfillment.start_sync(seller, connection["id"])
    drain(state)
    supply = state.fulfillment.records(seller, connection["id"], "supplies")["items"][0]
    order = next(
        v
        for v in state.fulfillment.records(seller, connection["id"], "orders")["items"]
        if v["external_id"] == "1002"
    )
    with pytest.raises(InvalidInput):
        state.fulfillment.prepare(
            seller,
            connection["id"],
            "supply_add",
            {"supply_id": supply["id"], "order_ids": [order["id"]]},
        )


def test_deliver_blocks_pending_metadata_and_nonempty_delete(wb_workspace):
    state, _, seller, _, connection, _, wb, _ = wb_workspace
    supply = state.fulfillment.records(seller, connection["id"], "supplies")["items"][0]
    for kind in ("supply_delete", "supply_deliver"):
        action = state.fulfillment.prepare(
            seller, connection["id"], kind, {"supply_id": supply["id"]}
        )
        final, job = send(wb_workspace, action)
        assert final["state"] == "rejected" and job["status"] == "failed"
        state.fulfillment.cancel(seller, action["id"])
    assert not any(v[0] in {"PATCH", "DELETE"} for v in wb.calls)


def test_seller_isolation_all_wb_views_and_mutations(wb_workspace):
    state, client, seller, _, connection, *_ = wb_workspace
    action, _, order = transfer(wb_workspace)
    other = state.sellers.create("Other seller")["id"]
    root = f"/api/sellers/{other}/wb"
    for suffix in (
        "overview",
        "records/products",
        "records/orders",
        "records/warehouses",
        "records/supplies",
        "links",
        "actions",
        f"orders/{order['id']}/available-codes",
    ):
        assert client.get(f"{root}/{connection['id']}/{suffix}").status_code == 404
    for suffix in ("send", "reconcile", "cancel"):
        assert client.post(f"{root}/actions/{action['id']}/{suffix}", json={}).status_code == 404
    assert client.get(f"{root}/actions/{action['id']}").status_code == 404
    assert client.post(f"{root}/{connection['id']}/sync", json={}).status_code == 404
    assert client.get(f"/sellers/{other}/wb").status_code == 200


@pytest.mark.parametrize(
    "mask,exp,claims",
    [
        (16, None, {}),
        (2, None, {}),
        (18, 1, {}),
        (18, None, {"test": True}),
        (18, None, {"t": True}),
        (18, None, {"acc": 2}),
    ],
)
def test_token_constraints(mask, exp, claims):
    with pytest.raises(InvalidInput):
        token_claims(token(mask=mask, exp=exp, **claims))


def test_invalid_metadata_response_cannot_confirm(wb_workspace):
    _, _, _, _, _, _, wb, _ = wb_workspace
    action, _, _ = transfer(wb_workspace)
    wb.bad_meta_ids = True
    final, _ = send(wb_workspace, action)
    assert final["state"] == "rejected"
    assert not any(v[0] == "PUT" for v in wb.calls)


def test_mixed_groups_transfer_independently(wb_workspace):
    from urllib.parse import urlparse

    from .chz_fixtures import card

    state, _, seller, chz_connection, connection, chz, wb, _ = wb_workspace
    first, _, _ = transfer(wb_workspace)
    wb.decision_after_send = "pending"
    assert send(wb_workspace, first)[0]["state"] == "pending"
    original_request = chz.request

    def group_request(method, url, **kwargs):
        reply = original_request(method, url, **kwargs)
        if urlparse(url).path.endswith("/participants"):
            reply.data["productGroups"] = [chz.group, "other-group"]
        return reply

    chz.request = group_request
    chz.cards[2] = card(2)
    wb.orders[1002].update(nmId=2, chrtId=21, supplyId="WB-GI-1")
    wb.statuses[1002] = "confirm"
    state.marking.start_sync(seller, chz_connection["id"])
    state.fulfillment.start_sync(seller, connection["id"])
    drain(state)
    nk = next(
        v
        for v in state.marking.products(seller, chz_connection["id"])["items"]
        if v["external_id"] == "2"
    )
    product = next(
        v
        for v in state.fulfillment.records(seller, connection["id"], "products", limit=200)["items"]
        if v["external_id"] == "2"
    )
    state.fulfillment.link(
        seller,
        connection["id"],
        {
            "product_id": product["id"],
            "chrt_id": "21",
            "chz_connection_id": chz_connection["id"],
            "chz_product_id": nk["id"],
            "gtin": "00000000000002",
            "product_group": "other-group",
        },
    )
    cis = "010000000000000221SERIALOTHER"
    row = {
        "cisInfo": {
            "requestedCis": cis,
            "gtin": "00000000000002",
            "ownerInn": chz.inn,
            "productGroup": "other-group",
            "packageType": "UNIT",
            "status": "INTRODUCED",
        }
    }
    chz.cises[cis] = row
    with state.database.connection() as conn:
        state.marking._apply_codes(
            conn, {"seller_id": seller, "connection_id": chz_connection["id"]}, [row]
        )
        conn.execute(
            "UPDATE marking_codes SET full_code=? WHERE seller_id=? AND code=?",
            (cis + "\x1d91KEY\x1d92CRYPTO", seller, cis),
        )
    code = next(
        v for v in state.marking.codes(seller, chz_connection["id"])["items"] if v["code"] == cis
    )
    order = next(
        v
        for v in state.fulfillment.records(seller, connection["id"], "orders")["items"]
        if v["external_id"] == "1002"
    )
    second = state.fulfillment.prepare(
        seller, connection["id"], "sgtin", {"order_id": order["id"], "code_ids": [code["id"]]}
    )
    wb.decision_after_send = "filled"
    assert send(wb_workspace, second)[0]["state"] == "confirmed"
    assert state.fulfillment.action(seller, first["id"])["state"] == "pending"


def test_changed_metadata_between_preflight_reads_is_never_overwritten(wb_workspace):
    state, _, seller, _, _, _, wb, _ = wb_workspace
    action, _, _ = transfer(wb_workspace)
    original = wb.request
    reads = [0]

    def request(method, url, **kwargs):
        if url.endswith("/api/marketplace/v3/orders/meta"):
            reads[0] += 1
            if reads[0] == 2:
                wb.meta[1001][0]["value"] = ["ANOTHER-CLIENT-CODE"]
        return original(method, url, **kwargs)

    wb.request = request
    result, _ = send(wb_workspace, action)
    assert result["state"] == "rejected" and not result["acknowledged"]
    assert not any(v[0] == "PUT" for v in wb.calls)
    assert state.fulfillment.cancel(seller, action["id"])["state"] == "cancelled"


def test_matching_pending_codes_are_observed_without_resubmission(wb_workspace):
    _, _, _, _, _, _, wb, _ = wb_workspace
    action, _, _ = transfer(wb_workspace)
    wb.meta[1001][0].update(value=action["body"]["sgtins"], decision="pending")
    result, _ = send(wb_workspace, action)
    assert result["state"] == "pending" and result["acknowledged"]
    assert result["result"]["already_present"]
    assert not any(v[0] == "PUT" for v in wb.calls)


def test_identity_cache_and_service_pacing_follow_wb_limits(wb_workspace):
    from fbe_flow.modules.connections import connection_context

    state, _, seller, _, connection, _, wb, _ = wb_workspace
    adapter = state.registry.get("wb")
    config = adapter.config(connection_context(state.connections.get(seller, connection["id"])))
    adapter._accounts.clear()
    adapter._next.clear()
    wb.calls.clear()
    clock = [0]
    delays = []
    adapter.clock = lambda: clock[0]

    def pause(delay):
        delays.append(delay)
        clock[0] += delay

    adapter.pause = pause
    adapter.account(config)
    adapter.account(config)
    clock[0] = 59
    adapter.account(config)
    assert len([v for v in wb.calls if v[1] == "/api/v1/seller-info"]) == 1
    clock[0] = 61
    adapter.account(config)
    assert len([v for v in wb.calls if v[1] == "/api/v1/seller-info"]) == 2
    for _ in range(2):
        adapter._call(config, "marketplace", "GET", "/api/v3/warehouses")
    assert delays[-1] == 0.5
    for _ in range(2):
        adapter._call(
            config,
            "content",
            "POST",
            "/content/v2/get/cards/list",
            body={"settings": {"cursor": {"limit": 1}}},
        )
    assert delays[-1] == pytest.approx(0.65)


def test_new_metadata_is_authoritative_and_duplicate_keys_fail():
    assert metadata({"metaDetails": [], "meta": {"sgtin": {"value": ["old"]}}}) == {}
    assert metadata({"meta": {"sgtin": {"value": ["old"]}}})["sgtin"]["decision"] is None
    with pytest.raises(RemoteError):
        metadata({"metaDetails": [{"key": "sgtin"}, {"key": "sgtin"}]})
