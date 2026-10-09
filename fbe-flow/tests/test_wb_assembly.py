"""Assembly queue boundaries and read-only background refresh, using an isolated WB provider."""

import json

import pytest
from fastapi.testclient import TestClient

from .wb_fixtures import application, drain, seed


@pytest.fixture
def assembly(tmp_path):
    app, chz, wb, _ = application(tmp_path)
    with TestClient(app, base_url="http://localhost", headers={"X-FBE-Flow": "1"}) as client:
        seller, _, connection = seed(app.state, client, chz, wb)
        yield app.state, client, seller, connection["id"], wb


def test_queues_exclude_assigned_new_and_received_supplies(assembly):
    state, client, seller, connection, wb = assembly
    root = f"/api/sellers/{seller}/wb/{connection}"

    def records(kind, queue, extra=""):
        response = client.get(f"{root}/records/{kind}?queue={queue}{extra}")
        assert response.status_code == 200, response.text
        return response.json()

    assert [v["external_id"] for v in records("orders", "new")["items"]] == ["1002"]
    assert records("supplies", "current", "&warehouse=999")["total"] == 1
    assert records("supplies", "current", "&warehouse=other")["total"] == 0
    assert records("orders", "current", "&supply=WB-GI-1")["total"] == 1
    wb.orders[1002]["supplyId"] = "WB-GI-1"  # Even before the status settles, it is not new.
    wb.supplies["WB-GI-1"]["done"] = True
    state.fulfillment.start_sync(seller, connection)
    drain(state)
    assert records("orders", "new")["total"] == 0
    assert records("supplies", "current")["items"][0]["status"] == "closed"
    wb.supplies["WB-GI-1"]["scanDt"] = "2026-01-01T12:00:00Z"
    state.fulfillment.start_sync(seller, connection)
    drain(state)
    assert records("supplies", "current")["total"] == 0
    assert records("orders", "current", "&supply=WB-GI-1")["total"] == 0
    assert records("orders", "archive")["total"] == 2
    assert records("supplies", "archive")["total"] == 1
    assert client.get(root + "/overview").json()["queues"]["new"] == 0


def test_refresh_singleflight_cooldown_skips_cards_and_rechecks_old_active(assembly):
    state, client, seller, connection, wb = assembly
    wb.calls.clear()
    root = f"/api/sellers/{seller}/wb/{connection}"
    first = client.post(root + "/refresh", json={}).json()
    second = client.post(root + "/refresh", json={}).json()
    assert first.get("state") == "queued", first
    assert second == {"state": "running", "id": first["id"]}
    # This order is no longer returned in /new or history. The cached source still gets checked.
    wb.statuses[1002] = "cancel"
    del wb.orders[1002]
    drain(state)
    paths = [call[1] for call in wb.calls]
    assert paths[0] == "/api/v3/orders/new"
    assert "/content/v2/get/cards/list" not in paths
    assert "/api/v3/warehouses" not in paths
    assert client.get(root + "/records/orders?queue=new").json()["total"] == 0
    assert client.post(root + "/refresh", json={}).json()["state"] == "cooldown"
    assert len(paths) == len(wb.calls)
    assert (
        state.fulfillment.overview(seller, connection)["snapshots"]["sync"]["value"]["state"]
        == "succeeded"
    )


def test_reference_refresh_and_real_card_variant(assembly):
    state, client, seller, connection, wb = assembly
    wb.cards[1]["photos"] = [{"square": "https://basket-01.wbbasket.ru/real.jpg"}]
    with state.database.connection() as conn:
        conn.execute("DELETE FROM wb_snapshots WHERE key='references'")
    state.fulfillment.ensure_refresh(seller, connection)
    drain(state)
    item = state.fulfillment.records(seller, connection, "orders", queue="new")["items"][0]
    assert item["packing"]["image"] == wb.cards[1]["photos"][0]["square"]
    assert item["packing"]["variant"]["chrtID"] == 11
    assert item["packing"]["title"] == wb.cards[1]["title"]
    # Never guess a variant when chrtID does not match.
    with state.database.connection() as conn:
        source = item["attributes"]
        source["source"]["chrtId"] = 999
        conn.execute(
            "UPDATE orders SET attributes_json=? WHERE id=?", (json.dumps(source), item["id"])
        )
    item = state.fulfillment.records(seller, connection, "orders", queue="new")["items"][0]
    assert item["packing"]["variant"] is None
    assert client.get(f"/sellers/{seller}/sales").status_code == 200
