import pytest
from fastapi.testclient import TestClient

from fbe_flow.app import create_app
from fbe_flow.config import AppConfig
from fbe_flow.modules.records import KINDS

from .conftest import FixtureAdapter, normalized_batch


def create_seller(client, name):
    response = client.post("/api/sellers", json={"name": name})
    assert response.status_code == 201
    return response.json()["id"]


def create_connection(client, seller, account):
    response = client.post(
        f"/api/sellers/{seller}/connections",
        json={"adapter_key": "test-fixture", "name": "Connection", "config": {"account": account}},
    )
    assert response.status_code == 201
    assert "config" not in response.json()
    return response.json()["id"]


def test_empty_foundation_and_shell_pages(tmp_path):
    app = create_app(AppConfig(tmp_path))
    with TestClient(app, base_url="http://localhost", headers={"X-FBE-Flow": "1"}) as client:
        assert "Начните с продавца" in client.get("/").text
        assert client.get("/api/adapters").json() == [
            {"key": "chz", "label": "Честный Знак"},
            {"key": "wb", "label": "Wildberries FBS"},
            {"key": "ozon", "label": "Ozon FBS"},
            {"key": "kit", "label": "Яндекс KIT"},
        ]
        assert client.get("/health").json() == {"status": "ok", "worker": "running"}
        seller = create_seller(client, "My seller")
        for page in ("overview", "connections", "operations", "settings"):
            response = client.get(f"/sellers/{seller}/{page}")
            assert response.status_code == 200
            assert "My seller" in response.text
            assert "Content-Security-Policy" in response.headers
        assert client.get("/static/shell.js").status_code == 200
        assert client.get("/static/shell.css").status_code == 200
        worker = app.state.worker
    assert not worker.alive


def test_lifespan_worker_executes_durable_job_and_shutdown_finishes_it(tmp_path):
    adapter = FixtureAdapter()
    app = create_app(AppConfig(tmp_path), [adapter])
    with TestClient(app, base_url="http://localhost", headers={"X-FBE-Flow": "1"}) as client:
        seller = create_seller(client, "First")
        connection = create_connection(client, seller, "account")
        response = client.post(
            f"/api/sellers/{seller}/operations",
            json={"connection_id": connection, "operation_key": "fetch"},
        )
        assert response.status_code == 202
        job = response.json()
        assert adapter.called.wait(timeout=3)
    assert not app.state.worker.alive
    assert app.state.operations.get(seller, job["id"])["status"] == "succeeded"
    assert app.state.records.list(seller, "products")[0]["title"] == "account"


@pytest.mark.parametrize("kind", KINDS)
def test_http_foreign_records_are_invisible(client, kind):
    first = create_seller(client, "First")
    second = create_seller(client, "Second")
    connection = create_connection(client, second, "account")
    client.app.state.records.apply(second, connection, normalized_batch())
    base = f"/api/sellers/{first}/records/{kind}"
    foreign = client.get(f"/api/sellers/{second}/records/{kind}").json()[0]
    assert client.get(base).json() == []
    assert client.get(f"{base}/{foreign['id']}").status_code == 404


def test_http_connection_operation_settings_isolation(client):
    first = create_seller(client, "First")
    second = create_seller(client, "Second")
    connection = create_connection(client, second, "account")
    own = f"/api/sellers/{first}"
    other = f"/api/sellers/{second}"
    assert client.get(f"{own}/connections/{connection}").status_code == 404
    assert (
        client.post(
            f"{own}/operations", json={"connection_id": connection, "operation_key": "fetch"}
        ).status_code
        == 404
    )
    job = client.post(
        f"{other}/operations", json={"connection_id": connection, "operation_key": "fetch"}
    ).json()
    assert client.get(f"{own}/operations/{job['id']}").status_code == 404
    assert client.get(f"{own}/operations").json() == []
    assert client.put(f"{other}/settings/key", json={"value": {"a": 1}}).status_code == 200
    assert client.get(f"{own}/settings").json() == {}
    assert client.get(f"{other}/settings").json() == {"key": {"a": 1}}


def test_http_explicit_scopes_and_validation_preserve_seller_boundary(client):
    first = create_seller(client, "First")
    second = create_seller(client, "Second")
    connection = create_connection(client, first, "account")
    path = f"/api/sellers/{first}/operations"
    body = {"connection_id": connection, "operation_key": "fetch", "scope_key": "A"}
    one = client.post(path, json=body)
    assert one.status_code == 202
    assert one.json()["scope_key"] == "A"
    assert client.post(path, json=body).status_code == 409
    assert client.post(path, json={**body, "scope_key": "B"}).status_code == 202
    assert client.post(path, json={**body, "scope_key": "  "}).status_code == 422
    assert client.post(f"/api/sellers/{second}/operations", json=body).status_code == 404
    assert client.get(f"/api/sellers/{second}/operations").json() == []


@pytest.mark.parametrize("resource", ["connections", "settings", "operations", "records/products"])
def test_unknown_seller_is_not_an_empty_workspace(client, resource):
    assert client.get(f"/api/sellers/missing/{resource}").status_code == 404


def test_api_rejects_tenant_override(client):
    seller = create_seller(client, "First")
    response = client.put(
        f"/api/sellers/{seller}/settings/key", json={"value": 1, "seller_id": "another"}
    )
    assert response.status_code == 422


@pytest.mark.parametrize("value", ["NaN", "Infinity", "1e999", '{"nested": [NaN]}'])
def test_non_finite_json_returns_validation_error_without_persisting(client, value):
    seller = create_seller(client, "First")
    response = client.put(
        f"/api/sellers/{seller}/settings/key",
        content='{"value": ' + value + "}",
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 422
    assert "input" not in response.json()["detail"][0]
    assert client.get(f"/api/sellers/{seller}/settings").json() == {}


def test_writes_require_local_json_request(client):
    client.headers.pop("X-FBE-Flow")
    assert client.post("/api/sellers", json={"name": "First"}).status_code == 403
    client.headers["X-FBE-Flow"] = "1"
    assert (
        client.post(
            "/api/sellers", json={"name": "First"}, headers={"Origin": "https://foreign.example"}
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/api/sellers",
            content="name=First",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        ).status_code
        == 415
    )
    assert client.get("/", headers={"Host": "foreign.example"}).status_code == 400


def test_shell_escapes_external_text_and_current_seller_is_per_page(client):
    first = create_seller(client, '<script>alert("x")</script>')
    second = create_seller(client, "Second")
    a = client.get(f"/sellers/{first}/overview").text
    b = client.get(f"/sellers/{second}/overview").text
    assert '<script>alert("x")</script>' not in a
    assert "&lt;script&gt;" in a
    assert f'data-seller-id="{first}"' in a
    assert f'data-seller-id="{second}"' in b


def test_unavailable_worker_is_visible_in_health(client):
    client.app.state.config = AppConfig(client.app.state.config.data_dir, worker_enabled=True)
    response = client.get("/health")
    assert response.status_code == 503
    assert response.json()["status"] == "degraded"
