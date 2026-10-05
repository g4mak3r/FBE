"""Assortment API using the existing local-origin JSON mutation boundary."""
from typing import Annotated, Literal
from urllib.parse import quote
from fastapi import APIRouter, Query, Request
from fastapi.responses import Response
from pydantic import Field, JsonValue
from fbe_flow.core.catalog_models import (
    CatalogBatch, CatalogDocument, CatalogProduct, ClassificationRule,
)
from fbe_flow.core.models import Contract, Text
from fbe_flow.modules import catalog_xlsx
from fbe_flow.modules.catalog import decode_upload

router = APIRouter(prefix="/api/sellers/{seller_id}/catalog")
Revision = Annotated[int, Field(strict=True, ge=1)]

class ProductUpdate(CatalogProduct):
    revision: Revision
class DocumentUpdate(CatalogDocument):
    revision: Revision
class BatchUpdate(CatalogBatch):
    revision: Revision
class RuleUpdate(ClassificationRule):
    revision: Revision
class LinkInput(Contract):
    source_product_id: Text
    variant: str = Field(default="", max_length=240)
    overrides: dict[str, JsonValue] = Field(default_factory=dict)
class AdoptInput(LinkInput):
    product: CatalogProduct
class FileInput(Contract):
    filename: Text = Field(max_length=200)
    content: str = Field(max_length=44739252)
class Mapping(Contract):
    column: int = Field(strict=True, ge=1, le=250)
    field: str = Field(default="", max_length=251)
    scale: Literal["1", "10", "100", "1000", "0.1", "0.01", "0.001"] = "1"
class WorkbookInput(FileInput):
    profile: Literal["fbe", "wb", "ozon", "custom"] = "fbe"
    sheet_name: str | None = Field(default=None, max_length=31)
    header_row: int | None = Field(default=None, strict=True, ge=1, le=100)
    data_row: int | None = Field(default=None, strict=True, ge=2, le=100)
    mapping: list[Mapping] | None = Field(default=None, max_length=250)
class FillInput(WorkbookInput):
    product_ids: list[Text] = Field(min_length=1, max_length=50000)
    sheet_name: str = Field(max_length=31)
    header_row: int = Field(strict=True, ge=1, le=100)
    mapping: list[Mapping] = Field(min_length=1, max_length=250)
class ExportInput(Contract):
    product_ids: list[Text] | None = Field(default=None, max_length=100000)
class ApplyInput(Contract):
    digest: str = Field(pattern=r"^[a-f0-9]{64}$")
class CheckInput(Contract):
    connection_id: Text
class SchemaInput(CheckInput):
    category: Text = Field(max_length=240)
class DictionaryInput(SchemaInput):
    attribute_id: int = Field(strict=True, gt=0)
    cursor: int = Field(default=0, strict=True, ge=0)
class CodesInput(Contract):
    code_ids: list[Text] = Field(min_length=1, max_length=500)
class EventInput(CodesInput):
    kind: Literal["printed", "applied", "quality_checked"]
    actor: Text = Field(max_length=240)
    note: str = Field(default="", max_length=2000)

def workbook_response(content, name):
    return Response(content,
                    media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": "attachment; filename*=UTF-8''" + quote(name),
                             "Cache-Control": "no-store"})

@router.get("/stats")
def stats(request: Request, seller_id: str):
    return request.app.state.catalog.stats(seller_id)

@router.get("/products")
def products(request: Request, seller_id: str, offset: int = Query(0, ge=0),
             limit: int = Query(100, ge=1, le=200), search: str = Query("", max_length=150),
             archived: bool = False):
    return request.app.state.catalog.list(seller_id, offset, limit, search, archived)

@router.post("/products", status_code=201)
def create_product(request: Request, seller_id: str, body: CatalogProduct):
    return request.app.state.catalog.save_product(seller_id, body.model_dump(mode="json"))

@router.get("/products/{product_id}")
def product(request: Request, seller_id: str, product_id: str):
    return request.app.state.catalog.detail(seller_id, product_id)

@router.put("/products/{product_id}")
def update_product(request: Request, seller_id: str, product_id: str, body: ProductUpdate):
    return request.app.state.catalog.save_product(
        seller_id, body.model_dump(mode="json", exclude={"revision"}), product_id, body.revision)

