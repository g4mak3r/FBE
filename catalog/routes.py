from __future__ import annotations

import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from .excel_io import export_catalog_xlsx, import_catalog_xlsx
from .repository import (
    CatalogDbError,
    bulk_apply_compliance,
    catalog_stats,
    database_backup,
    get_product,
    list_products,
    list_samples,
    prune_backups,
    save_product,
    save_sample,
    set_product_active,
)


class ProductPayload(BaseModel):
    original_article: str = ""
    seller_article: str
    wb_article: str = ""
    subcategory: str = ""
    product: str = ""
    volume: str = ""
    sample: str = ""
    sample_volume: str = ""
    category: str = ""
    marking_profile: str = ""
    gtin: str = ""
    tnved_code: str = ""
    certificate_number: str = ""
    certificate_date: str = ""
    certificate_valid_until: str = ""
    shelf_life_months: Any = ""
    alcohol_volume_pct: Any = ""
    notes: str = ""
    marketing_title: str = ""
    fragrance_family: str = ""
    target_audience: str = ""
    marketing_description: str = ""
    marketing_keywords: str = ""
    package_length_cm: Any = ""
    package_width_cm: Any = ""
    package_height_cm: Any = ""
    package_weight_g: Any = ""
    units_per_box: Any = ""
    cost_price_rub: Any = ""
    active: bool = True
    # Low-level fields remain accepted for legacy XLSX imports and custom profiles.
    kiz_required: bool | None = None
    product_group: str | None = None
    template_id: int | None = None
    cis_type: str | None = None
    certificate_type: str | None = None


class BulkPayload(BaseModel):
    articles: list[str] = Field(default_factory=list)
    values: dict[str, Any] = Field(default_factory=dict)


class ActivePayload(BaseModel):
    active: bool


class SamplePayload(BaseModel):
    original_name: str = ""
    sample_no: str = ""
    name: str
    wb_code: str = ""
    qr_link: str = ""
    active: bool = True


