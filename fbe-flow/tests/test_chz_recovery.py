import json
import sqlite3
from pathlib import Path
from uuid import uuid4

from fbe_flow.core import database as db_module
from fbe_flow.core.database import Database


def test_manual_token_can_be_rotated_and_old_secret_is_removed(workspace):
    state, client, seller, connection, provider, vault, _ = workspace
    old_ref = connection["config"]["credential_ref"]
    response = client.put(
        f"/api/sellers/{seller}/marking/{connection['id']}/credentials",
        json={"true_token": "new-secret"},
    )
    assert response.status_code == 200
    assert "new-secret" not in response.text and "credential_ref" not in response.text
    updated = state.connections.get(seller, connection["id"])
    assert old_ref not in vault.values
    assert vault.get(seller, updated["config"]["credential_ref"])["true_token"] == "new-secret"


def test_credential_rotation_waits_for_queued_jobs_and_does_not_delete_old_secret(workspace):
    state, client, seller, connection, _, vault, _ = workspace
    state.marking.start_sync(seller, connection["id"])
    old_refs = set(vault.values)
    response = client.put(
        f"/api/sellers/{seller}/marking/{connection['id']}/credentials",
        json={"true_token": "new-secret"},
    )
    assert response.status_code == 422 and set(vault.values) == old_refs


def test_failed_connect_does_not_leave_credentials_or_leak_token(workspace):
    state, client, seller, _, provider, vault, _ = workspace
    old_refs = set(vault.values)
    response = client.post(
        f"/api/sellers/{seller}/marking/connections",
        json={"name": "Wrong account", "inn": "999999999999", "true_token": "connection-secret"},
    )
    assert response.status_code == 422 and "connection-secret" not in response.text
    assert set(vault.values) == old_refs