@router.get("/sources")
def sources(request: Request, seller_id: str, connection_id: str | None = None,
            offset: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=200),
            search: str = Query("", max_length=150), unlinked: bool = False):
    return request.app.state.catalog.sources(
        seller_id, connection_id, offset, limit, search, unlinked)

@router.post("/sources/{source_id}/refresh")
def refresh_source(request: Request, seller_id: str, source_id: str):
    return request.app.state.catalog.refresh_source(seller_id, source_id)

@router.post("/sources/adopt", status_code=201)
def adopt(request: Request, seller_id: str, body: AdoptInput):
    return request.app.state.catalog.adopt(
        seller_id, body.product.model_dump(mode="json"), body.source_product_id, body.variant)

@router.post("/products/{product_id}/links", status_code=201)
def link(request: Request, seller_id: str, product_id: str, body: LinkInput):
    return request.app.state.catalog.link(
        seller_id, product_id, body.source_product_id, body.variant, body.overrides)

@router.delete("/products/{product_id}/links/{link_id}")
def unlink(request: Request, seller_id: str, product_id: str, link_id: str):
    return request.app.state.catalog.unlink(seller_id, product_id, link_id)

@router.get("/documents")
def documents(request: Request, seller_id: str, product_id: str | None = None):
    return request.app.state.catalog.documents(seller_id, product_id)

@router.post("/documents", status_code=201)
def create_document(request: Request, seller_id: str, body: CatalogDocument):
    return request.app.state.catalog.save_document(seller_id, body.model_dump(mode="json"))

@router.put("/documents/{document_id}")
def update_document(request: Request, seller_id: str, document_id: str, body: DocumentUpdate):
    return request.app.state.catalog.save_document(
        seller_id, body.model_dump(mode="json", exclude={"revision"}), document_id, body.revision)

@router.post("/documents/{document_id}/files", status_code=201)
def add_file(request: Request, seller_id: str, document_id: str, body: FileInput):
    return request.app.state.catalog.add_file(
        seller_id, document_id, body.filename, decode_upload(body.content, 10 * 1024 * 1024))

@router.get("/files/{file_id}")
def file(request: Request, seller_id: str, file_id: str):
    value = request.app.state.catalog.file(seller_id, file_id)
    return Response(value["content"], media_type=value["mime"], headers={
        "Content-Disposition": "attachment; filename*=UTF-8''" + quote(value["filename"]),
        "Cache-Control": "no-store",
    })

@router.get("/batches")
def batches(request: Request, seller_id: str, product_id: str | None = None):
    return request.app.state.catalog.batches(seller_id, product_id)

@router.post("/batches", status_code=201)
def create_batch(request: Request, seller_id: str, body: CatalogBatch):
    return request.app.state.catalog.save_batch(seller_id, body.model_dump(mode="json"))

@router.put("/batches/{batch_id}")
def update_batch(request: Request, seller_id: str, batch_id: str, body: BatchUpdate):
    return request.app.state.catalog.save_batch(
        seller_id, body.model_dump(mode="json", exclude={"revision"}), batch_id, body.revision)

@router.get("/batches/{batch_id}/codes")
def batch_codes(request: Request, seller_id: str, batch_id: str,
                offset: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=200)):
    return request.app.state.catalog.batch_codes(seller_id, batch_id, offset, limit)

@router.get("/batches/{batch_id}/available-codes")
def available_codes(request: Request, seller_id: str, batch_id: str,
                    connection_id: str | None = None, offset: int = Query(0, ge=0),
                    limit: int = Query(100, ge=1, le=200)):
    return request.app.state.catalog.available_codes(
        seller_id, batch_id, connection_id or None, offset, limit)

@router.post("/batches/{batch_id}/codes")
def assign_codes(request: Request, seller_id: str, batch_id: str, body: CodesInput):
    return request.app.state.catalog.assign_codes(seller_id, batch_id, body.code_ids)

@router.post("/batches/{batch_id}/events", status_code=201)
def record_event(request: Request, seller_id: str, batch_id: str, body: EventInput):
    return request.app.state.catalog.record_code_event(
        seller_id, batch_id, body.code_ids, body.kind, body.actor, body.note)

