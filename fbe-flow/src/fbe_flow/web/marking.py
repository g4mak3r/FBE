import json
from typing import Annotated, Literal

from fastapi import APIRouter, Query, Request
from pydantic import Field

from fbe_flow.core.errors import InvalidInput
from fbe_flow.core.models import Contract, Text
from fbe_flow.web.routes import connection_view

router = APIRouter(prefix="/api/sellers/{seller_id}/marking")


class ChzConnectionInput(Contract):
    name: Annotated[Text, Field(max_length=120)]
    environment: Literal["sandbox", "production"] = "sandbox"
    inn: str = Field(pattern=r"^\d{10}(\d{2})?$")
    certificate: str = Field(default="", pattern=r"^([A-Fa-f0-9]{40})?$")
    true_token: str = Field(default="", max_length=8192)


class SyncInput(Contract):
    force: bool = False


class CredentialsInput(Contract):
    certificate: str = Field(default="", pattern=r"^([A-Fa-f0-9]{40})?$")
    true_token: str = Field(default="", max_length=8192)


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
