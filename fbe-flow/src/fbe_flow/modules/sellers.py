import sqlite3
from uuid import uuid4

from fbe_flow.core.database import Database
from fbe_flow.core.errors import InvalidInput, NotFound


def require_seller(conn: sqlite3.Connection, seller_id: str) -> dict:
    row = conn.execute("SELECT * FROM sellers WHERE id = ?", (seller_id,)).fetchone()
    if row is None:
        raise NotFound("Продавец не найден")
    return dict(row)


class Sellers:
    def __init__(self, database: Database):
        self.db = database

    def list(self) -> list[dict]:
        with self.db.connection() as conn:
            return [
                dict(row) for row in conn.execute("SELECT * FROM sellers ORDER BY created_at, id")
            ]

    def get(self, seller_id: str) -> dict:
        with self.db.connection() as conn:
            return require_seller(conn, seller_id)

    def create(self, name: str) -> dict:
        name = name.strip()
        if not name or len(name) > 120:
            raise InvalidInput("Название продавца: от 1 до 120 символов")
        with self.db.connection() as conn:
            seller_id = str(uuid4())
            conn.execute("INSERT INTO sellers(id, name) VALUES (?, ?)", (seller_id, name))
            return require_seller(conn, seller_id)