@router.get("/rules")
def rules(request: Request, seller_id: str):
    return request.app.state.catalog.rules(seller_id)

@router.post("/rules", status_code=201)
def create_rule(request: Request, seller_id: str, body: ClassificationRule):
    return request.app.state.catalog.save_rule(seller_id, body.model_dump(mode="json"))

@router.put("/rules/{rule_id}")
def update_rule(request: Request, seller_id: str, rule_id: str, body: RuleUpdate):
    return request.app.state.catalog.save_rule(
        seller_id, body.model_dump(mode="json", exclude={"revision"}), rule_id, body.revision)

@router.get("/products/{product_id}/classification")
def classification(request: Request, seller_id: str, product_id: str):
    catalog = request.app.state.catalog
    return catalog.classify(seller_id, catalog.product(seller_id, product_id))

@router.post("/products/{product_id}/check")
def check(request: Request, seller_id: str, product_id: str, body: CheckInput):
    return request.app.state.catalog.check(seller_id, product_id, body.connection_id)

@router.get("/schemas")
def schemas(request: Request, seller_id: str):
    return request.app.state.catalog.schemas(seller_id)

@router.post("/schemas/refresh")
def refresh_schema(request: Request, seller_id: str, body: SchemaInput):
    return request.app.state.catalog.refresh_schema(seller_id, body.connection_id, body.category)

@router.post("/schemas/dictionary")
def dictionary(request: Request, seller_id: str, body: DictionaryInput):
    return request.app.state.catalog.dictionary(
        seller_id, body.connection_id, body.category, body.attribute_id, body.cursor)

@router.get("/xlsx/template")
def template(request: Request, seller_id: str):
    request.app.state.sellers.get(seller_id)
    return workbook_response(
        catalog_xlsx.export(request.app.state.catalog, seller_id, empty=True),
        "FBE-assortment-template.xlsx")

@router.get("/xlsx/export")
def export_all(request: Request, seller_id: str):
    return workbook_response(
        catalog_xlsx.export(request.app.state.catalog, seller_id), "FBE-assortment.xlsx")

@router.post("/xlsx/export")
def export_selected(request: Request, seller_id: str, body: ExportInput):
    return workbook_response(
        catalog_xlsx.export(request.app.state.catalog, seller_id, product_ids=body.product_ids),
        "FBE-assortment-selected.xlsx")

@router.post("/xlsx/inspect")
def inspect(request: Request, seller_id: str, body: FileInput):
    request.app.state.sellers.get(seller_id)
    return catalog_xlsx.inspect(decode_upload(body.content, catalog_xlsx.MAX_BYTES))

def workbook_arguments(body):
    return {"sheet_name": body.sheet_name, "header_row": body.header_row,
            "mapping": [v.model_dump() for v in body.mapping] if body.mapping else None,
            "data_row": body.data_row}

@router.post("/xlsx/preview", status_code=201)
def preview(request: Request, seller_id: str, body: WorkbookInput):
    return catalog_xlsx.preview(
        request.app.state.catalog, seller_id, decode_upload(body.content, catalog_xlsx.MAX_BYTES),
        body.profile, **workbook_arguments(body))

@router.post("/xlsx/fill")
def fill(request: Request, seller_id: str, body: FillInput):
    content = catalog_xlsx.fill_template(
        request.app.state.catalog, seller_id, decode_upload(body.content, catalog_xlsx.MAX_BYTES),
        body.product_ids, **workbook_arguments(body))
    return workbook_response(content, "FBE-" + body.profile + "-filled.xlsx")

@router.get("/imports/{import_id}")
def import_plan(request: Request, seller_id: str, import_id: str):
    return request.app.state.catalog.import_plan(seller_id, import_id)

@router.post("/imports/{import_id}/apply")
def apply(request: Request, seller_id: str, import_id: str, body: ApplyInput):
    return request.app.state.catalog.apply_import(seller_id, import_id, body.digest)

@router.post("/imports/{import_id}/cancel")
def cancel(request: Request, seller_id: str, import_id: str):
    return request.app.state.catalog.cancel_import(seller_id, import_id)
