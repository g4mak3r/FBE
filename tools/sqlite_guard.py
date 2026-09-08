from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path


def check(db: Path) -> tuple[bool, str]:
    try:
        c = sqlite3.connect(db, timeout=10)
        try:
            row = c.execute("PRAGMA integrity_check").fetchone()
            if row and str(row[0]).lower() == "ok":
                return True, "ok"
            return False, str(row[0] if row else "no result")
        finally:
            c.close()
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


def copy_family(source: Path, target_dir: Path, include_sidecars: bool = True) -> Path:
    target_dir.mkdir(parents=True, exist_ok=True)
    copied = target_dir / "fbe.db"
    shutil.copy2(source, copied)
    if include_sidecars:
        for suffix in ("-wal", "-shm"):
            side = Path(str(source) + suffix)
            if side.exists():
                shutil.copy2(side, Path(str(copied) + suffix))
    return copied


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    args = ap.parse_args()
    db = Path(args.db).resolve()
    if not db.exists():
        print(f"[FBE] SQLite guard: database does not exist yet: {db}")
        return 0

    wal = Path(str(db) + "-wal")
    shm = Path(str(db) + "-shm")

    # Critical invariant: never probe a possibly mismatched live DB/WAL family
    # in place. SQLite may perform WAL recovery/checkpoint work merely by opening
    # it. Always inspect temporary copies first, leaving the user's source bytes
    # untouched until we know exactly what is safe.
    with tempfile.TemporaryDirectory(prefix="fbe_guard_") as td:
        td_path = Path(td)
        family_db = copy_family(db, td_path / "family", include_sidecars=True)
        family_ok, family_detail = check(family_db)
        if family_ok:
            print("[FBE] SQLite guard: integrity_check ok")
            return 0

        main_db = copy_family(db, td_path / "main_only", include_sidecars=False)
        main_ok, main_detail = check(main_db)

    if not wal.exists() and not shm.exists():
        print(f"[FBE] SQLite guard ERROR: main database is malformed: {family_detail}", file=sys.stderr)
        return 2

    if not main_ok:
        print(
            f"[FBE] SQLite guard ERROR: DB family malformed ({family_detail}); "
            f"main file alone also malformed ({main_detail}).",
            file=sys.stderr,
        )
        return 2

    # Main DB is independently healthy, so the sidecars are stale/foreign.
    # Quarantine rather than delete. The live main file has not been opened yet.
    stamp = time.strftime("%Y%m%d_%H%M%S")
    quarantine = db.parent / "backups" / f"stale_sqlite_sidecars_{stamp}"
    quarantine.mkdir(parents=True, exist_ok=True)
    for src in (wal, shm):
        if src.exists():
            shutil.move(str(src), quarantine / src.name)
    print(f"[FBE] SQLite guard: quarantined incompatible WAL/SHM -> {quarantine}")

    ok2, detail2 = check(db)
    if not ok2:
        print(f"[FBE] SQLite guard ERROR after sidecar recovery: {detail2}", file=sys.stderr)
        return 2
    print("[FBE] SQLite guard: recovered main DB; integrity_check ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
