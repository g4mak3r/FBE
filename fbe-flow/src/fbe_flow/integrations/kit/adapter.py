"""Yandex KIT's own API. Unsupported checkout/receipt writes stay in its cabinet."""

from decimal import Decimal
from urllib.parse import quote

from fbe_flow.core.errors import Conflict, InvalidInput
from fbe_flow.core.models import NormalizedBatch, OperationResult, Order, Product, Warehouse
from fbe_flow.integrations.chz.http import RemoteError
from fbe_flow.integrations.commerce import ProtectedAdapter, objects, positive


def identifier(value):
    if not isinstance(value, str) or not value or len(value) > 100:
        raise RemoteError(None, "kit_identifier_invalid")
    return value


class KitAdapter(ProtectedAdapter):
    key, label = "kit", "Яндекс KIT"
    host, interval = "https://api.kit.yandex.net", 0.35
    phases = ("warehouses", "products", "orders")

    def headers(self, config, token):
        return {"Authorization": "Bearer " + token}

    def identity(self, config):
        store = self._call(config, "GET", "/v1/store")
        user = self._call(config, "GET", "/v1/users/current")
        if not isinstance(store, dict) or not isinstance(user, dict) or not user.get("role_name"):
            raise RemoteError(None, "kit_identity_invalid")
        return {
            "account_id": identifier(store.get("id")),
            # Public /store does not return a legal entity. Never invent a verified INN.
            "tin": config.tin,
            "tin_verified": False,
            "store": store,
            "role": user["role_name"],
        }

    def product(self, value):
        barcode = value.get("barcode")
        return Product(
            external_id=identifier(value["id"]),
            title=value["name"],
            sku=value.get("sku"),
            identifiers={"barcode": [barcode] if isinstance(barcode, str) and barcode else []},
            attributes={"source": value, "variant": value["id"]},
        )

    def warehouse(self, value):
        return Warehouse(
            external_id=identifier(value["id"]),
            name=value["title"],
            attributes={"source": value},
        )

    def items(self, source):
        result, ids = [], set()
        for chunk in objects(source, "delivery_chunks"):
            for item in objects(chunk, "items"):
                item_id = identifier(item["id"])
                if item_id in ids:
                    raise RemoteError(None, "kit_item_duplicate")
                ids.add(item_id)
                result.append(
                    {
                        "item_id": item_id,
                        "variant": identifier(item["product_variant_id"]),
                        "quantity": positive(item["quantity"]),
                        "title": item.get("name", item["product_variant_id"]),
                        "price": item.get("final_price"),
                        "chunk_id": chunk["id"],
                        "truthful_label": item.get("truthful_label"),
                        "refused_count": item.get("refused_count", 0),
                        "deleted": bool(item.get("is_product_variant_deleted")),
                    }
                )
        return result

    def order(self, value):
        return Order(
            external_id=identifier(value["id"]),
            status=value["status"],
            attributes={"source": value, "items": self.items(value)},
        )

    def sync(self, config, payload):
        self.account(config)
        phase, page = payload["phase"], payload.get("source_page", 1)
        path, key = {
            "warehouses": ("/v1/warehouses", "warehouses"),
            "products": ("/v1/variants", "variants"),
            "orders": ("/v1/orders", "orders"),
        }[phase]
        params = {"page": page, "per_page": 100}
        if phase == "warehouses":
            params["status"] = ["ACTIVE", "ARCHIVED"]
        elif phase == "products":
            params["status"] = ["PUBLISHED", "HIDDEN", "ARCHIVED"]
        value = self._call(config, "GET", path, params=params)
        values = objects(value, key)
        total = value.get("total_count")
        if type(total) is not int or total < 0 or len(values) > 100:
            raise RemoteError(None, "kit_pagination_invalid")
        start = (page - 1) * 100
        if start + len(values) < total:
            if len(values) != 100:
                raise RemoteError(None, "kit_pagination_incomplete")
            following = {"phase": phase, "source_page": page + 1}
        else:
            index = self.phases.index(phase) + 1
            following = {"phase": self.phases[index]} if index < len(self.phases) else None
        normalize = {"products": self.product, "orders": self.order, "warehouses": self.warehouse}
        batch = NormalizedBatch(**{phase: tuple(normalize[phase](v) for v in values)})
        return OperationResult(
            batch=batch,
            data={
                "sync": {
                    "phase": phase,
                    "count": len(values),
                    "following": following,
                    "source_total": total,
                }
            },
        )

    def get_order(self, config, external_id):
        self.account(config)
        value = self._call(config, "GET", "/v1/orders/" + quote(external_id, safe=""))
        if not isinstance(value, dict) or value.get("id") != external_id:
            raise RemoteError(None, "kit_order_mismatch")
        self.items(value)
        return value

    def get_variant(self, config, external_id):
        value = self._call(config, "GET", "/v1/variants/" + quote(external_id, safe=""))
        if not isinstance(value, dict) or value.get("id") != external_id:
            raise RemoteError(None, "kit_variant_mismatch")
        return value

    @staticmethod
    def field_value(source, kind, item):
        if kind == "prices":
            return {
                k: source.get("pricing", {}).get(k)
                for k in ("price", "manual_discount_price")
                if k in item
            }
        stocks = objects(source, "stocks")
        value = next((v for v in stocks if v.get("warehouse_id") == item["warehouse_id"]), None)
        if value is None or type(value.get("quantity")) is not int:
            raise Conflict("Остаток пары товар/склад отсутствует; обновите KIT")
        return {"quantity": value["quantity"]}

    @staticmethod
    def values_match(left, right):
        for key, value in right.items():
            other = left.get(key)
            if value is None or other is None:
                if value is not other:
                    return False
            elif key in {"price", "manual_discount_price"}:
                if Decimal(str(value)) != Decimal(str(other)):
                    return False
            elif value != other:
                return False
        return True

    def preflight(self, config, kind, body):
        self.account(config)
        if kind in {"prices", "stocks"}:
            for item, before in zip(body["wire"]["items"], body["before"], strict=True):
                value = self.get_variant(config, item["variant_id"])
                if value.get("status") == "ARCHIVED":
                    raise Conflict("Архивный товар нельзя изменить")
                current = self.field_value(value, kind, item)
                expected = {
                    k: v for k, v in item.items() if k not in {"variant_id", "warehouse_id"}
                }
                if not self.values_match(current, before) and not self.values_match(
                    current, expected
                ):
                    raise Conflict("Цена или остаток изменились после подготовки")
            return
        source = self.get_order(config, body["order_external_id"])
        if kind == "codes":
            item = next((v for v in self.items(source) if v["item_id"] == body["item_id"]), None)
            if (
                not item
                or item["variant"] != body["variant"]
                or item["quantity"] != len(body["sgtins"])
                or item["deleted"]
                or item["refused_count"]
            ):
                raise Conflict("Позиция или количество KIT изменились; обновите заказ")
            if source["status"] in {
                "CANCELLED",
                "COMPLETED",
                "DELIVERED",
                "FULL_REFUND",
                "PARTIAL_REFUND",
                "CANCELLATION_IN_PROGRESS",
                "CREATING_FINAL_RECEIPTS",
            }:
                raise Conflict("Заказ KIT уже завершён, отменён или возвращается")
            if (
                item["quantity"] == 1
                and item["truthful_label"]
                and item["truthful_label"] != body["sgtins"][0]
            ):
                raise Conflict("В KIT уже указан другой КМ; замена через Flow запрещена")
            return
        if self.items(source) != body["items"]:
            raise Conflict("Состав заказа KIT изменился")
        if kind == "confirm" and source["status"] != "WAIT_FOR_CONFIRMATION":
            raise Conflict("Заказ больше не ожидает подтверждения")
        if kind == "complete":
            if source["status"] != "WAIT_FOR_DELIVERY":
                raise Conflict("Заказ должен ожидать доставки")
            for chunk in objects(source, "delivery_chunks"):
                info = chunk.get("delivery_info", {})
                own = info.get("method") == "SELF_PICK_UP" or (
                    info.get("method") == "COURIER"
                    and info.get("courier_delivery_service_type") == "MERCHANT_SHIP"
                )
                if not own:
                    raise InvalidInput("Доставку внешней службы подтверждает сама служба")
            if body.get("marking_actions"):
                if any(v.get("refused_count") for v in self.items(source)):
                    raise InvalidInput("Частичный выкуп обработайте в кабинете KIT")
        if kind == "cancel" and source["status"] in {
            "COMPLETED",
            "DELIVERED",
            "FULL_REFUND",
            "PARTIAL_REFUND",
            "CANCELLED",
            "CREATING_FINAL_RECEIPTS",
            "CANCELLATION_IN_PROGRESS",
        }:
            raise InvalidInput("Для этого состояния отмена заказа недоступна")

    def send(self, config, kind, body):
        if kind in {"prices", "stocks"}:
            path = "/v1/variants/" + kind + "/bulk_update"
            reply = self._call(config, "POST", path, body=body["wire"], write=True)
        else:
            suffix = {"confirm": "confirm", "cancel": "cancel", "complete": "delivery/complete"}[
                kind
            ]
            path = "/v1/orders/" + quote(body["order_external_id"], safe="") + "/" + suffix
            reply = self._call(config, "POST", path, write=True)
        return {"receipt": reply}

    def reconcile(self, config, kind, body, acknowledged):
        self.account(config)
        if kind in {"prices", "stocks"}:
            matched, missing, sources = [], [], []
            for item in body["wire"]["items"]:
                source = self.get_variant(config, item["variant_id"])
                sources.append(source)
                actual = self.field_value(source, kind, item)
                expected = {
                    k: v for k, v in item.items() if k not in {"variant_id", "warehouse_id"}
                }
                (matched if self.values_match(actual, expected) else missing).append(item)
            state = (
                "confirmed"
                if not missing
                else "partial"
                if matched
                else ("pending" if acknowledged else "unknown")
            )
            return {
                "state": state,
                "matched": matched,
                "missing": missing,
                "remote_verified": not missing,
                "product_sources": sources,
            }
        source = self.get_order(config, body["order_external_id"])
        status = source["status"]
        if kind == "codes":
            item = next((v for v in self.items(source) if v["item_id"] == body["item_id"]), None)
            # The public field is one string. Multi-unit labels are not a documented list format.
            if item and item["quantity"] == 1 and item["truthful_label"] == body["sgtins"][0]:
                return {"state": "confirmed", "source": source, "remote_verified": True}
            return {
                "state": "awaiting_manual",
                "source": source,
                "remote_verified": False,
                "reason": "Внесите КМ в заказ в кабинете KIT. API не поддерживает запись КМ.",
            }
        if kind == "confirm" and status in {
            "CREATING_INITIAL_RECEIPT",
            "SETUP_DELIVERY",
            "WAIT_FOR_DELIVERY",
            "CREATING_FINAL_RECEIPTS",
            "DELIVERED",
            "COMPLETED",
        }:
            return {"state": "confirmed", "source": source, "remote_verified": True}
        if kind == "cancel" and status in {"CANCELLED", "CANCELLATION_IN_PROGRESS"}:
            return {"state": "confirmed", "source": source, "remote_verified": True}
        if kind == "complete" and status in {"DELIVERED", "CREATING_FINAL_RECEIPTS", "COMPLETED"}:
            return {"state": "confirmed", "source": source, "remote_verified": True}
        return {"state": "pending" if acknowledged else "unknown", "source": source}
