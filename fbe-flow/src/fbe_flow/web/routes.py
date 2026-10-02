from typing import Annotated, Literal

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import Field, JsonValue

from fbe_flow.core.models import Contract, Text

router = APIRouter()
Page = Literal[
    "overview",
    "connections",
    "operations",
    "settings",
    "marking",
    "wb",
    "ozon",
    "marketplaces",
    "store",
]
Kind = Literal["products", "orders", "supplies", "warehouses"]


class SellerInput(Contract):
    name: Annotated[Text, Field(max_length=120)]


class ConnectionInput(Contract):
    adapter_key: Text
    name: Annotated[Text, Field(max_length=120)]
    config: dict[str, JsonValue] = Field(default_factory=dict)


class OperationInput(Contract):
    connection_id: Text
    operation_key: Text
    payload: dict[str, JsonValue] = Field(default_factory=dict)
    scope_key: Text | None = None


class SettingInput(Contract):
    value: JsonValue


def connection_view(connection: dict) -> dict:
    value = {key: value for key, value in connection.items() if key != "config"}
    if connection["adapter_key"] == "chz":
        value["environment"] = connection["config"].get("environment", "sandbox")
    if connection["adapter_key"] in {"wb", "ozon", "kit"}:
        value["tin"] = connection["config"]["tin"]
        value["read_only"] = connection["config"]["read_only"]
        value["tin_verified"] = connection["adapter_key"] != "kit"
    return value


def render_shell(request: Request, seller_id: str | None, page: Page):
    state = request.app.state
    seller = state.sellers.get(seller_id) if seller_id else None
    connections = state.connections.list(seller_id) if seller_id else []
    settings = state.settings.list(seller_id) if seller_id else {}
    channel = (
        page
        if page in {"wb", "ozon"}
        else request.query_params.get("channel", settings.get("marketplace.selected", "wb"))
    )
    if channel not in {"wb", "ozon"}:
        channel = "wb"
    context = {
        "sellers": state.sellers.list(),
        "seller": seller,
        "page": page,
        "connections": [connection_view(item) for item in connections],
        "adapters": state.registry.descriptors(),
        "operations": state.operations.list(seller_id) if seller_id else [],
        "settings": settings,
        "channel": channel,
        "store_name": settings.get("store.name", "Интернет-магазин"),
    }
    return state.templates.TemplateResponse(request=request, name="shell.html", context=context)


@router.get("/")
def home(request: Request):
    sellers = request.app.state.sellers.list()
    if sellers:
        return RedirectResponse(f"/sellers/{sellers[0]['id']}/overview", status_code=303)
    return render_shell(request, None, "overview")


@router.get("/sellers/{seller_id}/{page}")
def shell(request: Request, seller_id: str, page: Page):
    return render_shell(request, seller_id, page)


@router.get("/health")
def health(request: Request):
    state = request.app.state
    with state.database.connection() as conn:
        conn.execute("SELECT 1").fetchone()
    running = state.worker.alive
    healthy = running or not state.config.worker_enabled
    return JSONResponse(
        {"status": "ok" if healthy else "degraded", "worker": "running" if running else "stopped"},
        status_code=200 if healthy else 503,
    )


@router.get("/api/adapters")
def adapters(request: Request):
    return request.app.state.registry.descriptors()


@router.get("/api/sellers")
def sellers(request: Request):
    return request.app.state.sellers.list()


@router.post("/api/sellers", status_code=201)
def create_seller(request: Request, body: SellerInput):
    return request.app.state.sellers.create(body.name)


@router.get("/api/sellers/{seller_id}/connections")
def connections(request: Request, seller_id: str):
    return [connection_view(item) for item in request.app.state.connections.list(seller_id)]


@router.post("/api/sellers/{seller_id}/connections", status_code=201)
def create_connection(request: Request, seller_id: str, body: ConnectionInput):
    return connection_view(
        request.app.state.connections.create(seller_id, body.adapter_key, body.name, body.config)
    )


@router.get("/api/sellers/{seller_id}/connections/{connection_id}")
def connection(request: Request, seller_id: str, connection_id: str):
    return connection_view(request.app.state.connections.get(seller_id, connection_id))


@router.get("/api/sellers/{seller_id}/operations")
def operations(request: Request, seller_id: str):
    return request.app.state.operations.list(seller_id)


@router.post("/api/sellers/{seller_id}/operations", status_code=202)
def enqueue_operation(request: Request, seller_id: str, body: OperationInput):
    return request.app.state.operations.enqueue(
        seller_id, body.connection_id, body.operation_key, body.payload, body.scope_key
    )


@router.get("/api/sellers/{seller_id}/operations/{operation_id}")
def operation(request: Request, seller_id: str, operation_id: str):
    return request.app.state.operations.get(seller_id, operation_id)


@router.get("/api/sellers/{seller_id}/settings")
def settings(request: Request, seller_id: str):
    return request.app.state.settings.list(seller_id)


@router.put("/api/sellers/{seller_id}/settings/{key}")
def set_setting(request: Request, seller_id: str, key: str, body: SettingInput):
    request.app.state.settings.set(seller_id, key, body.value)
    return {"key": key, "value": body.value}


@router.get("/api/sellers/{seller_id}/records/{kind}")
def records(
    request: Request, seller_id: str, kind: Kind, limit: Annotated[int, Query(ge=1, le=100)] = 100
):
    return request.app.state.records.list(seller_id, kind, limit)


@router.get("/api/sellers/{seller_id}/records/{kind}/{record_id}")
def record(request: Request, seller_id: str, kind: Kind, record_id: str):
    return request.app.state.records.get(seller_id, kind, record_id)
