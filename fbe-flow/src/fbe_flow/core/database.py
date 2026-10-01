import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


class Database:
    def __init__(self, path: Path):
        self.path = path

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        # Never share a connection between request threads and the worker.
        conn = sqlite3.connect(self.path, timeout=5)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 5000")
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        migrations = sorted((Path(__file__).parent / "migrations").glob("*.sql"))
        with self.connection() as conn:
            conn.execute("PRAGMA journal_mode = WAL")
            current = conn.execute("PRAGMA user_version").fetchone()[0]
            if current > len(migrations):
                raise RuntimeError("Database schema is newer than this application")
            if 0 < current < len(migrations):
                backup_path = self.path.with_suffix(f".before-v{len(migrations)}.sqlite3")
                if not backup_path.exists():
                    with sqlite3.connect(backup_path) as backup:
                        conn.backup(backup)
            for version, migration in enumerate(migrations, start=1):
                if version > current:
                    # executescript doesn't preserve an implicit transaction, so make it explicit.
                    conn.executescript(
                        f"BEGIN IMMEDIATE;\n{migration.read_text(encoding='utf-8')}\n"
                        f"PRAGMA user_version = {version};\nCOMMIT;"
                    )
