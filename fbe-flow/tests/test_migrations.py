import json
from pathlib import Path

import pytest

import fbe_flow.core.database as database_module
from fbe_flow.core.database import Database
from fbe_flow.core.errors import Conflict
from fbe_flow.core.integrations import AdapterRegistry
from fbe_flow.modules.connections import Connections
from fbe_flow.modules.operations import Operations
from fbe_flow.modules.records import KINDS, Records
from fbe_flow.modules.sellers import Sellers
from fbe_flow.modules.settings import Settings

from .conftest import FixtureAdapter, normalized_batch


def test_upgrade_from_v1_preserves_records_queue_and_success_counts(tmp_path):
    db = Database(tmp_path / "legacy.sqlite3")
    initial = Path(database_module.__file__).parent / "migrations" / "001_foundation.sql"
    with db.connection() as conn:
        conn.executescript(
            f"BEGIN IMMEDIATE;\n{initial.read_text(encoding='utf-8')}\n"
            "PRAGMA user_version = 1;\nCOMMIT;"
        )
    first = Sellers(db).create("Legacy seller")["id"]
    second = Sellers(db).create("Other seller")["id"]
    registry = AdapterRegistry([FixtureAdapter()])
    connection = Connections(db, registry).create(
        first, "test-fixture", "Legacy connection", {"account": "legacy-account"}
    )["id"]
    Records(db).apply(first, connection, normalized_batch())
    Settings(db).set(first, "legacy-key", {"kept": True})
    original_records = {kind: Records(db).list(first, kind) for kind in KINDS}
    with db.connection() as conn:
        for status in ("queued", "running", "succeeded", "failed", "interrupted"):
            conn.execute(
                "INSERT INTO operations(id, seller_id, connection_id, operation_key, payload_json, "
                "status, result_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    status,
                    first,
                    connection,
                    "fetch" if status == "queued" else status,
                    json.dumps({"preserved": status}),
                    status,
                    json.dumps(dict.fromkeys(KINDS, 1)) if status == "succeeded" else None,
                ),
            )
    db.initialize()
    operations = Operations(db, registry)
    after = operations.list(first)
    assert {job["id"]: job["status"] for job in after} == {
        status: status for status in ("queued", "running", "succeeded", "failed", "interrupted")
    }
    assert all(job["scope_key"] is None for job in after)
    assert all(job["payload"] == {"preserved": job["status"]} for job in after)
    assert operations.get(first, "succeeded")["result"] == {
        "counts": dict.fromkeys(KINDS, 1),
        "data": {},
    }
    for kind in KINDS:
        assert Records(db).list(first, kind) == original_records[kind]
        assert Records(db).list(second, kind) == []
    assert Settings(db).list(first) == {"legacy-key": {"kept": True}}
    assert operations.list(second) == []
    with pytest.raises(Conflict):
        operations.enqueue(first, connection, "fetch", {})
    scoped = operations.enqueue(first, connection, "fetch", {}, scope_key="specific-target")
    assert scoped["scope_key"] == "specific-target"
    with db.connection() as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 3
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    # Reopening an upgraded DB must neither wrap the results twice nor replay any job.
    db.initialize()
    assert operations.get(first, "succeeded")["result"] == {
        "counts": dict.fromkeys(KINDS, 1),
        "data": {},
    }
    assert operations.get(first, "running")["status"] == "running"
