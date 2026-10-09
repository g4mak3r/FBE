import json
from typing import Annotated, Literal

from fastapi import APIRouter, Query, Request
from pydantic import Field, JsonValue

from fbe_flow.core.errors import Conflict, InvalidInput
from fbe_flow.core.models import Contract, Text
from fbe_flow.integrations.wb.adapter import WbConfig, token_claims
from fbe_flow.web.routes import connection_view

router = APIRouter(prefix="/api/sellers/{seller_id}/wb")


class ConnectionInput(Contract):
    name: Text = Field(max_length=120)
    token: str = Field(min_length=20, max_length=8192)


class TokenInput(Contract):
    token: str = Field(min_length=20, max_length=8192)


class LinkInput(Contract):
    product_id: Text
    chrt_id: str = Field(pattern=r"^[1-9][0-9]*$")
    chz_connection_id: Text
    chz_product_id: Text
    gtin: str = Field(pattern=r"^[0-9]{14}$")
    product_group: Text


class PrepareInput(Contract):
    kind: Literal[
        "sgtin", "supply_create", "supply_add", "supply_deliver", "supply_delete", "expiration"
    ]
    payload: dict[str, JsonValue]


def configuration(state, seller, reference, token):
    claims = token_claims(token)
    provisional = WbConfig(
        credential_ref=reference, account_id="", tin="", read_only=claims["read_only"]
    )
    adapter = state.registry.get("wb")
    value = adapter.identity(provisional)
    if not isinstance(value, dict) or not all(
        isinstance(value.get(k), str) and value[k] for k in ("sid", "tin")
    ):
        raise InvalidInput("WB не подтвердил идентификатор продавца и ИНН")
    return provisional.model_copy(
        update={"account_id": value["sid"], "tin": value["tin"]}
    ).model_dump()


@router.post("/connections", status_code=201)
def connect(request: Request, seller_id: str, body: ConnectionInput):
    state = request.app.state
    state.sellers.get(seller_id)
    token = body.token.removeprefix("Bearer ").strip()
    token_claims(token)
    reference = state.vault.put(seller_id, {"wb_token": token})
    try:
        config = configuration(state, seller_id, reference, token)
        value = state.connections.create(seller_id, "wb", body.name, config)
    except Exception:
        state.vault.delete(seller_id, reference)
        raise
    return connection_view(value)


@router.put("/{connection_id}/credentials")
def credentials(request: Request, seller_id: str, connection_id: str, body: TokenInput):
    state = request.app.state
    connection = state.fulfillment._connection(seller_id, connection_id)
    token = body.token.removeprefix("Bearer ").strip()
    token_claims(token)
    reference = state.vault.put(seller_id, {"wb_token": token})
    try:
        config = configuration(state, seller_id, reference, token)
        adapter = state.registry.get("wb")
        adapter.validate_binding(seller_id, config)
        account = adapter.describe(config)
        if (
            account.external_account_id != connection["external_account_id"]
            or config["tin"] != connection["config"]["tin"]
        ):
            raise InvalidInput("Новый токен относится к другому аккаунту или ИНН")
        with state.database.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute(
                "SELECT 1 FROM operations WHERE seller_id=? AND connection_id=? AND "
                "status IN ('queued','running')",
                (seller_id, connection_id),
            ).fetchone():
                raise Conflict("Дождитесь завершения операций WB перед сменой токена")
            updated = conn.execute(
                "UPDATE connections SET config_json=?,operations_json=? WHERE "
                "seller_id=? AND id=? AND config_json=?",
                (
                    json.dumps(config, ensure_ascii=False),
                    json.dumps([v.model_dump() for v in account.operations], ensure_ascii=False),
                    seller_id,
                    connection_id,
                    json.dumps(connection["config"], ensure_ascii=False),
                ),
            )
            if updated.rowcount != 1:
                raise Conflict("Подключение уже изменилось; обновите страницу")
    except Exception:
        state.vault.delete(seller_id, reference)
        raise
    state.vault.delete(seller_id, connection["config"]["credential_ref"])
    return connection_view(state.connections.get(seller_id, connection_id))


@router.get("/{connection_id}/overview")
def overview(request: Request, seller_id: str, connection_id: str):
    return request.app.state.fulfillment.overview(seller_id, connection_id)


@router.post("/{connection_id}/sync", status_code=202)
def sync(request: Request, seller_id: str, connection_id: str):
    return request.app.state.fulfillment.start_sync(seller_id, connection_id)


@router.post("/{connection_id}/refresh", status_code=202)
def refresh(request: Request, seller_id: str, connection_id: str):
    return request.app.state.fulfillment.ensure_refresh(seller_id, connection_id)


