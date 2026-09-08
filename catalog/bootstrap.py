from __future__ import annotations

from pathlib import Path
from typing import Any

from .excel_io import import_catalog_xlsx
from .repository import count_products, database_backup, ensure_catalog_schema, prune_backups


def bootstrap_catalog(
    db_path: str | Path,
    *,
    legacy_xlsx_path: str | Path,
    sheet_name: str,
    columns: dict[str, str],
    backup_dir: str | Path,
    auto_import: bool = True,
) -> dict[str, Any]:
    ensure_catalog_schema(db_path)
    current = count_products(db_path)
    source = Path(legacy_xlsx_path)
    if current or not auto_import or not source.exists():
        return {"imported": False, "products": current, "source": str(source)}

    backup = database_backup(db_path, backup_dir, reason="before_catalog_import")
    result = import_catalog_xlsx(
        db_path,
        source,
        mode="merge",
        sheet_name=sheet_name,
        columns=columns,
        source_filename=source.name,
    )
    prune_backups(backup_dir, keep=30)
    return {"imported": True, "backup": str(backup), **result}
