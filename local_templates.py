"""Local-only BarTender template migration helpers.

Public source never ships .btw files. During an upgrade, the installer may stage
private templates under data/local/template_import/. A verified WB seller can
then explicitly claim them into its seller-scoped workspace without overwriting
existing templates.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from workspaces import seller_workspace

TEMPLATE_NAMES = ("default", "perfume", "spa", "set", "samples", "names")
TEMPLATE_FILENAMES = tuple(f"{name}.btw" for name in TEMPLATE_NAMES)


def _root(base_dir: str | Path) -> Path:
    return Path(base_dir).resolve() / "data" / "local" / "template_import"


def _manifest_path(base_dir: str | Path) -> Path:
    return _root(base_dir) / "manifest.json"


def _pending_dir(base_dir: str | Path) -> Path:
    return _root(base_dir) / "pending"


def _load_manifest(base_dir: str | Path) -> dict[str, Any]:
    path = _manifest_path(base_dir)
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def pending_template_status(base_dir: str | Path, sid: str = "") -> dict[str, Any]:
    pending_dir = _pending_dir(base_dir)
    files = [name for name in TEMPLATE_FILENAMES if (pending_dir / name).is_file()]
    manifest = _load_manifest(base_dir)
    expected_sid = str(manifest.get("expected_sid") or "").strip()
    current_sid = str(sid or "").strip()
    mismatch = bool(expected_sid and current_sid and expected_sid != current_sid)
    return {
        "pending": bool(files),
        "count": len(files),
        "files": files,
        "source_kind": str(manifest.get("source_kind") or "legacy"),
        "seller_bound": bool(expected_sid),
        "seller_match": bool(not expected_sid or (current_sid and expected_sid == current_sid)),
        "can_import": bool(files and current_sid and not mismatch),
    }


def import_pending_templates(base_dir: str | Path, sid: str) -> dict[str, Any]:
    current_sid = str(sid or "").strip()
    if not current_sid:
        raise ValueError("Seller SID is required before importing BarTender templates.")

    pending_dir = _pending_dir(base_dir)
    manifest = _load_manifest(base_dir)
    expected_sid = str(manifest.get("expected_sid") or "").strip()
    if expected_sid and expected_sid != current_sid:
        raise ValueError("Staged BarTender templates belong to another seller SID.")

    sources = [pending_dir / name for name in TEMPLATE_FILENAMES if (pending_dir / name).is_file()]
    if not sources:
        return {"ok": True, "imported": [], "existing": [], "conflicts": [], "pending": False}

    target_dir = seller_workspace(Path(base_dir).resolve(), current_sid) / "templates"
    target_dir.mkdir(parents=True, exist_ok=True)
    imported: list[str] = []
    existing: list[str] = []
    conflicts: list[str] = []

    for source in sources:
        target = target_dir / source.name
        if target.exists():
            if target.is_file() and _digest(source) == _digest(target):
                existing.append(source.name)
                source.unlink(missing_ok=True)
            else:
                conflicts.append(source.name)
            continue
        # Exclusive-create guarantees that a seller-local template can never
        # be overwritten even if another local action creates it concurrently.
        try:
            with source.open("rb") as src, target.open("xb") as dst:
                shutil.copyfileobj(src, dst)
        except FileExistsError:
            if target.is_file() and _digest(source) == _digest(target):
                existing.append(source.name)
                source.unlink(missing_ok=True)
            else:
                conflicts.append(source.name)
            continue
        except Exception:
            target.unlink(missing_ok=True)
            raise
        imported.append(source.name)
        source.unlink(missing_ok=True)

    remaining = [name for name in TEMPLATE_FILENAMES if (pending_dir / name).is_file()]
    manifest.update({
        "claimed_sid": current_sid,
        "last_import_at": datetime.now(timezone.utc).isoformat(),
        "last_imported": imported,
        "last_existing": existing,
        "last_conflicts": conflicts,
    })
    _atomic_json(_manifest_path(base_dir), manifest)

    if not remaining:
        try:
            pending_dir.rmdir()
        except OSError:
            pass

    return {
        "ok": not conflicts,
        "imported": imported,
        "existing": existing,
        "conflicts": conflicts,
        "pending": bool(remaining),
        "target": "seller workspace/templates",
    }
