"""Seller-scoped local National Catalogue views and durable paginated read orchestration."""

import json
from uuid import uuid4

from fbe_flow.core.errors import Conflict, InvalidInput
from fbe_flow.modules.connections import require_connection
from fbe_flow.modules.records import decode_record


def canonical(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


class Marking:
    def __init__(self, database, connections, operations, registry):
        self.db = database
        self.connections = connections
        self.operations = operations
        self.registry = registry

    def _connection(self, seller, connection):
        value = self.connections.get(seller, connection)
        if value["adapter_key"] != "chz":
            raise InvalidInput("Выберите подключение Честного Знака")
        return value

    def overview(self, seller, connection):
        self._connection(seller, connection)
        with self.db.connection() as conn:
            snapshots = {
                r["key"]: {"value": json.loads(r["value_json"]), "updated_at": r["updated_at"]}
                for r in conn.execute(
                    "SELECT * FROM marking_snapshots WHERE seller_id=? AND connection_id=?",
                    (seller, connection),
                )
            }
        return {"snapshots": snapshots}

    def products(self, seller, connection, offset=0, limit=100, search=""):
        self._connection(seller, connection)
        if not 0 <= offset or not 1 <= limit <= 200 or len(search) > 150:
            raise InvalidInput("Неверные параметры страницы")
        pattern = "%" + search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        predicate = (
            "n.seller_id=? AND n.connection_id=? AND (COALESCE(p.title,'') LIKE ? ESCAPE '\\' "
            "OR COALESCE(p.identifiers_json,'') LIKE ? ESCAPE '\\' "
            "OR n.external_id LIKE ? ESCAPE '\\')"
        )
        args = (seller, connection, pattern, pattern, pattern)
        source = (
            " FROM nk_cards n LEFT JOIN products p ON p.seller_id=n.seller_id "
            "AND p.connection_id=n.connection_id AND p.external_id=n.external_id WHERE "
        )
        with self.db.connection() as conn:
            total = conn.execute("SELECT count(*)" + source + predicate, args).fetchone()[0]
            rows = []
            for r in conn.execute(
                "SELECT p.*,n.external_id AS source_id,n.present,n.detail_available"
                + source
                + predicate
                + " ORDER BY CAST(n.external_id AS INTEGER) LIMIT ? OFFSET ?",
                (*args, limit, offset),
            ):
                value = (
                    decode_record(r)
                    if r["id"]
                    else {
                        "id": None,
                        "external_id": r["source_id"],
                        "title": f"Карточка НК #{r['source_id']}",
                        "identifiers": {},
                        "category": {},
                        "attributes": {},
                        "updated_at": None,
                    }
                )
                value.update(
                    present=bool(r["present"]), detail_available=bool(r["detail_available"])
                )
                rows.append(value)
        return {"items": rows, "total": total, "offset": offset, "limit": limit}

    def execute(self, adapter, context, key, payload):
        if adapter.key == "chz" and key == "nk.sync":
            with self.db.connection() as conn:
                snapshot = conn.execute(
                    "SELECT value_json FROM marking_snapshots WHERE seller_id=? "
                    "AND connection_id=? AND key='sync'",
                    (context.seller_id, context.connection_id),
                ).fetchone()
                if (
                    not snapshot
                    or not payload.get("run_id")
                    or (json.loads(snapshot["value_json"]).get("run_id") != payload["run_id"])
                ):
                    raise InvalidInput("Запустите синхронизацию из раздела Честный Знак")
                known = {
                    r["external_id"]: r["etag"]
                    for r in conn.execute(
                        "SELECT external_id,etag FROM nk_cards WHERE seller_id=? "
                        "AND connection_id=? AND detail_available=1 AND present=1",
                        (context.seller_id, context.connection_id),
                    )
                }
            return adapter.execute(context, key, {**payload, "known_etags": known})
        return adapter.execute(context, key, payload)

    def start_sync(self, seller, connection, force=False):
        self._connection(seller, connection)
        with self.db.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            active = conn.execute(
                "SELECT 1 FROM operations WHERE seller_id=? AND connection_id=? "
                "AND operation_key='nk.sync' AND status IN ('queued','running')",
                (seller, connection),
            ).fetchone()
            if active:
                raise Conflict("Синхронизация этого подключения уже выполняется")
            run = str(uuid4())
            operation_id = self._queue(
                conn,
                seller,
                connection,
                "nk.sync",
                {"offset": 0, "force": force, "run_id": run},
                f"sync:{run}:0",
            )
            self._snapshot(conn, seller, connection, "sync", {"state": "queued", "run_id": run})
        return self.operations.get(seller, operation_id)

    @staticmethod
    def _queue(conn, seller, connection, key, payload, scope):
        operation_id = str(uuid4())
        conn.execute(
            "INSERT INTO operations(id,seller_id,connection_id,operation_key,"
            "payload_json,scope_key) "
            "VALUES (?,?,?,?,?,?)",
            (operation_id, seller, connection, key, canonical(payload), scope),
        )
        return operation_id

    @staticmethod
    def _snapshot(conn, seller, connection, key, value):
        conn.execute(
            "INSERT INTO marking_snapshots(seller_id,connection_id,key,value_json) "
            "VALUES (?,?,?,?) ON CONFLICT(seller_id,connection_id,key) "
            "DO UPDATE SET value_json=excluded.value_json, "
            "updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')",
            (seller, connection, key, canonical(value)),
        )

    def apply_result(self, conn, job, result):
        connection = require_connection(conn, job["seller_id"], job["connection_id"])
        if connection["adapter_key"] != "chz":
            return
        seller, connection_id = job["seller_id"], job["connection_id"]
        for key, value in result.data.get("snapshots", {}).items():
            self._snapshot(conn, seller, connection_id, key, value)
        if "lookup" in result.data:
            self._snapshot(conn, seller, connection_id, "lookup", result.data["lookup"])
        if progress := result.data.get("sync"):
            complete = progress["complete"]
            for card in progress["cards"]:
                seen = conn.execute(
                    "SELECT 1 FROM nk_cards WHERE seller_id=? AND connection_id=? "
                    "AND external_id=? AND last_seen_run=?",
                    (seller, connection_id, card["external_id"], progress["run_id"]),
                ).fetchone()
                if seen:
                    raise Conflict(
                        "Каталог изменился во время обхода; запустите синхронизацию заново"
                    )
                conn.execute(
                    "INSERT INTO nk_cards(seller_id,connection_id,external_id,etag,"
                    "last_seen_run,present,detail_available) VALUES (?,?,?,?,?,1,?) "
                    "ON CONFLICT(seller_id,connection_id,external_id) DO UPDATE SET "
                    "etag=excluded.etag,last_seen_run=excluded.last_seen_run,present=1,"
                    "detail_available=excluded.detail_available",
                    (
                        seller,
                        connection_id,
                        card["external_id"],
                        card["etag"],
                        progress["run_id"],
                        int(card["detail_available"]),
                    ),
                )
            if complete:
                conn.execute(
                    "UPDATE nk_cards SET present=0 WHERE seller_id=? AND connection_id=? "
                    "AND last_seen_run<>?",
                    (seller, connection_id, progress["run_id"]),
                )
                self._snapshot(
                    conn,
                    seller,
                    connection_id,
                    "last_sync",
                    {"run_id": progress["run_id"], "total": progress["total"]},
                )
            self._snapshot(
                conn,
                seller,
                connection_id,
                "sync",
                {
                    **{k: v for k, v in progress.items() if k != "cards"},
                    "state": "succeeded" if complete else "running",
                },
            )
            if not complete:
                payload = {
                    "offset": progress["offset"],
                    "force": progress["force"],
                    "run_id": progress["run_id"],
                    "expected_total": progress["total"],
                }
                self._queue(
                    conn,
                    seller,
                    connection_id,
                    "nk.sync",
                    payload,
                    f"sync:{progress['run_id']}:{progress['offset']}",
                )

    def fail(self, conn, job, code):
        connection = require_connection(conn, job["seller_id"], job["connection_id"])
        if connection["adapter_key"] == "chz" and job["operation_key"] == "nk.sync":
            row = conn.execute(
                "SELECT value_json FROM marking_snapshots WHERE seller_id=? "
                "AND connection_id=? AND key='sync'",
                (job["seller_id"], job["connection_id"]),
            ).fetchone()
            if not row or json.loads(row["value_json"]).get("run_id") != job["payload"].get(
                "run_id"
            ):
                return
            self._snapshot(
                conn,
                job["seller_id"],
                job["connection_id"],
                "sync",
                {
                    "state": "failed",
                    "offset": job["payload"].get("offset", 0),
                    "run_id": job["payload"].get("run_id"),
                    "error": code,
                },
            )

    def recover(self, conn):
        # A queued continuation is already durable. Interrupted reads require an explicit restart.
        for row in conn.execute(
            "SELECT seller_id,connection_id,value_json FROM marking_snapshots WHERE key='sync'"
        ).fetchall():
            progress = json.loads(row["value_json"])
            if progress.get("state") not in {"queued", "running"}:
                continue
            active = conn.execute(
                "SELECT 1 FROM operations WHERE seller_id=? AND connection_id=? "
                "AND operation_key='nk.sync' AND status IN ('queued','running')",
                (row["seller_id"], row["connection_id"]),
            ).fetchone()
            if not active:
                self._snapshot(
                    conn,
                    row["seller_id"],
                    row["connection_id"],
                    "sync",
                    {**progress, "state": "interrupted", "error": "process_interrupted"},
                )
