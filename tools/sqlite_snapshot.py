from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass
class ProbeResult:
    ok: bool
    mode: str
    snapshot_db: Path | None = None
    freshness: str = ""
    rows: int = 0
    error: str = ""


def _copy_family(src_db: Path, dst_dir: Path) -> Path:
    dst_dir.mkdir(parents=True, exist_ok=True)
    dst_db = dst_dir / "fbe.db"
    shutil.copy2(src_db, dst_db)
    for suffix in ("-wal", "-shm"):
        p = Path(str(src_db) + suffix)
        if p.exists():
            shutil.copy2(p, Path(str(dst_db) + suffix))
    return dst_db


def _integrity(conn: sqlite3.Connection) -> None:
    row = conn.execute("PRAGMA integrity_check").fetchone()
    if not row or str(row[0]).lower() != "ok":
        raise sqlite3.DatabaseError(f"integrity_check failed: {row[0] if row else 'no result'}")


def _freshness(conn: sqlite3.Connection) -> tuple[str, int]:
    latest = ""
    rows_total = 0
    probes = [
        ("marking_codes", "updated_at"),
        ("marking_events", "created_at"),
        ("suz_orders", "updated_at"),
        ("fbs_order_registry", "last_sync_at"),
        ("fbs_supply_registry", "last_sync_at"),
    ]
    for table, col in probes:
        try:
            cnt = int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] or 0)
            rows_total += cnt
            value = conn.execute(f"SELECT MAX({col}) FROM {table}").fetchone()[0]
            if value and str(value) > latest:
                latest = str(value)
        except sqlite3.Error:
            pass
    return latest, rows_total


def probe_source(src_db: Path, work_root: Path, label: str) -> ProbeResult:
    """Probe a DB without ever mutating the source files.

    First try the complete SQLite family (db + WAL + SHM), which preserves all
    committed WAL transactions. If that family is inconsistent, retry the main
    DB alone. The second mode is specifically able to recover installations
    affected by FBE 0.83.9's foreign-WAL migration bug.
    """
    if not src_db.exists():
        return ProbeResult(False, "missing", error="fbe.db not found")

    candidate_dir = work_root / label
    if candidate_dir.exists():
        shutil.rmtree(candidate_dir, ignore_errors=True)
    copied_db = _copy_family(src_db, candidate_dir)

    def attempt(db_path: Path, mode: str) -> ProbeResult:
        try:
            conn = sqlite3.connect(db_path, timeout=10)
            try:
                _integrity(conn)
                fresh, rows = _freshness(conn)
                # Create a canonical single-file snapshot through SQLite's
                # backup API; this includes committed WAL content when valid.
                snapshot = candidate_dir / f"snapshot_{mode}.db"
                if snapshot.exists():
                    snapshot.unlink()
                dst = sqlite3.connect(snapshot)
                try:
                    conn.backup(dst)
                    dst.commit()
                    _integrity(dst)
                finally:
                    dst.close()
                return ProbeResult(True, mode, snapshot, fresh, rows)
            finally:
                conn.close()
        except Exception as exc:
            return ProbeResult(False, mode, error=f"{type(exc).__name__}: {exc}")

    full = attempt(copied_db, "db+wal")
    if full.ok:
        return full

    # Never delete source sidecars. Remove only the temporary copies and retry.
    for suffix in ("-wal", "-shm"):
        Path(str(copied_db) + suffix).unlink(missing_ok=True)
    main_only = attempt(copied_db, "main-only")
    if main_only.ok:
        main_only.error = f"full family failed: {full.error}"
        return main_only
    main_only.error = f"full family failed: {full.error}; main-only failed: {main_only.error}"
    return main_only


def _version_key(name: str) -> tuple[int, ...]:
    raw = name.removeprefix("FBE_")
    parts: list[int] = []
    for token in raw.split("."):
        try:
            parts.append(int(token))
        except ValueError:
            break
    return tuple(parts)


