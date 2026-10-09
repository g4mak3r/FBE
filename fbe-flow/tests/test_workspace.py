import sqlite3
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from fbe_flow.core.models import NormalizedBatch, Order, Warehouse

from .commerce_fixtures import application, seed_all


@pytest.fixture
def ui(tmp_path):
    app, chz, wb, *_ = application(tmp_path)
    with TestClient(app, base_url="http://localhost", headers={"X-FBE-Flow": "1"}) as client:
        seller, chz_connection, wb_connection, ozon, kit = seed_all(app.state, client, chz, wb)
        other = app.state.sellers.create("Other organization")["id"]
        yield app.state, client, seller, other, chz_connection, wb_connection, ozon, kit


def test_navigation_remembers_organization_and_keeps_legacy_deep_links(ui):
    _, client, seller, other, *_ = ui
    page = client.get(f"/sellers/{seller}/overview").text
    assert page.split('<nav id="primary-navigation"')[1].split("</nav>")[0].count("<a ") == 5
    assert 'id="seller-select"' in page
    assert 'id="worker-status"' not in page and "FBE Flow 0.8.0" not in page
    client.get(f"/sellers/{other}/overview")
    assert client.get("/", follow_redirects=False).headers["location"].endswith(f"{other}/overview")
    response = client.get(
        f"/sellers/{seller}/ozon?connection=owned&stage=assembly", follow_redirects=False
    )
    query = parse_qs(urlparse(response.headers["location"]).query)
    assert query == {"channel": ["ozon"], "connection": ["owned"], "stage": ["assembly"]}
    assert (
        "tab=integrations"
        in client.get(f"/sellers/{seller}/connections", follow_redirects=False).headers["location"]
    )


def test_single_organization_has_no_switcher(client):
    seller = client.app.state.sellers.create("Only organization")["id"]
    page = client.get(f"/sellers/{seller}/overview").text
    assert 'id="seller-select"' not in page
    assert 'id="add-seller"' not in page
    assert 'id="add-seller"' in client.get(f"/sellers/{seller}/settings").text


def test_workspace_counts_and_sales_deep_links_match_actual_filtered_data(ui):
    state, client, seller, other, _, wb, ozon, kit = ui
    state.records.apply(
        seller,
        ozon["id"],
        NormalizedBatch(
            warehouses=(Warehouse(external_id="east", name="East"),),
            orders=(
                Order(
                    external_id="ready-east",
                    status="awaiting_deliver",
                    warehouse_external_id="east",
                ),
            ),
        ),
    )
    widgets = client.get(f"/api/sellers/{seller}/workspace").json()["widgets"]
    for widget in widgets[:2]:
        for row in widget["rows"]:
            query = parse_qs(urlparse(row["href"]).query)
            channel, cid = query["channel"][0], query["connection"][0]
            endpoint = "wb" if channel == "wb" else "commerce"
            response = client.get(
                f"/api/sellers/{seller}/{endpoint}/{cid}/records/{query['kind'][0]}",
                params={
                    key: value[0]
                    for key, value in query.items()
                    if key in {"stage", "status", "warehouse"}
                },
            )
            assert response.status_code == 200, response.text
            assert response.json()["total"] == row["count"]
    assert {r["channel"] for r in widgets[0]["rows"]} == {"wb", "ozon", "kit"}
    assert not client.get(f"/api/sellers/{other}/workspace").json()["connections"]
    warehouse = next(
        w
        for w in state.records.list(seller, "warehouses")
        if w["connection_id"] == ozon["id"] and w["external_id"] == "east"
    )
    layout = {
        "widgets": [
            {
                "id": "ready",
                "kind": "shipping",
                "channel": "ozon",
                "warehouse_id": warehouse["id"],
                "status": "awaiting_deliver",
            }
        ]
    }
    response = client.put(
        f"/api/sellers/{seller}/settings/workspace.layout", json={"value": layout}
    )
    assert response.status_code == 200
    row = client.get(f"/api/sellers/{seller}/workspace").json()["widgets"][0]["rows"][0]
    assert row["count"] == 1 and row["examples"][0]["number"] == "ready-east"
    assert parse_qs(urlparse(row["href"]).query)["warehouse"] == ["east"]
    assert (
        client.get(f"/api/sellers/{other}/commerce/{ozon['id']}/records/orders").status_code == 404
    )


