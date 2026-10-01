import json
import sqlite3
from uuid import uuid4

from pydantic import JsonValue

from fbe_flow.core.database import Database
from fbe_flow.core.errors import Conflict, InvalidInput, NotFound
from fbe_flow.core.integrations import AdapterRegistry
from fbe_flow.core.models import AccountInfo, ConnectionContext
from fbe_flow.modules.sellers import Sellers, require_seller


def decode_connection(row: sqlite3.Row) -> dict:
    result = dict(row)
    result["config"] = json.loads(result.pop("config_json"))
    result["operations"] = json.loads(result.pop("operations_json"))
    return result


def require_connection(conn: sqlite3.Connection, seller_id: str, connection_id: str) -> dict:
    row = conn.execute(
        "SELECT * FROM connections WHERE seller_id = ? AND id = ?", (seller_id, connection_id)
    ).fetchone()
    if row is None:
        raise NotFound("Подключение не найдено")
    return decode_connection(row)


def connection_context(connection: dict) -> ConnectionContext:
    return ConnectionContext(
        seller_id=connection["seller_id"],
        connection_id=connection["id"],
        external_account_id=connection["external_account_id"],
        config=connection["config"],
    )


class Connections:
    def __init__(self, database: Database, registry: AdapterRegistry):
        self.db = database
        self.registry = registry

    def list(self, seller_id: str) -> list[dict]:
        with self.db.connection() as conn:
            require_seller(conn, seller_id)
            return [
                decode_connection(row)
                for row in conn.execute(
                    "SELECT * FROM connections WHERE seller_id = ? ORDER BY created_at, id",
                    (seller_id,),
                )
            ]

    def get(self, seller_id: str, connection_id: str) -> dict:
        with self.db.connection() as conn:
            return require_connection(conn, seller_id, connection_id)

    def create(
        self, seller_id: str, adapter_key: str, name: str, config: dict[str, JsonValue]
    ) -> dict:
        Sellers(self.db).get(seller_id)
        if not name.strip() or len(name.strip()) > 120:
            raise InvalidInput("Название подключения: от 1 до 120 символов")
        adapter = self.registry.get(adapter_key)
        try:
            account = AccountInfo.model_validate(adapter.describe(config))
        except Exception as exc:
            # Adapter exceptions can contain tokens/URLs. Never expose their text.
            raise InvalidInput("Не удалось проверить подключение") from exc
        keys = [operation.key for operation in account.operations]
        if len(keys) != len(set(keys)):
            raise InvalidInput("Адаптер вернул повторяющиеся операции")
        connection_id = str(uuid4())
        try:
            with self.db.connection() as conn:
                conn.execute(
                    "INSERT INTO connections(id, seller_id, adapter_key, name, "
                    "external_account_id, "
                    "config_json, operations_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        connection_id,
                        seller_id,
                        adapter_key,
                        name.strip(),
                        account.external_account_id,
                        json.dumps(config, ensure_ascii=False, allow_nan=False),
                        json.dumps(
                            [item.model_dump() for item in account.operations], ensure_ascii=False
                        ),
                    ),
                )
                return require_connection(conn, seller_id, connection_id)
        except sqlite3.IntegrityError as exc:
            raise Conflict("Этот аккаунт уже подключен к продавцу") from exc
