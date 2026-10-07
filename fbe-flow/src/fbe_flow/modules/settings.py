import json

from pydantic import JsonValue

from fbe_flow.core.database import Database
from fbe_flow.core.errors import InvalidInput
from fbe_flow.modules.sellers import require_seller
from fbe_flow.modules.workspace import validate_ui_setting


class Settings:
    def __init__(self, database: Database):
        self.db = database

    def list(self, seller_id: str) -> dict[str, JsonValue]:
        with self.db.connection() as conn:
            require_seller(conn, seller_id)
            return {
                row["key"]: json.loads(row["value_json"])
                for row in conn.execute(
                    "SELECT key, value_json FROM settings WHERE seller_id = ? ORDER BY key",
                    (seller_id,),
                )
            }

    def set(self, seller_id: str, key: str, value: JsonValue) -> None:
        if not key.strip() or key != key.strip() or len(key) > 120:
            raise InvalidInput("Ключ настройки: от 1 до 120 символов, без пробелов по краям")
        if key == "store.name":
            if (
                not isinstance(value, str)
                or not 1 <= len(value.strip()) <= 60
                or any(ord(v) < 32 for v in value)
            ):
                raise InvalidInput("Название магазина: от 1 до 60 символов")
            value = value.strip()
        if key == "marketplace.selected" and (
            not isinstance(value, str) or value not in {"wb", "ozon"}
        ):
            raise InvalidInput("Выберите WB или Ozon")
        if key == "sales.selected" and value not in ("wb", "ozon", "kit"):
            raise InvalidInput("Выберите канал продаж")
        with self.db.connection() as conn:
            require_seller(conn, seller_id)
            value = validate_ui_setting(conn, seller_id, key, value)
            conn.execute(
                "INSERT INTO settings(seller_id, key, value_json) VALUES (?, ?, ?) "
                "ON CONFLICT(seller_id, key) DO UPDATE SET value_json = excluded.value_json",
                (seller_id, key, json.dumps(value, ensure_ascii=False, allow_nan=False)),
            )
