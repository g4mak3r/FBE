import json
import logging
import re
import sqlite3
from collections.abc import Callable
from threading import Event, Thread
from uuid import uuid4

from pydantic import JsonValue

from fbe_flow.core.database import Database
from fbe_flow.core.errors import Conflict, InvalidInput, NotFound
from fbe_flow.core.integrations import AdapterRegistry
from fbe_flow.core.models import OperationResult
from fbe_flow.modules.connections import Connections, connection_context, require_connection
from fbe_flow.modules.records import Records
from fbe_flow.modules.sellers import require_seller

logger = logging.getLogger(__name__)


def decode_operation(row: sqlite3.Row) -> dict:
    result = dict(row)
    result["payload"] = json.loads(result.pop("payload_json"))
    raw = result.pop("result_json")
    result["result"] = json.loads(raw) if raw else None
    result["scope_key"] = result["scope_key"] or None
    return result


class Operations:
    def __init__(self, database: Database, registry: AdapterRegistry):
        self.db = database
        self.registry = registry
        # Concrete contours can persist their own state without giving adapters a DB.
        self.executor: Callable | None = None
        self.result_handler: Callable | None = None
        self.recovery_handler: Callable | None = None
        self.failure_handler: Callable | None = None
        self.idle_handler: Callable | None = None

    def list(self, seller_id: str) -> list[dict]:
        with self.db.connection() as conn:
            require_seller(conn, seller_id)
            return [
                decode_operation(row)
                for row in conn.execute(
                    "SELECT * FROM operations WHERE seller_id = ? "
                    "ORDER BY created_at DESC, id LIMIT 100",
                    (seller_id,),
                )
            ]

    def get(self, seller_id: str, operation_id: str) -> dict:
        with self.db.connection() as conn:
            row = conn.execute(
                "SELECT * FROM operations WHERE seller_id = ? AND id = ?", (seller_id, operation_id)
            ).fetchone()
            if row is None:
                raise NotFound("Операция не найдена")
            return decode_operation(row)

    def enqueue(
        self,
        seller_id: str,
        connection_id: str,
        key: str,
        payload: dict[str, JsonValue],
        scope_key: str | None = None,
    ) -> dict:
        if scope_key is not None:
            scope_key = scope_key.strip()
            if not scope_key:
                raise InvalidInput("Ключ области операции не может быть пустым")
        operation_id = str(uuid4())
        try:
            with self.db.connection() as conn:
                connection = require_connection(conn, seller_id, connection_id)
                self.registry.get(connection["adapter_key"])
                if key not in {item["key"] for item in connection["operations"]}:
                    raise InvalidInput("Операция недоступна этому подключению")
                conn.execute(
                    "INSERT INTO operations(id, seller_id, connection_id, operation_key, "
                    "payload_json, scope_key) VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        operation_id,
                        seller_id,
                        connection_id,
                        key,
                        json.dumps(payload, ensure_ascii=False, allow_nan=False),
                        scope_key or "",
                    ),
                )
                row = conn.execute(
                    "SELECT * FROM operations WHERE seller_id = ? AND id = ?",
                    (seller_id, operation_id),
                ).fetchone()
                return decode_operation(row)
        except sqlite3.IntegrityError as exc:
            raise Conflict("Такая операция уже ожидает или выполняется") from exc

    def recover_interrupted(self) -> None:
        # System-wide maintenance under the exclusive instance lock, never a seller API.
        with self.db.connection() as conn:
            conn.execute(
                "UPDATE operations SET status = 'interrupted', error_code = 'process_interrupted', "
                "finished_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE status = 'running'"
            )
            if self.recovery_handler:
                self.recovery_handler(conn)

    def claim(self) -> dict | None:
        # The only cross-seller read: dispatch one job, then carry its seller_id everywhere.
        with self.db.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM operations WHERE status = 'queued' ORDER BY created_at, id LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            conn.execute(
                "UPDATE operations SET status = 'running', "
                "started_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') "
                "WHERE seller_id = ? AND id = ? AND status = 'queued'",
                (row["seller_id"], row["id"]),
            )
            result = decode_operation(row)
            result["status"] = "running"
            return result

    def run_once(self) -> bool:
        if self.idle_handler:
            self.idle_handler()
        job = self.claim()
        if job is None:
            return False
        scope = (job["seller_id"], job["id"])
        try:
            connection = Connections(self.db, self.registry).get(
                job["seller_id"], job["connection_id"]
            )
            adapter = self.registry.get(connection["adapter_key"])
            execute = self.executor or (lambda a, c, k, p: a.execute(c, k, p))
            result = OperationResult.model_validate(
                execute(
                    adapter, connection_context(connection), job["operation_key"], job["payload"]
                )
            )
            with self.db.connection() as conn:
                conn.execute("BEGIN IMMEDIATE")
                counts = None
                if result.batch is not None:
                    counts = Records(self.db).apply_in_transaction(
                        conn, job["seller_id"], job["connection_id"], result.batch
                    )
                if self.result_handler:
                    self.result_handler(conn, job, result)
                updated = conn.execute(
                    "UPDATE operations SET status = 'succeeded', result_json = ?, "
                    "finished_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') "
                    "WHERE seller_id = ? AND id = ? AND status = 'running'",
                    (
                        json.dumps(
                            {"counts": counts, "data": result.data},
                            ensure_ascii=False,
                            allow_nan=False,
                        ),
                        *scope,
                    ),
                )
                if updated.rowcount != 1:
                    raise Conflict("Состояние операции изменилось")
        except Exception as exc:
            logger.warning("Operation %s failed (%s)", job["id"], type(exc).__name__)
            with self.db.connection() as conn:
                code = getattr(exc, "code", "execution_failed")
                if not isinstance(code, str) or not re.fullmatch(r"[a-z0-9_]{1,80}", code):
                    code = "execution_failed"
                conn.execute(
                    "UPDATE operations SET status = 'failed', error_code = ?, "
                    "finished_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') "
                    "WHERE seller_id = ? AND id = ? AND status = 'running'",
                    (code, *scope),
                )
                if self.failure_handler:
                    self.failure_handler(conn, job, code)
        return True


class Worker:
    def __init__(self, operations: Operations):
        self.operations = operations
        self._stop = Event()
        self._thread: Thread | None = None

    @property
    def alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        self.operations.recover_interrupted()
        self._stop.clear()
        self._thread = Thread(target=self._run, name="fbe-flow-worker", daemon=False)
        self._thread.start()

    def _run(self) -> None:
        try:
            while not self._stop.is_set():
                if not self.operations.run_once():
                    self._stop.wait(0.5)
        except Exception as exc:
            # A persistence failure must be visible through health, not hidden in an endless loop.
            logger.error("Worker stopped (%s)", type(exc).__name__)

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            # Finish the current bounded adapter call before releasing the lock.
            self._thread.join()
