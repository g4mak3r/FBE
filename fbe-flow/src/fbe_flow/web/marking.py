import json
from typing import Annotated, Literal

from fastapi import APIRouter, Query, Request
from pydantic import Field, JsonValue

from fbe_flow.core.errors import Conflict, InvalidInput
from fbe_flow.core.models import Contract, Text
from fbe_flow.modules.connections import connection_context
from fbe_flow.web.routes import connection_view

router = APIRouter(prefix="/api/sellers/{seller_id}/marking")


class ChzConnectionInput(Contract):
    name: Annotated[Text, Field(max_length=120)]
    environment: Literal["sandbox", "production"] = "sandbox"
    inn: str = Field(pattern=r"^\d{10}(\d{2})?$")
    certificate: str = Field(default="", pattern=r"^([A-Fa-f0-9]{40})?$")
    true_token: str = Field(default="", max_length=8192)
    oms_id: str = ""
    oms_connection: str = ""


class SyncInput(Contract):
    force: bool = False


class CredentialsInput(Contract):
    certificate: str = Field(default="", pattern=r"^([A-Fa-f0-9]{40})?$")
    true_token: str = Field(default="", max_length=8192)


class PrepareInput(Contract):
    action: Literal[
        "edit", "sign", "true", "suz_order", "suz_codes", "suz_utilisation", "suz_close"
    ]
    payload: dict[str, JsonValue] = Field(default_factory=dict)


class SelectionInput(Contract):
    product_ids: list[str] = Field(min_length=1, max_length=500)


class SuzInput(Contract):
    oms_id: str = Field(pattern=r"^[a-fA-F0-9-]{36}$")
    oms_connection: str = Field(pattern=r"^[a-fA-F0-9-]{36}$")


class ReconcileInput(Contract):
    external_id: str = Field(default="", max_length=150)


class CodesSelectionInput(Contract):
    code_ids: list[str] = Field(min_length=1, max_length=500)
    product_group: Text
    attributes: dict[str, JsonValue] = Field(default_factory=dict)


@router.get("/certificates")
def certificates(request: Request, seller_id: str):
    request.app.state.sellers.get(seller_id)
    return request.app.state.signer.certificates()


@router.post("/connections", status_code=201)
def connect(request: Request, seller_id: str, body: ChzConnectionInput):
    state = request.app.state
    state.sellers.get(seller_id)
    if not body.certificate and not body.true_token:
        raise InvalidInput("Выберите сертификат или введите токен True API")
    reference = state.vault.put(seller_id, {"true_token": body.true_token})
    try:
        connection = state.connections.create(
            seller_id,
            "chz",
            body.name,
            {
                "inn": body.inn,
                "environment": body.environment,
                "credential_ref": reference,
                "certificate": body.certificate,
                "oms_id": body.oms_id,
                "oms_connection": body.oms_connection,
            },
        )
    except Exception:
        state.vault.delete(seller_id, reference)
        raise
    state.operations.enqueue(seller_id, connection["id"], "nk.references", {})
    return connection_view(connection)


@router.get("/{connection_id}/overview")
def overview(request: Request, seller_id: str, connection_id: str):
    return request.app.state.marking.overview(seller_id, connection_id)


@router.put("/{connection_id}/credentials")
def renew_credentials(request: Request, seller_id: str, connection_id: str, body: CredentialsInput):
    state = request.app.state
    connection = state.marking._connection(seller_id, connection_id)
    if not body.certificate and not body.true_token:
        raise InvalidInput("Выберите УКЭП или введите новый токен")
    reference = state.vault.put(seller_id, {"true_token": body.true_token})
    try:
        config = {
            **connection["config"],
            "credential_ref": reference,
            "certificate": body.certificate,
        }
        adapter = state.registry.get("chz")
        adapter.validate_binding(seller_id, config)
        account = adapter.describe(config)
        if account.external_account_id != connection["external_account_id"]:
            raise InvalidInput("Новый доступ относится к другому аккаунту")
        with state.database.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            active = conn.execute(
                "SELECT 1 FROM operations WHERE seller_id=? AND connection_id=? "
                "AND status IN ('queued','running')",
                (seller_id, connection_id),
            ).fetchone()
            if active:
                raise InvalidInput("Дождитесь завершения операций перед обновлением доступа")
            # Concurrent renewals must not overwrite each other's credential reference.
            updated = conn.execute(
                "UPDATE connections SET config_json=?,operations_json=? "
                "WHERE seller_id=? AND id=? AND config_json=?",
                (
                    json.dumps(config, ensure_ascii=False),
                    json.dumps([v.model_dump() for v in account.operations], ensure_ascii=False),
                    seller_id,
                    connection_id,
                    json.dumps(connection["config"], ensure_ascii=False),
                ),
            )
            if updated.rowcount != 1:
                raise InvalidInput("Подключение уже изменилось; обновите страницу")
    except Exception:
        state.vault.delete(seller_id, reference)
        raise
    state.vault.delete(seller_id, connection["config"]["credential_ref"])
    return connection_view(state.connections.get(seller_id, connection_id))


