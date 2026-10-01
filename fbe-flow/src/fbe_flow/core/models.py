from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, JsonValue, StringConstraints

Text = Annotated[str, StringConstraints(strict=True, strip_whitespace=True, min_length=1)]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class Product(Contract):
    external_id: Text
    title: Text
    sku: str | None = None
    category: dict[str, JsonValue] | None = None
    identifiers: dict[str, list[str]] = Field(default_factory=dict)
    attributes: dict[str, JsonValue] = Field(default_factory=dict)


class Warehouse(Contract):
    external_id: Text
    name: Text
    attributes: dict[str, JsonValue] = Field(default_factory=dict)


class Supply(Contract):
    external_id: Text
    status: Text
    warehouse_external_id: Text | None = None
    attributes: dict[str, JsonValue] = Field(default_factory=dict)


class Order(Contract):
    external_id: Text
    status: Text
    warehouse_external_id: Text | None = None
    supply_external_id: Text | None = None
    attributes: dict[str, JsonValue] = Field(default_factory=dict)


class NormalizedBatch(Contract):
    products: tuple[Product, ...] = ()
    warehouses: tuple[Warehouse, ...] = ()
    supplies: tuple[Supply, ...] = ()
    orders: tuple[Order, ...] = ()


class OperationResult(Contract):
    """An integration outcome, optionally accompanied by normalized source records."""

    batch: NormalizedBatch | None = None
    data: dict[str, JsonValue] = Field(default_factory=dict)


class OperationSpec(Contract):
    key: Text
    label: Text


class AccountInfo(Contract):
    external_account_id: Text
    operations: tuple[OperationSpec, ...] = ()


class ConnectionContext(Contract):
    seller_id: Text
    connection_id: Text
    external_account_id: Text
    config: dict[str, JsonValue]
