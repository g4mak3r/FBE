import json
from typing import Annotated, Literal

from fastapi import APIRouter, Query, Request
from pydantic import Field, JsonValue

from fbe_flow.core.errors import Conflict, InvalidInput
from fbe_flow.core.models import Contract, Text
from fbe_flow.integrations.commerce import CommerceConfig
from fbe_flow.web.routes import connection_view

router = APIRouter(prefix="/api/sellers/{seller_id}/commerce")


class ConnectionInput(Contract):
    name: Text = Field(max_length=120)
    adapter_key: Literal["ozon", "kit"]
    token: str = Field(min_length=8, max_length=8192)
    client_id: str | None = Field(default=None, max_length=30)
    tin: str | None = Field(default=None, pattern=r"^([0-9]{10}|[0-9]{12})$")
    read_only: bool = False


class CredentialsInput(Contract):
    token: str = Field(min_length=8, max_length=8192)


class LinkInput(Contract):
    product_id: Text
    chz_connection_id: Text
    chz_product_id: Text
    gtin: str = Field(pattern=r"^[0-9]{14}$")
    product_group: Text


class ActionInput(Contract):
    kind: Literal["codes", "ship", "cancel", "confirm", "complete", "prices", "stocks"]
    payload: dict[str, JsonValue]


def configuration(state, seller, key, reference, client_id, tin, read_only):
    if key == "ozon" and (
        not client_id or not client_id.isascii() or not client_id.isdigit() or int(client_id) <= 0
    ):
        raise InvalidInput("Укажите Client-Id Ozon")
    if key == "kit" and (
        not tin or not tin.isascii() or not tin.isdigit() or len(tin) not in {10, 12}
    ):
        raise InvalidInput("Укажите ИНН организации магазина для связи с ЧЗ")
    config = CommerceConfig(
        credential_ref=reference, account_id=client_id or "", tin=tin or "", read_only=read_only
    )
    adapter = state.registry.get(key)
    adapter.validate_binding(seller, config.model_dump())
    identity = adapter.identity(config)
    return config.model_copy(
        update={"account_id": identity["account_id"], "tin": identity["tin"]}
    ).model_dump()


@router.post("/connections", status_code=201)
def connect(request: Request, seller_id: str, body: ConnectionInput):
    state = request.app.state
    state.sellers.get(seller_id)
    token = body.token.removeprefix("Bearer ").strip()
    reference = state.vault.put(seller_id, {body.adapter_key + "_token": token})
    try:
        config = configuration(
            state, seller_id, body.adapter_key, reference, body.client_id, body.tin, body.read_only
        )
        value = state.connections.create(seller_id, body.adapter_key, body.name, config)
    except Exception:
        state.vault.delete(seller_id, reference)
        raise
    return connection_view(value)


@router.put("/{connection_id}/credentials")
def credentials(request: Request, seller_id: str, connection_id: str, body: CredentialsInput):
    state = request.app.state
    value = state.commerce._connection(seller_id, connection_id)
    key, old = value["adapter_key"], value["config"]
    reference = state.vault.put(
        seller_id, {key + "_token": body.token.removeprefix("Bearer ").strip()}
    )
    try:
        config = configuration(
            state,
            seller_id,
            key,
            reference,
            old["account_id"] if key == "ozon" else None,
            old["tin"],
            old["read_only"],
        )
        adapter = state.registry.get(key)
        account = adapter.describe(config)
        if (
            account.external_account_id != value["external_account_id"]
            or config["tin"] != old["tin"]
        ):
            raise InvalidInput("Ключ относится к другому аккаунту или ИНН")
        with state.database.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute(
                "SELECT 1 FROM operations WHERE seller_id=? AND connection_id=? "
                "AND status IN ('queued','running')",
                (seller_id, connection_id),
            ).fetchone():
                raise Conflict("Дождитесь завершения операций перед сменой ключа")
            updated = conn.execute(
                "UPDATE connections SET config_json=?,operations_json=? WHERE seller_id=? AND "
                "id=? AND config_json=?",
                (
                    json.dumps(config, ensure_ascii=False),
                    json.dumps([v.model_dump() for v in account.operations], ensure_ascii=False),
                    seller_id,
                    connection_id,
                    json.dumps(old, ensure_ascii=False),
                ),
            )
            if updated.rowcount != 1:
                raise Conflict("Подключение уже изменилось")
    except Exception:
        state.vault.delete(seller_id, reference)
        raise
    state.vault.delete(seller_id, old["credential_ref"])
    return connection_view(state.connections.get(seller_id, connection_id))