def choose_best_source(current_root: Path, preferred_root: Path | None, work_root: Path) -> tuple[Path, ProbeResult, list[str]]:
    roots: list[Path] = []
    if preferred_root and preferred_root.exists() and preferred_root.resolve() != current_root.resolve():
        roots.append(preferred_root.resolve())

    # Recovery fallback: the 0.83.9 shortcut may point at a DB paired with a
    # foreign packaged WAL. Older sibling FBE folders can still hold the last
    # fully consistent live WAL, so inspect them without modifying anything.
    parent = current_root.parent
    siblings = sorted(
        [p for p in parent.glob("FBE_*") if p.is_dir() and p.resolve() != current_root.resolve()],
        key=lambda p: (_version_key(p.name), p.stat().st_mtime),
        reverse=True,
    )
    for p in siblings:
        if p.resolve() not in {r.resolve() for r in roots}:
            roots.append(p.resolve())

    results: list[tuple[Path, ProbeResult, int]] = []
    log: list[str] = []
    for idx, root in enumerate(roots):
        db = root / "data" / "fbe.db"
        if not db.exists():
            continue
        result = probe_source(db, work_root, f"candidate_{idx}")
        log.append(f"{root} -> {result.mode}, ok={result.ok}, freshness={result.freshness or '-'}, rows={result.rows}, error={result.error or '-'}")
        if result.ok:
            results.append((root, result, idx))

    if not results:
        raise RuntimeError("No valid previous FBE database found. " + " | ".join(log))

    # Logical freshness is the primary criterion: never prefer an old packaged
    # database merely because it has no broken sidecar over a newer verified
    # main-file recovery. For equally fresh candidates, prefer a complete
    # db+wal snapshot because it can contain committed transactions not yet
    # checkpointed into the main file.
    def score(item: tuple[Path, ProbeResult, int]):
        root, result, idx = item
        safe = 1 if result.mode == "db+wal" else 0
        return (result.freshness, safe, result.rows, -idx, _version_key(root.name))

    root, result, _ = max(results, key=score)
    return root, result, log


def install_snapshot(current_root: Path, preferred_root: Path | None) -> tuple[Path, str]:
    data = current_root / "data"
    backups = data / "backups"
    backups.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")

    with tempfile.TemporaryDirectory(prefix="fbe_sqlite_migrate_") as td:
        work = Path(td)
        source_root, result, probe_log = choose_best_source(current_root, preferred_root, work)
        assert result.snapshot_db is not None

        # Preserve exact raw source files for forensic/manual recovery.
        raw_backup = backups / f"migration_source_{stamp}_{source_root.name}"
        raw_backup.mkdir(parents=True, exist_ok=True)
        src_db = source_root / "data" / "fbe.db"
        shutil.copy2(src_db, raw_backup / "fbe.db")
        for suffix in ("-wal", "-shm"):
            side = Path(str(src_db) + suffix)
            if side.exists():
                shutil.copy2(side, raw_backup / f"fbe.db{suffix}")
        (raw_backup / "PROBE.txt").write_text("\n".join(probe_log) + "\n", encoding="utf-8")

        dst_db = data / "fbe.db"
        # The exact 0.83.9 corruption vector: never leave sidecars from the
        # package (or from another DB generation) next to the migrated main DB.
        for suffix in ("-wal", "-shm"):
            Path(str(dst_db) + suffix).unlink(missing_ok=True)

        temp_dst = data / ".fbe.db.migrating"
        temp_dst.unlink(missing_ok=True)
        shutil.copy2(result.snapshot_db, temp_dst)

        conn = sqlite3.connect(temp_dst)
        try:
            _integrity(conn)
        finally:
            conn.close()
        # Set persistent journal mode in a fresh connection after the integrity
        # reader has been closed. No checkpoint is needed: the snapshot has no
        # pending WAL frames at this point.
        conn = sqlite3.connect(temp_dst)
        try:
            conn.execute("PRAGMA journal_mode=WAL").fetchone()
            conn.commit()
        finally:
            conn.close()
        for suffix in ("-wal", "-shm"):
            Path(str(temp_dst) + suffix).unlink(missing_ok=True)

        # Keep the packaged/current DB before replacement as another fallback.
        if dst_db.exists():
            package_backup = backups / f"current_before_migration_{stamp}.db"
            shutil.copy2(dst_db, package_backup)

        os.replace(temp_dst, dst_db)
        for suffix in ("-wal", "-shm"):
            Path(str(dst_db) + suffix).unlink(missing_ok=True)

        # Final verification from the exact file that FBE will open.
        final = sqlite3.connect(dst_db)
        try:
            _integrity(final)
        finally:
            final.close()

    return source_root, result.mode


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--current-root", required=True)
    ap.add_argument("--preferred-root", default="")
    args = ap.parse_args()
    current = Path(args.current_root).resolve()
    preferred = Path(args.preferred_root).resolve() if args.preferred_root else None
    try:
        source, mode = install_snapshot(current, preferred)
    except Exception as exc:
        print(f"[FBE] DATABASE MIGRATION ERROR: {exc}", file=sys.stderr)
        return 2
    print(f"[FBE] SQLite snapshot migrated from: {source}")
    print(f"[FBE] SQLite recovery mode: {mode}")
    print("[FBE] SQLite integrity_check: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
