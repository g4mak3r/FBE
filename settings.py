from __future__ import annotations

import json
import os
import threading
import uuid
from pathlib import Path
from typing import Any

from connections import ConnectionStore

PATH_KEYS = {
    "excel_path",
    "bartender_exe",
    "bartender_template",
    "bartender_csv",
    "manual_bartender_csv",
    "pdf_output_dir",
    "database_path",
    "catalog_legacy_excel_path",
    "catalog_exports_dir",
    "catalog_backups_dir",
    "supply_qr_output_dir",
    "suz_api_spec_path",
    "suz_local_settings_path",
    "manual_pdf_output_dir",
    "paired_pdf_output_dir",
    "auto_pdf_output_dir",
    "portal_pdf_output_dir",
    "mixed_pdf_output_dir",
    "marking_print_output_dir",
    "shipping_unit_qr_output_dir",
    "analytics_cache_path",
    "inventory_path",
    "inventory_writeoff_state_path",
    "supply_articles_cache_dir",
    "economy_settings_path",
    "economy_raw_dir",
    "economy_exports_dir",
    "economy_diagnostics_dir",
    "catalog_template_path",
    "sumatra_pdf_exe",
    "bullzip_runonce_dir",
    "sticker_cache_dir",
    "sticker_metadata_dir",
    "print_debug_dir",
    "print_log_path",
}

EXTERNAL_SETTING_KEYS = {
    "wb_token",
    "powershell_exe",
    "java_exe",
}