def test_layout_rejects_foreign_warehouse_and_preferences_are_organization_scoped(ui):
    state, client, seller, other, *_ = ui
    warehouse = state.records.list(seller, "warehouses")[0]
    root = f"/api/sellers/{other}/settings/"
    widget = {"id": "one", "kind": "assembly", "warehouse_id": warehouse["id"]}
    assert (
        client.put(root + "workspace.layout", json={"value": {"widgets": [widget]}}).status_code
        == 422
    )
    widget.pop("warehouse_id")
    assert (
        client.put(
            root + "workspace.layout", json={"value": {"widgets": [widget, widget]}}
        ).status_code
        == 422
    )
    assert client.put(root + "workspace.layout", json={"value": {"widgets": []}}).status_code == 200
    assert client.get(f"/api/sellers/{other}/workspace").json()["widgets"] == []
    assert len(client.get(f"/api/sellers/{seller}/workspace").json()["widgets"]) == 3
    assert (
        client.put(
            root + "application.preferences", json={"value": {"refresh_seconds": 0}}
        ).status_code
        == 422
    )
    assert (
        client.put(root + "printing.preferences", json={"value": {"width_mm": 500}}).status_code
        == 422
    )


def test_organization_rename_preserves_connections_catalog_and_external_identity(ui):
    state, client, seller, other, *_ = ui
    before = state.connections.list(seller)
    products = state.records.list(seller, "products")
    response = client.put(
        f"/api/sellers/{seller}/organization",
        json={"name": "Renamed", "legal_name": "Legal name", "tin": "123456789012"},
    )
    assert response.status_code == 200
    assert state.sellers.get(seller)["name"] == "Renamed"
    assert state.connections.list(seller) == before
    assert state.records.list(seller, "products") == products
    assert state.sellers.get(other)["name"] == "Other organization"
    assert client.put(f"/api/sellers/{seller}/organization", json={"name": "  "}).status_code == 422


def test_settings_move_credentials_out_of_operational_pages_and_check_does_not_write(ui):
    state, client, seller, other, chz, wb, ozon, kit = ui
    before = state.operations.list(seller)
    for connection in (chz, wb, ozon, kit):
        response = client.post(
            f"/api/sellers/{seller}/connections/{connection['id']}/check", json={}
        )
        assert response.status_code == 200, response.text
        assert (
            client.post(
                f"/api/sellers/{other}/connections/{connection['id']}/check", json={}
            ).status_code
            == 404
        )
    assert state.operations.list(seller) == before
    settings = client.get(f"/sellers/{seller}/settings?tab=integrations").text
    for key in ("wb", "ozon", "kit", "chz"):
        assert f'id="settings-connect-{key}"' in settings
    for channel in ("wb", "ozon", "kit"):
        page = client.get(f"/sellers/{seller}/sales?channel={channel}").text
        assert 'type="password"' not in page
    assert 'type="password"' not in client.get(f"/sellers/{seller}/marking").text
    assert "private-key" not in settings and "private-token" not in settings


def test_consistent_backup_and_test_label_have_no_marking_side_effects(ui):
    state, client, seller, other, *_ = ui
    root = f"/api/sellers/{seller}"
    before = state.operations.list(seller)
    assert (
        client.put(
            root + "/settings/printing.preferences",
            json={"value": {"width_mm": 58, "height_mm": 40}},
        ).status_code
        == 200
    )
    assert "58.0mm 40.0mm" in client.get(root + "/printing/style.css").text
    assert client.get(f"/sellers/{seller}/printing/test").status_code == 200
    response = client.post(root + "/backup", json={})
    assert response.status_code == 201
    name = response.json()["filename"]
    with sqlite3.connect(state.workspace.backup_path(name)) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert {r[0] for r in conn.execute("SELECT id FROM sellers")} == {seller, other}
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 8
    assert client.get(root + "/backup/" + name).content.startswith(b"SQLite format 3")
    assert client.get(root + "/backup/flow-20260101T010101-00000000.sqlite3").status_code == 404
    assert state.operations.list(seller) == before
