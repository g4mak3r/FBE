from __future__ import annotations

from datetime import datetime, timezone
from concurrent.futures import Future
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import FastAPI
from fastapi.testclient import TestClient

from catalog.repository import ensure_catalog_schema, save_product
from catalog.routes import build_catalog_router
from economy.repository import ensure_economy_schema
from economy.routes import build_economy_router
import app as fbe
import marking_db


def test_catalog_and_economy_routes_receive_build_globals(tmp_path):
    db = ensure_catalog_schema(tmp_path / "module-smoke.db")
    templates_dir = Path(__file__).resolve().parents[1] / "templates"
    globals_ = {"fbe_version": "TEST", "fbe_build_id": "test-build-12345678"}

    app = FastAPI()
    app.include_router(build_catalog_router(
        db_path=db,
        templates_dir=templates_dir,
        config={},
        template_globals=globals_,
    ))
    app.include_router(build_economy_router(
        db_path=db,
        templates_dir=templates_dir,
        config={},
        template_globals=globals_,
    ))

    client = TestClient(app)
    catalog = client.get("/catalog")
    economy = client.get("/economy")

    assert catalog.status_code == 200
    assert economy.status_code == 200
    assert "TEST" in catalog.text and "12345678" in catalog.text
    assert "TEST" in economy.text and "12345678" in economy.text


def test_application_has_no_duplicate_method_path_routes():
    seen = set()
    duplicates = []
    for route in fbe.app.routes:
        for method in sorted(getattr(route, "methods", set()) or set()):
            key = (method, getattr(route, "path", ""))
            if key in seen:
                duplicates.append(key)
            seen.add(key)
    assert duplicates == []


def test_economy_sync_returns_immediately_as_background_job(tmp_path):
    db = marking_db.ensure_database(tmp_path / "economy-job.db")
    templates_dir = Path(__file__).resolve().parents[1] / "templates"
    submitted = []

    def submit_job(title, worker, **kwargs):
        submitted.append((title, worker, kwargs))
        return "job-smoke"

    app = FastAPI()
    app.include_router(build_economy_router(
        db_path=db,
        templates_dir=templates_dir,
        config={},
        submit_job=submit_job,
        template_globals={"fbe_version": "TEST", "fbe_build_id": "test-build"},
    ))

    response = TestClient(app).post("/economy/sync", follow_redirects=False)
    assert response.status_code == 303
    assert "job_id=job-smoke" in response.headers["location"]
    assert len(submitted) == 1
    assert submitted[0][2]["operation_key"] == "economy-sync"


def test_catalog_cost_history_is_atomic_and_economy_get_is_read_only(tmp_path):
    db = ensure_economy_schema(ensure_catalog_schema(tmp_path / "cost-history.db"))
    templates_dir = Path(__file__).resolve().parents[1] / "templates"

    save_product(db, {
        "seller_article": "COST-1",
        "product": "Test",
        "cost_price_rub": 100,
        "active": True,
    })
    save_product(db, {
        "seller_article": "COST-1",
        "product": "Test",
        "cost_price_rub": 100,
        "active": True,
    })
    with sqlite3.connect(db) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM economy_cost_history WHERE seller_article='COST-1'"
        ).fetchone()[0] == 1

    save_product(db, {
        "seller_article": "COST-1",
        "product": "Test",
        "cost_price_rub": 120,
        "active": True,
    })
    with sqlite3.connect(db) as conn:
        before = conn.execute("SELECT COUNT(*) FROM economy_cost_history").fetchone()[0]

    local_app = FastAPI()
    local_app.include_router(build_economy_router(
        db_path=db,
        templates_dir=templates_dir,
        config={},
        template_globals={"fbe_version": "TEST", "fbe_build_id": "test-build"},
    ))
    assert TestClient(local_app).get("/economy").status_code == 200
    with sqlite3.connect(db) as conn:
        after = conn.execute("SELECT COUNT(*) FROM economy_cost_history").fetchone()[0]
        latest = conn.execute(
            "SELECT cost_price_rub FROM economy_cost_history "
            "WHERE seller_article='COST-1' ORDER BY id DESC LIMIT 1"
        ).fetchone()[0]
    assert before == after
    assert latest == 120


def test_primary_get_pages_render_without_external_network(tmp_path, monkeypatch):
    db = marking_db.ensure_database(tmp_path / "page-smoke.db")
    monkeypatch.setattr(fbe, "marking_db_path", db)

    class NoopExecutor:
        def submit(self, *args, **kwargs):
            future = Future()
            future.set_result(None)
            return future

    # Rendering smoke tests are hermetic even if a future page starts a new SWR
    # cache. No background loader is allowed to outlive the monkeypatch fixture.
    monkeypatch.setattr(fbe, "_executor", lambda kind: NoopExecutor())
    monkeypatch.setattr(fbe, "get_new_orders_cached", lambda: [])
    monkeypatch.setattr(
        fbe,
        "get_fbs_operational_sync_cached",
        lambda: {"local": True, "errors": []},
    )
    monkeypatch.setattr(
        fbe,
        "get_supply_details_cached",
        lambda supply_id: {
            "id": supply_id, "name": "Smoke", "done": False,
            "createdAt": datetime.now(timezone.utc).isoformat(),
        },
    )
    monkeypatch.setattr(fbe, "supply_orders_context", lambda supply_id, sort=None: [])
    monkeypatch.setattr(
        fbe,
        "_supply_shipping_context",
        lambda supply_id, order_ids=None, remote=False: {
            "label": "На сборке", "class": "status-assembling", "stage": "assembly",
            "order_ids": [], "order_count": 0, "statuses": [], "trbx_ids": [],
            "trbx_count": 0, "trbx_max": 1, "status_error": "", "trbx_error": "",
        },
    )

    with TestClient(fbe.app) as client:
        checks = {
            "/": "FBE",
            "/marking": "Честный",
            "/manual-print": "печ",
            "/supplies/SMOKE": "Smoke",
            "/supplies/SMOKE/pdf-mixer": "PDF",
            "/marking/post-sale": "КИЗ",
        }
        for path, marker in checks.items():
            response = client.get(path)
            assert response.status_code == 200, path
            assert marker.lower() in response.text.lower(), path

        info = client.get("/api/fbe-info")
        assert info.status_code == 200
        assert info.json()["app"] == "FBE"