@router.get("/{connection_id}/overview")
def overview(request: Request, seller_id: str, connection_id: str):
    return request.app.state.commerce.overview(seller_id, connection_id)


@router.post("/{connection_id}/sync", status_code=202)
def sync(request: Request, seller_id: str, connection_id: str):
    return request.app.state.commerce.start_sync(seller_id, connection_id)


@router.get("/{connection_id}/records/{kind}")
def records(
    request: Request,
    seller_id: str,
    connection_id: str,
    kind: Literal["products", "warehouses", "orders", "supplies", "returns"],
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
    search: Annotated[str, Query(max_length=150)] = "",
    status: Annotated[str, Query(max_length=100)] = "",
    stage: Annotated[str, Query(max_length=30)] = "",
    warehouse: Annotated[str, Query(max_length=120)] = "",
):
    return request.app.state.commerce.records(
        seller_id, connection_id, kind, offset, limit, search, status, stage, warehouse
    )


@router.get("/{connection_id}/orders/{order_id}/details")
def details(request: Request, seller_id: str, connection_id: str, order_id: str):
    return request.app.state.commerce.order_details(seller_id, connection_id, order_id)


@router.get("/{connection_id}/orders/{order_id}/items/{item_id}/available-codes")
def available_codes(
    request: Request,
    seller_id: str,
    connection_id: str,
    order_id: str,
    item_id: str,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
):
    return request.app.state.commerce.available_codes(
        seller_id, connection_id, order_id, item_id, offset, limit
    )


@router.get("/{connection_id}/cancel-reasons")
def reasons(request: Request, seller_id: str, connection_id: str):
    value = request.app.state.commerce._connection(seller_id, connection_id)
    if value["adapter_key"] != "ozon":
        raise InvalidInput("Причины доступны для Ozon")
    adapter, config = request.app.state.commerce.adapter(value)
    return adapter.cancel_reasons(config)


@router.get("/{connection_id}/links")
def links(request: Request, seller_id: str, connection_id: str):
    return request.app.state.commerce.links(seller_id, connection_id)


@router.post("/{connection_id}/links", status_code=201)
def link(request: Request, seller_id: str, connection_id: str, body: LinkInput):
    return request.app.state.commerce.link(seller_id, connection_id, body.model_dump())


@router.delete("/{connection_id}/links/{link_id}")
def unlink(request: Request, seller_id: str, connection_id: str, link_id: str):
    return request.app.state.commerce.unlink(seller_id, connection_id, link_id)


@router.get("/{connection_id}/actions")
def actions(request: Request, seller_id: str, connection_id: str):
    return request.app.state.commerce.actions(seller_id, connection_id)


@router.post("/{connection_id}/actions", status_code=201)
def prepare(request: Request, seller_id: str, connection_id: str, body: ActionInput):
    return request.app.state.commerce.prepare(seller_id, connection_id, body.kind, body.payload)


@router.get("/actions/{action_id}")
def action(request: Request, seller_id: str, action_id: str):
    return request.app.state.commerce.action(seller_id, action_id)


@router.post("/actions/{action_id}/send", status_code=202)
def send(request: Request, seller_id: str, action_id: str):
    return request.app.state.commerce.enqueue(seller_id, action_id)


@router.post("/actions/{action_id}/reconcile", status_code=202)
def reconcile(request: Request, seller_id: str, action_id: str):
    return request.app.state.commerce.enqueue(seller_id, action_id, reconcile=True)


@router.post("/actions/{action_id}/cancel")
def cancel(request: Request, seller_id: str, action_id: str):
    return request.app.state.commerce.cancel(seller_id, action_id)