class Settings:
    def __init__(self, path: str = "config.json"):
        self.path = Path(path).resolve()
        self.base_dir = self.path.parent
        self._lock = threading.RLock()
        self.connection_store = ConnectionStore(self.base_dir)
        runtime = self.connection_store.load()
        self._demo = runtime["mode"] == "demo"
        # DEMO reads only shipped safe defaults, never legacy production config.
        self.data: dict[str, Any] = (
            self._read_json_object(self.base_dir / "config.example.json")
            if self._demo else self._load()
        )
        self._apply_runtime_connection_overrides(runtime)
        self._apply_env_overrides()
        self._normalize_paths()

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            example = self.base_dir / "config.example.json"
            if example.exists():
                self.path.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
            else:
                self.path.write_text("{}", encoding="utf-8")
        return json.loads(self.path.read_text(encoding="utf-8"))


    def _apply_runtime_connection_overrides(self, runtime: dict[str, Any]) -> None:
        from workspaces import configure_workspace
        wb = dict(runtime.get("wb") or {})
        profile = dict(wb.get("profile") or {})
        sid = str(profile.get("sid") or "").strip()
        token = str(wb.get("token") or "").strip()
        if not token:
            sid = ""
        # Legacy config/env tokens have no validated binding to a seller DB.
        # They remain on disk for manual recovery, but are never activated.
        self.data["wb_token"] = token if sid and not self._demo else ""
        self.data["mock_mode"] = self._demo
        defaults = self._read_json_object(self.base_dir / "config.example.json")
        for key in list(self.data):
            if key.startswith("suz_"):
                self.data[key] = defaults.get(key, "")
        if not self._demo and sid:
            self.data.update({k: v for k, v in
                dict((runtime.get("marking_by_sid") or {}).get(sid) or {}).items()
                if k.startswith("suz_") or k in {"powershell_exe", "java_exe"}})
        if self._demo:
            self.data.update({
                "wb_token": "DEMO",
                "suz_oms_id": "00000000-0000-4000-8000-000000000017",
                "suz_connection_id": "00000000-0000-4000-8000-000000000018",
                "suz_cert_thumbprint": "DEMO000000000000000000000000000000000000",
                "suz_auth_inn": "000000000000",
                "suz_true_participant_inn": "000000000000",
                "suz_true_producer_inn": "000000000000",
                "suz_true_owner_inn": "000000000000",
            })
        configure_workspace(self.data, self.base_dir, sid=sid, demo=self._demo)
        self._public = self.connection_store.public_state()

    def connection_public_state(self) -> dict[str, Any]:
        import copy
        return copy.deepcopy(self._public)

    def set_runtime_mode(self, mode: str) -> dict[str, Any]:
        return self.connection_store.set_mode(mode)

    def set_runtime_wb_connection(self, token: str, profile: dict[str, Any]) -> dict[str, Any]:
        if self._demo:
            raise ValueError("DEMO MODE: подключение production запрещено.")
        if not str(profile.get("sid") or "").strip():
            raise ValueError("WB не вернул seller SID. Подключение не сохранено.")
        # Active clients and DB paths are immutable until process restart.
        return self.connection_store.set_wb(token=token, profile=profile)

    def clear_runtime_wb_connection(self) -> dict[str, Any]:
        if self._demo:
            raise ValueError("DEMO MODE: изменение REAL credentials запрещено.")
        return self.connection_store.clear_wb()

    def _apply_env_overrides(self) -> None:
        # Only print preference is supported via env. Account credentials must
        # pass the read-only connection flow and be bound to the validated SID.
        dry_run = os.getenv("FBE_DRY_RUN_PRINT")
        if dry_run is not None and not self._demo:
            self.data["dry_run_print"] = dry_run.strip().lower() in {"1", "true", "yes", "on"}
        if self._demo:
            self.data["mock_mode"] = True
            self.data["dry_run_print"] = True
            self.data["marking_direct_print_dry_run"] = True

    @staticmethod
    def _read_json_object(path: Path) -> dict[str, Any]:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return {}
        return dict(value) if isinstance(value, dict) else {}

    @staticmethod
    def _write_json_atomic(path: Path, value: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            temporary.write_text(
                json.dumps(value, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def external_api_settings(self) -> dict[str, Any]:
        """Return the single effective WB/True API/SUZ configuration view."""
        with self._lock:
            return {
                key: value
                for key, value in self.data.items()
                if key in EXTERNAL_SETTING_KEYS or key in {"mock_mode", "workspace_root"} or str(key).startswith("suz_")
            }

    def update_external_api_settings(self, changes: dict[str, Any]) -> dict[str, Any]:
        """Persist user-editable external settings without exposing file layout."""
        if self._demo:
            raise ValueError("DEMO MODE: изменение credentials запрещено.")
        allowed = {
            str(key): value
            for key, value in changes.items()
            if str(key).startswith("suz_") or str(key) in {"powershell_exe", "java_exe"}
        }
        if not allowed:
            return self.external_api_settings()
        with self._lock:
            self.connection_store.update_marking(allowed)
            self.data.update(allowed)
            self._public = self.connection_store.public_state()
            # Reassert process mode/print invariants after local changes.
            self._apply_env_overrides()
            self._normalize_paths()
            return self.external_api_settings()

    def set_value(self, key: str, value: Any) -> Any:
        """Persist one setting without serializing normalized runtime paths."""
        from workspaces import RUNTIME_PATHS
        if key == "wb_token" or key.startswith("suz_") or key in RUNTIME_PATHS or key in {
            "mock_mode", "workspace_root", "workspace_sid", "manual_pdf_output_by_type",
            "manual_bartender_csv_by_type", "catalog_auto_import_legacy"
        }:
            raise ValueError("Используйте профиль подключения; рабочие границы нельзя менять через config.")
        if self._demo:
            raise ValueError("DEMO MODE: сохранение настроек REAL запрещено.")
        with self._lock:
            raw = self._read_json_object(self.path)
            raw[str(key)] = value
            self._write_json_atomic(self.path, raw)
            self.data[str(key)] = value
            # A configured environment variable remains authoritative even when
            # a settings form writes a fallback value to config.json.
            self._apply_env_overrides()
            self._normalize_paths()
            return self.data.get(str(key))

    def _resolve_path(self, value: str) -> str:
        if not value:
            return value
        p = Path(value)
        if not p.is_absolute():
            p = self.base_dir / p
        # BarTender's command parser treats forward slashes inside /F=templates/perfume.btw
        # as new command parameters. Always pass absolute native Windows-style paths.
        return str(p.resolve())

    def _normalize_paths(self) -> None:
        for key in PATH_KEYS:
            if key in self.data and isinstance(self.data[key], str):
                self.data[key] = self._resolve_path(self.data[key])

        templates = self.data.get("bartender_templates_by_category")
        if isinstance(templates, dict):
            self.data["bartender_templates_by_category"] = {
                str(category): self._resolve_path(str(template_path))
                for category, template_path in templates.items()
            }

        manual_templates = self.data.get("manual_bartender_templates")
        if isinstance(manual_templates, dict):
            self.data["manual_bartender_templates"] = {
                str(kind): self._resolve_path(str(template_path))
                for kind, template_path in manual_templates.items()
            }

        for mapping_key in ("manual_bartender_csv_by_type", "manual_pdf_output_by_type"):
            mapping = self.data.get(mapping_key)
            if isinstance(mapping, dict):
                self.data[mapping_key] = {
                    str(kind): self._resolve_path(str(file_path))
                    for kind, file_path in mapping.items()
                }
