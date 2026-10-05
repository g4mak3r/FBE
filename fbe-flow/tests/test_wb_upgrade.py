import json
import sqlite3
from pathlib import Path
from uuid import uuid4

import fbe_flow.core.database as database_module
from fbe_flow.core.database import Database


def test_stage3_upgrade_preserves_full_km_journal_and_inflight_job(tmp_path):
    db = Database(tmp_path / "flow.sqlite3")
    migrations = Path(database_module.__file__).parent / "migrations"
    seller, connection, code_id, document, job = (str(uuid4()) for _ in range(5))
    cis = "010000000000000121SERIAL000001"
    full = cis + "\x1d91KEY1\x1d92EXACT-CRYPTO"
    config = json.dumps(
        {
            "inn": "123456789012",
            "credential_ref": f"{seller}:{uuid4()}",
            "environment": "production",
        }
    )
    body = json.dumps({"original": "Сохранить"})
    with db.connection() as conn:
        conn.executescript(
            "BEGIN IMMEDIATE;\n"
            + "\n".join(
                p.read_text(encoding="utf-8") for p in sorted(migrations.glob("00[1-4]_*.sql"))
            )
            + "\nPRAGMA user_version=4;\nCOMMIT;"
        )
        conn.execute("INSERT INTO sellers(id,name) VALUES (?,?)", (seller, "Existing seller"))
        conn.execute(
            "INSERT INTO connections(id,seller_id,adapter_key,name,external_account"
            "_id,config_json,operations_json) VALUES (?,?,?,?,?,?,?)",
            (connection, seller, "chz", "Existing ChZ", "production:123456789012", config, "[]"),
        )
        conn.execute(
            "INSERT INTO marking_documents(id,seller_id,connection_id,kind,title,bo"
            "dy_json,digest,state,external_id) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                document,
                seller,
                connection,
                "true",
                "Original document",
                body,
                "old-digest",
                "accepted",
                str(uuid4()),
            ),
        )
        conn.execute(
            "INSERT INTO marking_codes(id,seller_id,connection_id,code,full_code,pr"
            "oduct_group,gtin,external_status) VALUES (?,?,?,?,?,?,?,?)",
            (
                code_id,
                seller,
                connection,
                cis,
                full,
                "original-group",
                "00000000000001",
                "INTRODUCED",
            ),
        )
        conn.execute(
            "INSERT INTO operations(id,seller_id,connection_id,operation_key,payloa"
            "d_json,status,scope_key) VALUES (?,?,?,?,?,?,?)",
            (
                job,
                seller,
                connection,
                "document.poll",
                json.dumps({"document_id": document}),
                "running",
                document,
            ),
        )
    db.initialize()
    with db.connection() as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 7
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert (
            conn.execute("SELECT full_code FROM marking_codes WHERE id=?", (code_id,)).fetchone()[0]
            == full
        )
        assert conn.execute(
            "SELECT body_json,state FROM marking_documents WHERE id=?", (document,)
        ).fetchone()[:] == (body, "accepted")
        assert conn.execute(
            "SELECT status,scope_key FROM operations WHERE id=?", (job,)
        ).fetchone()[:] == ("running", document)
        assert (
            conn.execute(
                "SELECT config_json FROM connections WHERE id=?", (connection,)
            ).fetchone()[0]
            == config
        )
        assert conn.execute("SELECT count(*) FROM wb_actions").fetchone()[0] == 0
    with sqlite3.connect(tmp_path / "flow.before-v6.sqlite3") as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 4
        assert (
            conn.execute("SELECT full_code FROM marking_codes WHERE id=?", (code_id,)).fetchone()[0]
            == full
        )
    db.initialize()
    assert len(list(tmp_path.glob("flow.before-v*.sqlite3"))) == 1