def test_upgrade_from_v2_backs_up_and_preserves_existing_identity_and_scope(tmp_path):
    database = Database(tmp_path / "flow.sqlite3")
    directory = Path(db_module.__file__).parent / "migrations"
    with database.connection() as conn:
        conn.executescript(
            "BEGIN IMMEDIATE;\n"
            + (directory / "001_foundation.sql").read_text()
            + (directory / "002_operation_results_and_scope.sql").read_text()
            + "\nPRAGMA user_version=2;\nCOMMIT;"
        )
        conn.execute("INSERT INTO sellers(id,name) VALUES (?,?)", (str(uuid4()), "Preserved"))
    database.initialize()
    backup_path = tmp_path / "flow.before-v6.sqlite3"
    assert backup_path.exists()
    with sqlite3.connect(backup_path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
        assert conn.execute("SELECT name FROM sellers").fetchone()[0] == "Preserved"
    with database.connection() as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 7
        assert conn.execute("SELECT name FROM sellers").fetchone()[0] == "Preserved"
    before = backup_path.read_bytes()
    database.initialize()
    assert backup_path.read_bytes() == before


def test_upgrade_from_stage1_v3_preserves_cards_connection_refs_and_queue(tmp_path):
    database = Database(tmp_path / "flow.sqlite3")
    migrations = Path(db_module.__file__).parent / "migrations"
    seller, connection, product, operation = (str(uuid4()) for _ in range(4))
    config = json.dumps({"credential_ref": f"{seller}:existing-ref", "inn": "123456789012"})
    with database.connection() as conn:
        conn.executescript(
            "BEGIN IMMEDIATE;\n"
            + "\n".join(
                (migrations / name).read_text(encoding="utf-8")
                for name in (
                    "001_foundation.sql",
                    "002_operation_results_and_scope.sql",
                    "003_chz_catalog.sql",
                )
            )
            + "\nPRAGMA user_version=3;\nCOMMIT;"
        )
        conn.execute("INSERT INTO sellers(id,name) VALUES (?,?)", (seller, "Preserved"))
        conn.execute(
            "INSERT INTO connections(id,seller_id,adapter_key,name,external_account_id,"
            "config_json,operations_json) VALUES (?,?,?,?,?,?,?)",
            (connection, seller, "chz", "Account", "sandbox:123456789012", config, "[]"),
        )
        conn.execute(
            "INSERT INTO products(id,seller_id,connection_id,external_id,title,category_json,"
            "identifiers_json,attributes_json) VALUES (?,?,?,?,?,?,?,?)",
            (product, seller, connection, "1", "Original", "{}", "{}", '{"own_card":true}'),
        )
        conn.execute(
            "INSERT INTO nk_cards(seller_id,connection_id,external_id,etag,detail_available,"
            "present,last_seen_run) VALUES (?,?,?,?,?,?,?)",
            (seller, connection, "1", "original-etag", 1, 1, "original-run"),
        )
        conn.execute(
            "INSERT INTO operations(id,seller_id,connection_id,operation_key,payload_json,"
            "scope_key) VALUES (?,?,?,?,?,?)",
            (operation, seller, connection, "nk.references", "{}", "original-scope"),
        )
    database.initialize()
    with database.connection() as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 7
        assert (
            dict(conn.execute("SELECT * FROM products WHERE id=?", (product,)).fetchone())["title"]
            == "Original"
        )
        assert (
            conn.execute(
                "SELECT config_json FROM connections WHERE id=?", (connection,)
            ).fetchone()[0]
            == config
        )
        assert tuple(
            conn.execute("SELECT etag,detail_available,present FROM nk_cards").fetchone()
        ) == ("original-etag", 1, 1)
        assert tuple(
            conn.execute(
                "SELECT status,scope_key FROM operations WHERE id=?", (operation,)
            ).fetchone()
        ) == ("queued", "original-scope")
        assert not conn.execute("PRAGMA foreign_key_check").fetchall()
    with sqlite3.connect(tmp_path / "flow.before-v6.sqlite3") as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 3
        assert conn.execute("SELECT id FROM products").fetchone()[0] == product


def test_interrupted_page_exposes_status_and_can_restart(workspace):
    state, _, seller, connection, _, _, _ = workspace
    job = state.marking.start_sync(seller, connection["id"])
    assert state.operations.claim()["id"] == job["id"]
    state.operations.recover_interrupted()
    value = state.marking.overview(seller, connection["id"])["snapshots"]["sync"]["value"]
    assert value["state"] == "interrupted"
    state.marking.start_sync(seller, connection["id"])
    assert state.operations.run_once()
    assert state.marking.products(seller, connection["id"])["total"] == 1


def test_page_records_index_and_continuation_commit_atomically(workspace):
    state, _, seller, connection, _, _, _ = workspace
    original = state.operations.result_handler

    def fail_after_result(conn, job, result):
        original(conn, job, result)
        raise RuntimeError("Simulated transaction failure")

    state.operations.result_handler = fail_after_result
    job = state.marking.start_sync(seller, connection["id"])
    state.operations.run_once()
    assert state.operations.get(seller, job["id"])["status"] == "failed"
    assert state.marking.products(seller, connection["id"])["total"] == 0
    with state.database.connection() as conn:
        assert conn.execute("SELECT count(*) FROM products").fetchone()[0] == 0
        assert (
            conn.execute("SELECT count(*) FROM operations WHERE status='queued'").fetchone()[0] == 0
        )
    assert "last_sync" not in state.marking.overview(seller, connection["id"])["snapshots"]


def test_raw_sync_payload_cannot_interfere_with_valid_run(workspace):
    state, _, seller, connection, _, _, _ = workspace
    job = state.marking.start_sync(seller, connection["id"])
    raw = state.operations.enqueue(seller, connection["id"], "nk.sync", {"run_id": "forged"}, "raw")
    state.operations.run_once()
    state.operations.run_once()
    assert state.operations.get(seller, job["id"])["status"] == "succeeded"
    assert state.operations.get(seller, raw["id"])["status"] == "failed"
    assert (
        state.marking.overview(seller, connection["id"])["snapshots"]["sync"]["value"]["state"]
        == "succeeded"
    )


def test_connect_with_token_queues_references_and_sync_http_endpoint_works(workspace):
    state, client, _, _, provider, _, _ = workspace
    seller = state.sellers.create("New seller")["id"]
    root = f"/api/sellers/{seller}/marking"
    response = client.post(
        root + "/connections",
        json={"name": "New account", "inn": provider.inn, "true_token": "ui-test-secret"},
    )
    assert response.status_code == 201
    assert "ui-test-secret" not in response.text and "config" not in response.text
    connection = response.json()["id"]
    state.operations.run_once()
    assert (
        client.get(root + f"/{connection}/overview").json()["snapshots"]["account"]["value"]["inn"]
        == provider.inn
    )
    assert client.post(root + f"/{connection}/sync", json={}).status_code == 202
    assert client.post(root + f"/{connection}/sync", json={}).status_code == 409
    state.operations.run_once()
    assert client.get(root + f"/{connection}/products").json()["total"] == 1
    assert client.get(root + f"/{connection}/products?offset=-1").status_code == 422
    assert client.get(root + f"/{connection}/products?limit=201").status_code == 422
