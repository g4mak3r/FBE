"""Explicit offline copy to one asserted seller SID. Never overwrite a workspace."""
from pathlib import Path
import argparse
import os
import sqlite3
import sys
import uuid
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from workspaces import seller_workspace


def adopt(source: Path, base_dir: Path, sid: str) -> Path:
    source = source.resolve(strict=True)
    target_root = seller_workspace(base_dir, sid)
    # Exclusive reservation prevents replacing an existing or concurrently
    # selected workspace. Stop all FBE processes before this maintenance step.
    target_root.mkdir(parents=True, exist_ok=False)
    stage = target_root / ("snapshot-" + uuid.uuid4().hex + ".db")
    target = target_root / "fbe.db"
    try:
        with sqlite3.connect(source.as_uri() + "?mode=ro", uri=True) as src:
            if src.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("Source integrity check failed; original preserved")
            with sqlite3.connect(stage) as dst:
                src.backup(dst)
                if dst.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise ValueError("Snapshot integrity check failed")
        os.replace(stage, target)
        return target
    except Exception:
        stage.unlink(missing_ok=True)
        try:
            target_root.rmdir()
        except OSError:
            pass
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--sid", required=True, help="Seller SID whose ownership you have verified")
    args = parser.parse_args()
    target = adopt(args.source, Path(__file__).resolve().parents[1], args.sid)
    print("[FBE] Verified snapshot saved:", target)
    print("[FBE] Source preserved. Connect exactly this seller through the profile UI.")


if __name__ == "__main__":
    main()