@router.get("/{connection_id}/records/{kind}")
def records(
    request: Request,
    seller_id: str,
    connection_id: str,
    kind: Literal["products", "warehouses", "orders", "supplies"],
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
    search: Annotated[str, Query(max_length=150)] = "",
    supply: str | None = None,
    stage: Annotated[str, Query(max_length=30)] = "",
    status: Annotated[str, Query(max_length=100)] = "",
    warehouse: Annotated[str, Query(max_length=120)] = "",
    queue: Literal["", "new", "current", "archive"] = "",
):
    return request.app.state.fulfillment.records(
        seller_id,
        connection_id,
        kind,
        offset,
        limit,
        search,
        supply,
        stage,
        status,
        warehouse,
        queue,
    )


@router.get("/{connection_id}/orders/{order_id}/available-codes")
def available_codes(
    request: Request,
    seller_id: str,
    connection_id: str,
    order_id: str,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
):
    return request.app.state.fulfillment.available_codes(
        seller_id, connection_id, order_id, offset, limit
    )


@router.get("/{connection_id}/links")
def links(request: Request, seller_id: str, connection_id: str):
    return request.app.state.fulfillment.links(seller_id, connection_id)


@router.post("/{connection_id}/links", status_code=201)
def link(request: Request, seller_id: str, connection_id: str, body: LinkInput):
    return request.app.state.fulfillment.link(seller_id, connection_id, body.model_dump())


@router.get("/{connection_id}/supplies/{supply_id}/details")
def supply_details(request: Request, seller_id: str, connection_id: str, supply_id: str):
    return request.app.state.fulfillment.supply_details(seller_id, connection_id, supply_id)


@router.delete("/{connection_id}/links/{link_id}")
def unlink(request: Request, seller_id: str, connection_id: str, link_id: str):
    return request.app.state.fulfillment.unlink(seller_id, connection_id, link_id)


@router.get("/{connection_id}/actions")
def actions(request: Request, seller_id: str, connection_id: str):
    return request.app.state.fulfillment.actions(seller_id, connection_id)


@router.post("/{connection_id}/actions", status_code=201)
def prepare(request: Request, seller_id: str, connection_id: str, body: PrepareInput):
    return request.app.state.fulfillment.prepare(seller_id, connection_id, body.kind, body.payload)


@router.get("/actions/{action_id}")
def action(request: Request, seller_id: str, action_id: str):
    return request.app.state.fulfillment.action(seller_id, action_id)


@router.post("/actions/{action_id}/send", status_code=202)
def send(request: Request, seller_id: str, action_id: str):
    return request.app.state.fulfillment.enqueue(seller_id, action_id)


@router.post("/actions/{action_id}/reconcile", status_code=202)
def reconcile(request: Request, seller_id: str, action_id: str):
    return request.app.state.fulfillment.enqueue(seller_id, action_id, reconcile=True)


@router.post("/actions/{action_id}/cancel")
def cancel(request: Request, seller_id: str, action_id: str):
    return request.app.state.fulfillment.cancel(seller_id, action_id)


@router.post("/actions/{action_id}/retry-codes", status_code=202)
def retry_codes(request: Request, seller_id: str, action_id: str):
    return request.app.state.fulfillment.retry_codes(seller_id, action_id)


class PackingOrders(Contract):
    order_ids: list[Text] = Field(min_length=1, max_length=100)


class ExpiryItem(Contract):
    order_id: Text
    batch_id: Text


class PackingExpiry(Contract):
    items: list[ExpiryItem] = Field(min_length=1, max_length=100)


class PrintInput(Contract):
    kind: Literal["orders", "codes", "supply"]
    order_ids: list[Text] = Field(default_factory=list, max_length=100)
    supply_id: Text | None = None


@router.post("/{connection_id}/packing/expiration")
def packing_expiry(request: Request, seller_id: str, connection_id: str, body: PackingExpiry):
    from fbe_flow.modules.packing import bulk_expiration

    return bulk_expiration(
        request.app.state.fulfillment,
        seller_id,
        connection_id,
        [v.model_dump() for v in body.items],
    )


@router.post("/{connection_id}/packing/codes")
def packing_codes(request: Request, seller_id: str, connection_id: str, body: PackingOrders):
    from fbe_flow.modules.packing import bulk_codes

    return bulk_codes(request.app.state.fulfillment, seller_id, connection_id, body.order_ids)


@router.post("/{connection_id}/packing/print", status_code=201)
def packing_print(request: Request, seller_id: str, connection_id: str, body: PrintInput):
    from fbe_flow.modules.packing import create_print_job

    return create_print_job(
        request.app.state.fulfillment,
        seller_id,
        connection_id,
        body.kind,
        body.order_ids,
        body.supply_id,
    )


@router.post("/print/{job_id}/confirm")
def confirm_print(request: Request, seller_id: str, job_id: str):
    from fbe_flow.modules.packing import print_job

    print_job(request.app.state.fulfillment, seller_id, job_id)
    with request.app.state.database.connection() as conn:
        conn.execute(
            "UPDATE wb_print_jobs SET confirmed_at=COALESCE(confirmed_at,"
            "strftime('%Y-%m-%dT%H:%M:%fZ','now')) WHERE seller_id=? AND id=?",
            (seller_id, job_id),
        )
    return {"confirmed": True}