def build_catalog_router(
    *,
    db_path: str | Path,
    templates_dir: str | Path,
    config: dict[str, Any],
    template_globals: dict[str, Any] | None = None,
) -> APIRouter:
    router = APIRouter()
    templates = Jinja2Templates(directory=str(templates_dir))
    templates.env.globals.update(template_globals or {})
    db_path = Path(db_path)
    exports_dir = Path(config.get("catalog_exports_dir") or "data/exports")
    backups_dir = Path(config.get("catalog_backups_dir") or "data/backups")
    legacy_path = Path(config.get("catalog_legacy_excel_path") or config.get("excel_path") or "data/products.xlsx")
    template_path = Path(config.get("catalog_template_path") or "data/FBE_catalog_template_0.81.1.xlsx")

    @router.get("/catalog", response_class=HTMLResponse)
    def catalog_page(
        request: Request,
        q: str = "",
        category: str = "",
        marking: str = "",
        active: str = "active",
        page: int = 1,
        msg: str = "",
        error: str = "",
    ):
        result = list_products(
            db_path,
            query=q,
            category=category,
            marking=marking,
            active=active,
            page=page,
            page_size=int(config.get("catalog_page_size") or 100),
        )
        return templates.TemplateResponse(
            request,
            "catalog.html",
            {
                "request": request,
                "config": config,
                "result": result,
                "products": result["items"],
                "stats": catalog_stats(db_path),
                "samples": list_samples(db_path, active_only=False),
                "filters": {"q": q, "category": category, "marking": marking, "active": active},
                "msg": msg,
                "error": error,
                "legacy_path": str(legacy_path),
            },
        )

    @router.get("/api/catalog/products/{seller_article}")
    def api_product(seller_article: str):
        product = get_product(db_path, seller_article)
        if not product:
            return JSONResponse({"ok": False, "error": "Товар не найден"}, status_code=404)
        return {"ok": True, "product": product}

    @router.post("/api/catalog/products")
    def api_save_product(payload: ProductPayload):
        try:
            saved = save_product(
                db_path,
                payload.model_dump(exclude={"original_article"}, exclude_none=True),
                original_article=payload.original_article or None,
            )
            return {"ok": True, "product": saved}
        except (CatalogDbError, ValueError) as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=409)

    @router.post("/api/catalog/products/{seller_article}/active")
    def api_set_active(seller_article: str, payload: ActivePayload):
        try:
            set_product_active(db_path, seller_article, payload.active)
            return {"ok": True}
        except CatalogDbError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=404)

    @router.post("/api/catalog/bulk-compliance")
    def api_bulk(payload: BulkPayload):
        try:
            database_backup(db_path, backups_dir, reason="before_bulk_catalog")
            count = bulk_apply_compliance(db_path, payload.articles, payload.values)
            prune_backups(backups_dir, keep=int(config.get("catalog_backup_keep") or 30))
            return {"ok": True, "updated": count}
        except (CatalogDbError, ValueError) as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=409)

    @router.post("/api/catalog/samples")
    def api_save_sample(payload: SamplePayload):
        try:
            saved = save_sample(
                db_path,
                payload.model_dump(exclude={"original_name"}),
                original_name=payload.original_name or None,
            )
            return {"ok": True, "sample": saved}
        except CatalogDbError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=409)

    @router.post("/catalog/import")
    async def catalog_import(file: UploadFile = File(...), mode: str = Form("merge")):
        if not file.filename or not file.filename.lower().endswith(".xlsx"):
            return JSONResponse({"ok": False, "error": "Нужен файл .xlsx"}, status_code=400)
        backups_dir.mkdir(parents=True, exist_ok=True)
        backup = database_backup(db_path, backups_dir, reason="before_catalog_import")
        suffix = Path(file.filename).suffix
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as temp:
            temp.write(await file.read())
            temp_path = Path(temp.name)
        try:
            result = import_catalog_xlsx(
                db_path,
                temp_path,
                mode=mode,
                sheet_name=str(config.get("excel_sheet") or "mainSheet"),
                columns=config.get("excel_columns") or {},
                source_filename=file.filename,
            )
            prune_backups(backups_dir, keep=int(config.get("catalog_backup_keep") or 30))
            return {"ok": True, "backup": str(backup), **result}
        except Exception as exc:
            return JSONResponse({"ok": False, "error": str(exc), "backup": str(backup)}, status_code=409)
        finally:
            try:
                temp_path.unlink()
            except OSError:
                pass

    @router.get("/catalog/export.xlsx")
    def catalog_export():
        exports_dir.mkdir(parents=True, exist_ok=True)
        target = exports_dir / f"Каталог товаров {datetime.now().strftime('%d.%m.%Y')}.xlsx"
        export_catalog_xlsx(db_path, target)
        return FileResponse(target, filename=target.name, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

    @router.get("/catalog/template.xlsx")
    def catalog_template():
        if not template_path.exists():
            return JSONResponse({"ok": False, "error": "Шаблон каталога не найден"}, status_code=404)
        return FileResponse(
            template_path,
            filename="FBE_catalog_template_0.81.1.xlsx",
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

    @router.post("/api/catalog/backup")
    def catalog_backup():
        try:
            target = database_backup(db_path, backups_dir, reason="manual")
            prune_backups(backups_dir, keep=int(config.get("catalog_backup_keep") or 30))
            return {"ok": True, "filename": target.name, "path": str(target)}
        except CatalogDbError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=409)

    @router.get("/catalog/backup/latest")
    def download_latest_backup():
        files = sorted(backups_dir.glob("fbe_*.db"), key=lambda p: p.stat().st_mtime, reverse=True)
        if not files:
            target = database_backup(db_path, backups_dir, reason="download")
        else:
            target = files[0]
        return FileResponse(target, filename=target.name, media_type="application/vnd.sqlite3")

    return router
