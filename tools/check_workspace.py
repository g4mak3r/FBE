"""Read-only integrity gate for the actual selected DB, including after restart."""
from pathlib import Path
import sqlite3
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from settings import Settings


def main():
    root = Path(__file__).resolve().parents[1]
    db = Path(Settings(root / "config.json").data["database_path"])
    if not db.exists():
        print("[FBE] Workspace: fresh database")
        return 0
    with sqlite3.connect(db.as_uri() + "?mode=ro", uri=True) as conn:
        good = conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    print("[FBE] Workspace integrity: " + ("PASS" if good else "FAIL; originals preserved"))
    return 0 if good else 2


if __name__ == "__main__":
    raise SystemExit(main())
