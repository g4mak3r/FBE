from __future__ import annotations

import json
import sqlite3

import pytest

from connections import ConnectionStore
from demo_mode import prepare_demo_database
from settings import Settings
from suz_client import SuzApiError, SuzClient
from wb_api import WBClient


def test_connection_store_keeps_secret_out_of_public_state(tmp_path):
    store = ConnectionStore(tmp_path)
    store.set_wb(
        token="super-secret-token",
        profile={"name": "Demo Seller", "sid": "seller-1", "tin": "000000000000", "tradeMark": "Demo"},
    )
    public = store.public_state()
    assert public["wb"]["connected"] is True
    assert public["wb"]["name"] == "Demo Seller"
    assert "token" not in public["wb"]
    assert "super-secret-token" not in json.dumps(public, ensure_ascii=False)


def test_settings_demo_mode_uses_separate_workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("WB_TOKEN", "REAL-SECRET-MUST-NOT-LEAK")
    monkeypatch.setenv("CZ_AUTH_INN", "000000000000")
    monkeypatch.delenv("FBE_DRY_RUN_PRINT", raising=False)
    (tmp_path / "config.json").write_text(json.dumps({"database_path": "data/fbe.db"}), encoding="utf-8")
    ConnectionStore(tmp_path).set_mode("demo")
    settings = Settings(tmp_path / "config.json")
    assert settings.data["mock_mode"] is True
    assert settings.data["database_path"].endswith("data/demo/fbe_demo.db")
    assert settings.data["dry_run_print"] is True
    assert settings.data["wb_token"] == "DEMO"
    assert settings.data["suz_auth_inn"] == "000000000000"


def test_demo_database_contains_only_small_synthetic_fixture(tmp_path):
    db = prepare_demo_database(tmp_path / "demo.db")
    with sqlite3.connect(db) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("SELECT COUNT(*) FROM products").fetchone()[0] == 3
        assert conn.execute("SELECT COUNT(*) FROM fbs_supply_registry").fetchone()[0] == 3
        assert conn.execute("SELECT COUNT(*) FROM marking_codes").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM economy_orders").fetchone()[0] == 3


def test_demo_clients_do_not_need_external_network(tmp_path):
    wb = WBClient(token="", mock_mode=True)
    assert wb.get_seller_info()["tradeMark"] == "DEMO"

    suz = SuzClient(base_dir=tmp_path, settings_loader=lambda: {"mock_mode": True})
    with pytest.raises(SuzApiError, match="DEMO MODE"):
        suz._request("GET", "https://suzgrid.crpt.ru/health", stage="demo")
