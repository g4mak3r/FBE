from __future__ import annotations

import json
import os
import threading
import uuid
from pathlib import Path
from typing import Any


class ConnectionStore:
    """Local-only connection/profile state.

    This file is runtime state, not application configuration. It may contain
    secrets and therefore lives under data/local/ which is excluded from Git.
    """

    def __init__(self, base_dir: str | Path, relative_path: str = "data/local/connections.json"):
        self.base_dir = Path(base_dir).resolve()
        self.path = (self.base_dir / relative_path).resolve()
        self._lock = threading.RLock()

    @staticmethod
    def _default() -> dict[str, Any]:
        return {
            "version": 2,
            "mode": "real",
            "wb": {"token": "", "profile": {}},
            "marking": {},
            "marking_by_sid": {},
        }

    def load(self) -> dict[str, Any]:
        with self._lock:
            if not self.path.exists():
                return self._default()
            try:
                payload = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError):
                return self._default()
            if not isinstance(payload, dict):
                return self._default()
            result = self._default()
            result.update({k: v for k, v in payload.items() if k in result})
            if not isinstance(result.get("wb"), dict):
                result["wb"] = {"token": "", "profile": {}}
            if not isinstance(result["wb"].get("profile"), dict):
                result["wb"]["profile"] = {}
            if not isinstance(result.get("marking_by_sid"), dict):
                result["marking_by_sid"] = {}
            if not isinstance(result.get("marking"), dict):
                result["marking"] = {}
            mode = str(result.get("mode") or "real").lower()
            result["mode"] = "demo" if mode == "demo" else "real"
            return result

    def save(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            normalized = self._default()
            normalized.update(payload)
            normalized["mode"] = "demo" if str(normalized.get("mode") or "").lower() == "demo" else "real"
            temporary = self.path.with_name(f".{self.path.name}.{uuid.uuid4().hex}.tmp")
            try:
                temporary.touch(mode=0o600, exist_ok=False)
                temporary.write_text(
                    json.dumps(normalized, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
                os.replace(temporary, self.path)
                try:
                    os.chmod(self.path, 0o600)
                except OSError:
                    pass
            finally:
                temporary.unlink(missing_ok=True)
            return normalized

    def set_mode(self, mode: str) -> dict[str, Any]:
        payload = self.load()
        payload["mode"] = "demo" if str(mode).lower() == "demo" else "real"
        return self.save(payload)

    def set_wb(self, *, token: str, profile: dict[str, Any]) -> dict[str, Any]:
        payload = self.load()
        payload["wb"] = {
            "token": str(token or "").strip(),
            "profile": {
                "name": str(profile.get("name") or "").strip(),
                "sid": str(profile.get("sid") or "").strip(),
                "tin": str(profile.get("tin") or "").strip(),
                "tradeMark": str(profile.get("tradeMark") or "").strip(),
            },
        }
        return self.save(payload)

    def clear_wb(self) -> dict[str, Any]:
        payload = self.load()
        payload["wb"] = {"token": "", "profile": {}}
        return self.save(payload)

    def update_marking(self, changes: dict[str, Any]) -> dict[str, Any]:
        payload = self.load()
        sid = str((payload.get("wb", {}).get("profile") or {}).get("sid") or "")
        if not sid or not payload.get("wb", {}).get("token"):
            raise ValueError("Сначала подключите Wildberries для выбора рабочего пространства.")
        profiles = dict(payload.get("marking_by_sid") or {})
        current = dict(profiles.get(sid) or {})
        for key, value in changes.items():
            if str(key).startswith("suz_") or str(key) in {"powershell_exe", "java_exe"}:
                current[str(key)] = value
        profiles[sid] = current
        payload["marking_by_sid"] = profiles
        self.save(payload)
        return dict(current)

    def public_state(self) -> dict[str, Any]:
        payload = self.load()
        if payload["mode"] == "demo":
            return {"mode": "demo", "wb": {
                "connected": True, "name": "FBE Demo Company",
                "sid": "00000000-0000-4000-8000-000000000017", "tin": "000000000000",
                "tradeMark": "DEMO"}, "marking": {
                "configured": True, "inn": "000000000000", "oms_id_set": True,
                "connection_id_set": True, "certificate_set": True}}
        wb = dict(payload.get("wb") or {})
        profile = dict(wb.get("profile") or {})
        marking = dict((payload.get("marking_by_sid") or {}).get(str(profile.get("sid") or "")) or {})
        inn = str(
            marking.get("suz_auth_inn")
            or marking.get("suz_true_participant_inn")
            or ""
        ).strip()
        return {
            "mode": payload.get("mode", "real"),
            "wb": {
                "connected": bool(str(wb.get("token") or "").strip() and str(profile.get("sid") or "").strip()),
                "name": str(profile.get("name") or "").strip(),
                "sid": str(profile.get("sid") or "").strip(),
                "tin": str(profile.get("tin") or "").strip(),
                "tradeMark": str(profile.get("tradeMark") or "").strip(),
            },
            "marking": {
                "configured": bool(
                    str(marking.get("suz_oms_id") or "").strip()
                    and str(marking.get("suz_connection_id") or "").strip()
                    and inn
                ),
                "inn": inn,
                "oms_id_set": bool(str(marking.get("suz_oms_id") or "").strip()),
                "connection_id_set": bool(str(marking.get("suz_connection_id") or "").strip()),
                "certificate_set": bool(str(marking.get("suz_cert_thumbprint") or "").strip()),
            },
        }
