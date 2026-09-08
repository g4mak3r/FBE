"""Physical operational isolation. No automatic adoption of unowned legacy data."""
from __future__ import annotations

import hashlib
from pathlib import Path

# Every operational path is selected together with the DB, never from a former
# seller's config. Executables and shipped templates remain application assets.
RUNTIME_PATHS = {
    "database_path": "fbe.db",
    "excel_path": "products.xlsx",
    "catalog_legacy_excel_path": "products.xlsx",
    "catalog_exports_dir": "exports",
    "catalog_backups_dir": "backups",
    "pdf_output_dir": "stickers_pdf",
    "sticker_cache_dir": "stickers",
    "sticker_metadata_dir": "sticker_meta",
    "supply_qr_output_dir": "supply_qr",
    "shipping_unit_qr_output_dir": "shipping_unit_qr",
    "marking_print_output_dir": "marking_print",
    "manual_pdf_output_dir": "manual_pdf",
    "paired_pdf_output_dir": "paired_pdf",
    "auto_pdf_output_dir": "auto_pdf",
    "portal_pdf_output_dir": "portal_pdf",
    "mixed_pdf_output_dir": "mixed_pdf",
    "analytics_cache_path": "cache/analytics_today.json",
    "inventory_path": "inventory.xlsx",
    "inventory_writeoff_state_path": "cache/inventory_writeoffs.json",
    "supply_articles_cache_dir": "cache/supply_articles",
    "economy_settings_path": "economy/settings.json",
    "economy_raw_dir": "economy/raw",
    "economy_exports_dir": "economy/exports",
    "economy_diagnostics_dir": "diagnostics",
    "bartender_csv": "print/internal_label.csv",
    "manual_bartender_csv": "print/manual_label.csv",
    "print_debug_dir": "print",
    "print_log_path": "logs/print_log.jsonl",
    "suz_local_settings_path": "unused_legacy_suz.json",
}


def seller_workspace(base_dir: Path, sid: str) -> Path:
    if not str(sid).strip():
        raise ValueError("Seller SID is required")
    # Full hash handles arbitrary SID strings, Windows reserved names, case and
    # Unicode without collisions caused by slug normalization or path traversal.
    key = hashlib.sha256(str(sid).strip().encode("utf-8")).hexdigest()
    return Path(base_dir).resolve() / "data" / "workspaces" / key


def configure_workspace(config: dict, base_dir: Path, *, sid: str, demo: bool) -> None:
    root = (base_dir / "data" / "demo" if demo else
            seller_workspace(base_dir, sid) if sid else base_dir / "data" / "unconnected")
    root = root.resolve()
    config["workspace_root"] = str(root)
    config["workspace_sid"] = "DEMO" if demo else sid
    for key, relative in RUNTIME_PATHS.items():
        config[key] = str(root / relative)
    if demo:
        config["database_path"] = str(root / "fbe_demo.db")
    config["manual_bartender_csv_by_type"] = {
        key: str(root / "print" / f"manual_{key}.csv") for key in ("samples", "names")}
    config["manual_pdf_output_by_type"] = {
        key: str(root / "manual_pdf" / f"FBE_MANUAL_{key.upper()}.pdf") for key in ("samples", "names")}
    config["catalog_auto_import_legacy"] = False
    # Old global campaign IDs are operational data belonging to an unknown SID.
    config["economy_campaign_ids"] = []
    config["bartender_template"] = str(root / "templates/default.btw")
    config["bartender_templates_by_category"] = {
        category: str(root / "templates" / name) for category, name in {
            "Парфюмерия": "perfume.btw", "СПА": "spa.btw", "Набор": "set.btw",
            "Наборы": "set.btw", "default": "default.btw"}.items()}
    config["manual_bartender_templates"] = {
        key: str(root / "templates" / (key + ".btw")) for key in ("samples", "names")}