@router.get("/{connection_id}/products")
def products(
    request: Request,
    seller_id: str,
    connection_id: str,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
    search: Annotated[str, Query(max_length=150)] = "",
):
    return request.app.state.marking.products(seller_id, connection_id, offset, limit, search)


@router.post("/{connection_id}/sync", status_code=202)
def sync(request: Request, seller_id: str, connection_id: str, body: SyncInput):
    return request.app.state.marking.start_sync(seller_id, connection_id, body.force)


@router.put("/{connection_id}/suz")
def configure_suz(request: Request, seller_id: str, connection_id: str, body: SuzInput):
    state = request.app.state
    connection = state.marking._connection(seller_id, connection_id)

    def ensure_idle(conn):
        if conn.execute(
            "SELECT 1 FROM operations WHERE seller_id=? AND connection_id=? AND status IN "
            "('queued','running')",
            (seller_id, connection_id),
        ).fetchone():
            raise Conflict("Дождитесь завершения заданий перед изменением СУЗ")
        if conn.execute(
            "SELECT 1 FROM marking_documents WHERE seller_id=? AND connection_id=? AND kind "
            "LIKE 'suz_%' AND state IN "
            "('prepared','submitting','accepted','processing','unknown')",
            (seller_id, connection_id),
        ).fetchone():
            raise Conflict("Завершите документы текущего СУЗ перед сменой подключения")

    with state.database.connection() as conn:
        ensure_idle(conn)
    config = {**connection["config"], **body.model_dump()}
    adapter = state.registry.get("chz")
    context = connection_context({**connection, "config": config})
    ping = adapter.suz_ping(adapter.config(context))
    with state.database.connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        ensure_idle(conn)
        changed = conn.execute(
            "UPDATE connections SET config_json=? WHERE seller_id=? AND id=? AND config_json=?",
            (
                json.dumps(config, ensure_ascii=False),
                seller_id,
                connection_id,
                json.dumps(connection["config"], ensure_ascii=False),
            ),
        )
        if changed.rowcount != 1:
            raise Conflict("Подключение изменилось; обновите страницу")
        state.marking._snapshot(conn, seller_id, connection_id, "suz", ping)
    return {"verified": True}


@router.post("/{connection_id}/edit-model")
def edit_model(request: Request, seller_id: str, connection_id: str, body: SelectionInput):
    return request.app.state.marking.edit_model(seller_id, connection_id, body.product_ids)


@router.post("/{connection_id}/prepare", status_code=201)
def prepare_document(request: Request, seller_id: str, connection_id: str, body: PrepareInput):
    return request.app.state.marking.prepare(seller_id, connection_id, body.action, body.payload)


@router.get("/{connection_id}/documents")
def documents(request: Request, seller_id: str, connection_id: str):
    return request.app.state.marking.documents(seller_id, connection_id)


@router.get("/documents/{document_id}")
def document(request: Request, seller_id: str, document_id: str):
    return request.app.state.marking.get_document(seller_id, document_id)


@router.post("/documents/{document_id}/submit", status_code=202)
def submit(request: Request, seller_id: str, document_id: str):
    return request.app.state.marking.enqueue_document(seller_id, document_id)


@router.post("/documents/{document_id}/poll", status_code=202)
def poll_document(request: Request, seller_id: str, document_id: str):
    return request.app.state.marking.enqueue_document(seller_id, document_id, poll=True)


@router.post("/documents/{document_id}/cancel")
def cancel_document(request: Request, seller_id: str, document_id: str):
    return request.app.state.marking.cancel_document(seller_id, document_id)


@router.post("/documents/{document_id}/retry")
def retry_document(request: Request, seller_id: str, document_id: str):
    return request.app.state.marking.retry_document(seller_id, document_id)


@router.post("/documents/{document_id}/reconcile")
def reconcile_document(request: Request, seller_id: str, document_id: str, body: ReconcileInput):
    return request.app.state.marking.reconcile_document(seller_id, document_id, body.external_id)


@router.get("/{connection_id}/codes")
def codes(
    request: Request,
    seller_id: str,
    connection_id: str,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
):
    return request.app.state.marking.codes(seller_id, connection_id, offset, limit)


@router.get("/{connection_id}/blocks/{block_id}/export")
def export_block(request: Request, seller_id: str, connection_id: str, block_id: str):
    return request.app.state.marking.code_export(seller_id, connection_id, block_id)


@router.post("/{connection_id}/utilisation", status_code=201)
def utilisation(request: Request, seller_id: str, connection_id: str, body: CodesSelectionInput):
    return request.app.state.marking.utilisation_from_codes(
        seller_id, connection_id, body.model_dump()
    )
