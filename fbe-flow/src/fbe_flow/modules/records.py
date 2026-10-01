import json
import sqlite3
from uuid import uuid4

from fbe_flow.core.database import Database
from fbe_flow.core.errors import InvalidInput, NotFound
from fbe_flow.core.models import NormalizedBatch
from fbe_flow.modules.connections import require_connection
from fbe_flow.modules.sellers import require_seller

# Only structural module names; never external categories/statuses/warehouse IDs.
KINDS = ("products", "warehouses", "supplies", "orders")


def decode_record(row: sqlite3.Row) -> dict:
    result = dict(row)
    for key in tuple(result):
        if key.endswith("_json"):
            result[key[:-5]] = json.loads(result.pop(key))
    return result


def require_kind(kind: str) -> None:
    if kind not in KINDS:
        raise InvalidInput("Неизвестный модуль")


class Records:
    """Foundation persistence for four small normalized models, not a catalog service."""

    def __init__(self, database: Database):
        self.db = database

    def list(self, seller_id: str, kind: str, limit: int = 100) -> list[dict]:
        require_kind(kind)
        if not 1 <= limit <= 100:
            raise InvalidInput("Размер страницы: от 1 до 100")
        with self.db.connection() as conn:
            require_seller(conn, seller_id)
            return [
                decode_record(row)
                for row in conn.execute(
                    f"SELECT * FROM {kind} WHERE seller_id = ? ORDER BY external_id, id LIMIT ?",
                    (seller_id, limit),
                )
            ]

    def get(self, seller_id: str, kind: str, record_id: str) -> dict:
        require_kind(kind)
        with self.db.connection() as conn:
            row = conn.execute(
                f"SELECT * FROM {kind} WHERE seller_id = ? AND id = ?", (seller_id, record_id)
            ).fetchone()
            if row is None:
                raise NotFound("Запись не найдена")
            return decode_record(row)

    def apply(self, seller_id: str, connection_id: str, batch: NormalizedBatch) -> dict[str, int]:
        with self.db.connection() as conn:
            return self.apply_in_transaction(conn, seller_id, connection_id, batch)

    def apply_in_transaction(
        self, conn: sqlite3.Connection, seller_id: str, connection_id: str, batch: NormalizedBatch
    ) -> dict[str, int]:
        require_connection(conn, seller_id, connection_id)
        batch = NormalizedBatch.model_validate(batch)
        # Referenced rows precede their dependants. Any failure rolls back the entire batch.
        for kind in ("warehouses", "products", "supplies", "orders"):
            items = getattr(batch, kind)
            seen = set()
            for item in items:
                if item.external_id in seen:
                    raise InvalidInput("Повторяющийся внешний ID в пакете")
                seen.add(item.external_id)
                values = item.model_dump()
                for key in ("category", "identifiers", "attributes"):
                    if key in values:
                        values[key + "_json"] = json.dumps(
                            values.pop(key), ensure_ascii=False, allow_nan=False
                        )
                columns = ["id", "seller_id", "connection_id", *values]
                update = ", ".join(
                    f"{key} = excluded.{key}" for key in values if key != "external_id"
                )
                conn.execute(
                    f"INSERT INTO {kind} ({', '.join(columns)}) "
                    f"VALUES ({', '.join('?' for _ in columns)}) "
                    f"ON CONFLICT(seller_id, connection_id, external_id) DO UPDATE SET {update}, "
                    "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')",
                    (str(uuid4()), seller_id, connection_id, *values.values()),
                )
        return {kind: len(getattr(batch, kind)) for kind in KINDS}
