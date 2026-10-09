"""WB FBS protocol. Reads are paginated; writes are called only by the local journal."""

import base64
import json
import re
import time
from threading import Lock
from urllib.parse import quote

from fbe_flow.core.credentials import credential_owner
from fbe_flow.core.errors import Conflict, InvalidInput
from fbe_flow.core.models import (
    AccountInfo,
    Contract,
    NormalizedBatch,
    OperationResult,
    OperationSpec,
    Order,
    Product,
    Supply,
    Warehouse,
)
from fbe_flow.integrations.chz.http import HttpTransport, RemoteError, redact

HOSTS = {
    "common": "https://common-api.wildberries.ru",
    "marketplace": "https://marketplace-api.wildberries.ru",
    "content": "https://content-api.wildberries.ru",
}


class WbConfig(Contract):
    credential_ref: str
    account_id: str
    tin: str
    read_only: bool


def token_claims(token):
    """Unverified claims only constrain access; WB authenticates the token remotely."""
    try:
        middle = token.split(".")[1]
        value = json.loads(base64.urlsafe_b64decode(middle + "=" * (-len(middle) % 4)))
        if not isinstance(value, dict):
            raise ValueError
        mask, expiry = value["s"], value["exp"]
        if type(mask) is not int or type(expiry) is not int or expiry <= time.time():
            raise ValueError
    except (IndexError, ValueError, KeyError, UnicodeError) as exc:
        raise InvalidInput("Нужен действующий API-токен WB") from exc
    if mask & 18 != 18:
        raise InvalidInput("Токену нужны категории Контент и Маркетплейс")
    if value.get("test") is True or value.get("t") is True or value.get("acc") == 2:
        raise InvalidInput("WB FBS требует производственный токен")
    return {
        "read_only": bool(mask & (1 << 30)),
        "expires_at": expiry,
        "account_hint": value.get("sid") if isinstance(value.get("sid"), str) else None,
    }


def collection(value, key=None):
    values = value.get(key) if isinstance(value, dict) and key else value
    if not isinstance(values, list) or any(not isinstance(v, dict) for v in values):
        raise RemoteError(None, "wb_response_invalid")
    return values


def integer(value):
    if type(value) is not int or value <= 0:
        raise RemoteError(None, "wb_identifier_invalid")
    return str(value)


def order_ids(values, maximum=1000):
    if not isinstance(values, list) or not 1 <= len(values) <= maximum:
        raise InvalidInput(f"Выберите от 1 до {maximum} заданий")
    result = []
    for value in values:
        if isinstance(value, str) and value.isascii() and value.isdigit():
            value = int(value)
        if type(value) is not int or value <= 0:
            raise InvalidInput("Неверный ID задания WB")
        result.append(value)
    if len(set(result)) != len(result):
        raise InvalidInput("Задания не должны повторяться")
    return result


def metadata(value):
    """New metaDetails is authoritative; retain compatibility with the old meta object."""
    details = value.get("metaDetails")
    if details is not None:
        if not isinstance(details, list) or any(
            not isinstance(v, dict) or not isinstance(v.get("key"), str) for v in details
        ):
            raise RemoteError(None, "wb_metadata_invalid")
        if len({v["key"] for v in details}) != len(details):
            raise RemoteError(None, "wb_metadata_invalid")
        result = {
            v["key"]: {"value": v.get("value"), "decision": v.get("decision")} for v in details
        }
        for key, item in result.items():
            if item["decision"] is not None and not isinstance(item["decision"], str):
                raise RemoteError(None, "wb_metadata_invalid")
            values = item["value"]
            if key == "sgtin" and values is not None:
                if (
                    not isinstance(values, list)
                    or any(not isinstance(v, str) or not v for v in values)
                    or len(set(values)) != len(values)
                ):
                    raise RemoteError(None, "wb_metadata_invalid")
        return result
    legacy = value.get("meta")
    if not isinstance(legacy, dict):
        raise RemoteError(None, "wb_metadata_invalid")
    return {
        key: {"value": item.get("value"), "decision": None}
        for key, item in legacy.items()
        if isinstance(item, dict)
    }


class WbAdapter:
    key = "wb"
    label = "Wildberries FBS"

    def __init__(self, vault, transport=None, *, clock=time.monotonic, pause=time.sleep):
        self.vault = vault
        self.http = transport or HttpTransport()
        self.clock, self.pause = clock, pause
        self._lock = Lock()
        self._identity_lock = Lock()
        self._next = {}
        self._accounts = {}

    @staticmethod
    def supported_operations(read_only=False):
        values = [("wb.sync", "Синхронизировать WB")]
        if not read_only:
            values.append(("wb.command", "Выполнить подготовленное действие WB"))
        values.append(("wb.reconcile", "Сверить результат WB"))
        return [OperationSpec(key=k, label=v).model_dump() for k, v in values]

    def validate_binding(self, seller, config):
        parsed = WbConfig.model_validate(config)
        token_claims(self.vault.get(seller, parsed.credential_ref).get("wb_token", ""))

    def token(self, config):
        return self.vault.get(credential_owner(config.credential_ref), config.credential_ref)[
            "wb_token"
        ]

    def _call(self, config, service, method, path, *, params=None, body=None):
        token = self.token(config)
        claims = token_claims(token)
        if method in {"PUT", "PATCH", "DELETE"} or (
            method == "POST" and path == "/api/v3/supplies"
        ):
            if config.read_only or claims["read_only"]:
                raise InvalidInput("Токен WB разрешает только чтение")
        # Identity: 1/min; Content: <=100/min; Marketplace basic tokens: <=150/min.
        key = (config.account_id or claims["account_hint"] or config.credential_ref, service)
        interval = {"common": 60, "content": 0.65, "marketplace": 0.5}[service]
        with self._lock:
            delay = self._next.get(key, 0) - self.clock()
            if delay > 0:
                self.pause(delay)
            self._next[key] = self.clock() + interval
        try:
            reply = self.http.request(
                method,
                HOSTS[service] + path,
                params=params,
                headers={"Authorization": token},
                body=body,
            )
        except RemoteError as exc:
            with self._lock:
                if exc.status == 409:
                    self._next[key] = max(self._next[key], self.clock() + 10 * interval)
                elif exc.status == 429:
                    self._next[key] = max(self._next[key], self.clock() + 10)
            raise RemoteError(exc.status, exc.code, redact(exc.details, [token])) from exc
        if reply.status not in {200, 201, 204}:
            raise RemoteError(reply.status, "wb_unexpected_status")
        return redact(reply.data, [token])

    def identity(self, config):
        token_claims(self.token(config))
        with self._identity_lock:
            cached = self._accounts.get(config.credential_ref)
            if cached and self.clock() - cached[1] < 60:
                return dict(cached[0])
            value = self._call(config, "common", "GET", "/api/v1/seller-info")
            if not isinstance(value, dict) or not all(
                isinstance(value.get(k), str) and value[k].strip() for k in ("sid", "tin")
            ):
                raise RemoteError(None, "wb_account_invalid")
            self._accounts[config.credential_ref] = (dict(value), self.clock())
            return value

    def account(self, config):
        value = self.identity(config)
        if config.account_id != value["sid"] or config.tin != value["tin"]:
            raise InvalidInput("Токен WB относится к другому продавцу или ИНН")
        return value

    def describe(self, config):
        parsed = WbConfig.model_validate(config)
        self.account(parsed)
        claims = token_claims(self.token(parsed))
        if claims["read_only"] != parsed.read_only:
            raise InvalidInput("Разрешения токена WB изменились")
        # Real category reads, not repeated /ping calls.
        self._call(parsed, "marketplace", "GET", "/api/v3/warehouses")
        self._call(
            parsed,
            "content",
            "POST",
            "/content/v2/get/cards/list",
            body={"settings": {"cursor": {"limit": 1}, "filter": {"withPhoto": -1}}},
        )
        return AccountInfo(
            external_account_id=parsed.account_id,
            operations=tuple(
                OperationSpec.model_validate(v) for v in self.supported_operations(parsed.read_only)
            ),
        )

    def config(self, context):
        self.validate_binding(context.seller_id, context.config)
        value = WbConfig.model_validate(context.config)
        if context.external_account_id != value.account_id:
            raise InvalidInput("Неверный аккаунт WB")
        return value

    @staticmethod
    def product(value):
        source = dict(value)
        external = integer(value.get("nmID"))
        skus = [s for v in value.get("sizes", []) for s in v.get("skus", [])]
        if any(not isinstance(s, str) for s in skus):
            raise RemoteError(None, "wb_product_invalid")
        return Product(
            external_id=external,
            title=value.get("title") or value.get("vendorCode") or external,
            sku=value.get("vendorCode"),
            identifiers={"barcode": skus},
            category={"id": value.get("subjectID"), "name": value.get("subjectName")},
            attributes={"source": source},
        )

    @staticmethod
    def catalog_variants(record):
        from fbe_flow.integrations.catalog import variants

        return variants(record, "wb")

    def catalog_schema(self, context, category):
        if not re.fullmatch(r"[0-9]{1,12}", category) or int(category) <= 0:
            raise InvalidInput("Укажите subjectID категории WB")
        config = self.config(context)
        self.account(config)
        attributes = self._call(config, "content", "GET", "/content/v2/object/charcs/" + category)
        tnved = self._call(
            config,
            "content",
            "GET",
            "/content/v2/directory/tnved",
            params={"subjectID": int(category)},
        )
        return {
            "category": category,
            "attributes": collection(attributes, "data"),
            "tnved": collection(tnved, "data"),
            "dimension_unit": "cm",
            "weight_unit": "kg",
            "source_url": "https://dev.wildberries.ru/openapi/work-with-products",
        }

    @staticmethod
    def supply(value):
        if (
            not isinstance(value.get("id"), str)
            or not value["id"]
            or type(value.get("done")) is not bool
        ):
            raise RemoteError(None, "wb_supply_invalid")
        status = "scanned" if value.get("scanDt") else ("closed" if value["done"] else "open")
        return Supply(external_id=value["id"], status=status, attributes={"source": value})

    @staticmethod
    def order(value, status):
        external = integer(value.get("id"))
        if not isinstance(status.get("supplierStatus"), str) or not status["supplierStatus"]:
            raise RemoteError(None, "wb_order_status_invalid")
        return Order(
            external_id=external,
            status=status["supplierStatus"],
            warehouse_external_id=str(value["warehouseId"]) if value.get("warehouseId") else None,
            supply_external_id=value.get("supplyId") or None,
            attributes={"source": value, "wb_status": status.get("wbStatus")},
        )

    def statuses(self, config, ids):
        result = {}
        for start in range(0, len(ids), 1000):
            values = collection(
                self._call(
                    config,
                    "marketplace",
                    "POST",
                    "/api/v3/orders/status",
                    body={"orders": ids[start : start + 1000]},
                ),
                "orders",
            )
            for item in values:
                key = integer(item.get("id"))
                if key in result:
                    raise RemoteError(None, "wb_order_status_invalid")
                result[key] = item
        if set(result) != {str(v) for v in ids}:
            raise RemoteError(None, "wb_order_status_missing")
        return result

    def metas(self, config, ids):
        result = {}
        for start in range(0, len(ids), 100):
            values = collection(
                self._call(
                    config,
                    "marketplace",
                    "POST",
                    "/api/marketplace/v3/orders/meta",
                    body={"orders": ids[start : start + 100]},
                ),
                "orders",
            )
            for item in values:
                key = integer(item.get("id"))
                if key in result:
                    raise RemoteError(None, "wb_metadata_invalid")
                result[key] = metadata(item)
        if set(result) != {str(v) for v in ids}:
            raise RemoteError(None, "wb_metadata_missing")
        return result

    def supply_info(self, config, supply_id):
        value = self._call(
            config, "marketplace", "GET", "/api/v3/supplies/" + quote(supply_id, safe="")
        )
        if not isinstance(value, dict) or value.get("id") != supply_id:
            raise RemoteError(None, "wb_supply_invalid")
        self.supply(value)
        return value

    def members(self, config, supply_id):
        value = self._call(
            config,
            "marketplace",
            "GET",
            "/api/marketplace/v3/supplies/" + quote(supply_id, safe="") + "/order-ids",
        )
        ids = value.get("orderIds") if isinstance(value, dict) else None
        if ids == []:
            return []
        return order_ids(ids, 5000)

    def sync_page(self, config, payload):
        phase = payload.get("phase")
        following = None
        batch = NormalizedBatch()
        if phase == "warehouses":
            self.account(config)
            items = collection(self._call(config, "marketplace", "GET", "/api/v3/warehouses"))
            batch = NormalizedBatch(
                warehouses=tuple(
                    Warehouse(
                        external_id=integer(v.get("id")),
                        name=v.get("name") or str(v["id"]),
                        attributes={"source": v},
                    )
                    for v in items
                )
            )
            following = {"phase": "products", "cursor": {"limit": 100}}
        elif phase == "products":
            cursor = payload["cursor"]
            value = self._call(
                config,
                "content",
                "POST",
                "/content/v2/get/cards/list",
                body={
                    "settings": {
                        "sort": {"ascending": True},
                        "cursor": cursor,
                        "filter": {"withPhoto": -1},
                    }
                },
            )
            items = collection(value, "cards")
            batch = NormalizedBatch(products=tuple(self.product(v) for v in items))
            returned = value.get("cursor")
            if not isinstance(returned, dict) or type(returned.get("total")) is not int:
                raise RemoteError(None, "wb_cursor_invalid")
            if returned["total"] >= 100:
                if not returned.get("updatedAt") or not returned.get("nmID"):
                    raise RemoteError(None, "wb_cursor_invalid")
                following = {
                    "phase": phase,
                    "cursor": {
                        "limit": 100,
                        "updatedAt": returned["updatedAt"],
                        "nmID": returned["nmID"],
                    },
                }
                if following["cursor"] == cursor:
                    raise RemoteError(None, "wb_cursor_stalled")
            else:
                following = {"phase": "supplies", "next": 0}
        elif phase in {"supplies", "orders"}:
            params = {"limit": 1000, "next": payload["next"]}
            if phase == "orders":
                params.update(dateFrom=payload["date_from"], dateTo=payload["date_to"])
            value = self._call(config, "marketplace", "GET", "/api/v3/" + phase, params=params)
            items = collection(value, phase)
            cursor = value.get("next")
            if type(cursor) is not int or cursor < 0:
                raise RemoteError(None, "wb_cursor_invalid")
            if phase == "supplies":
                batch = NormalizedBatch(supplies=tuple(self.supply(v) for v in items))
            else:
                statuses = (
                    self.statuses(config, [int(integer(v.get("id"))) for v in items])
                    if items
                    else {}
                )
                batch = NormalizedBatch(
                    orders=tuple(self.order(v, statuses[str(v["id"])]) for v in items)
                )
            if cursor:
                if cursor == payload["next"]:
                    raise RemoteError(None, "wb_cursor_stalled")
                following = {"phase": phase, "next": cursor}
            elif phase == "supplies":
                following = {"phase": "orders", "next": 0}
            else:
                following = None if payload.get("auto") else {"phase": "new"}
        elif phase == "new":
            items = collection(
                self._call(config, "marketplace", "GET", "/api/v3/orders/new"), "orders"
            )
            if payload.get("auto"):
                # Recheck cached active orders even if older than the history window.
                merged = {str(v["id"]): v for v in payload.get("cached_orders", [])}
                merged.update({str(v["id"]): v for v in items})
                items = list(merged.values())
                following = (
                    {"phase": "warehouses"}
                    if payload.get("references")
                    else {"phase": "supplies", "next": 0}
                )
            statuses = (
                self.statuses(config, [int(integer(v.get("id"))) for v in items]) if items else {}
            )
            batch = NormalizedBatch(
                orders=tuple(self.order(v, statuses[str(v["id"])]) for v in items)
            )
        else:
            raise InvalidInput("Неизвестная страница WB")
        return OperationResult(
            batch=batch,
            data={
                "sync": {
                    "run_id": payload["run_id"],
                    "auto": payload.get("auto", False),
                    "references": payload.get("references", True),
                    "phase": phase,
                    "following": following,
                    "count": len(items),
                    "page": payload["page"],
                    "date_from": payload["date_from"],
                    "date_to": payload["date_to"],
                }
            },
        )

    def execute(self, context, operation, payload):
        config = self.config(context)
        if operation == "wb.sync":
            return self.sync_page(config, payload)
        raise InvalidInput("Команды WB выполняются только через журнал подготовленных действий")

    def preflight(self, config, kind, body):
        self.account(config)
        if config.read_only or token_claims(self.token(config))["read_only"]:
            raise InvalidInput("Токен WB разрешает только чтение")
        if kind == "expiration":
            from datetime import date, datetime, timedelta

            order = int(body["order_external_id"])
            if datetime.strptime(body["expiration"], "%d.%m.%Y").date() < date.today() + timedelta(
                days=30
            ):
                raise InvalidInput("Остаточный срок годности меньше 30 дней")
            if self.statuses(config, [order])[str(order)]["supplierStatus"] != "confirm":
                raise Conflict("Срок годности можно передать только для задания на сборке")
            meta = self.metas(config, [order])[str(order)].get("expiration")
            if meta is None:
                raise InvalidInput("WB не разрешает срок годности для этого задания")
            if meta["value"] and meta["value"] != body["expiration"]:
                raise Conflict("В WB уже указан другой срок: проверьте партию товара")
            return
        if kind == "sgtin":
            order = int(body["order_external_id"])
            if self.statuses(config, [order])[str(order)]["supplierStatus"] != "confirm":
                raise Conflict("Для передачи кодов задание должно быть на сборке (confirm)")
            meta = self.metas(config, [order])[str(order)].get("sgtin")
            if meta is None:
                raise InvalidInput("WB не разрешает sgtin для этого задания")
            current = meta["value"]
            if current and current != body["sgtins"] and set(current) != set(body["sgtins"]):
                raise Conflict("В WB уже закреплены другие коды; перезапись запрещена")
        elif kind != "supply_create":
            supply = self.supply_info(config, body["supply_id"])
            members = self.members(config, body["supply_id"])
            if kind == "supply_deliver" and supply["done"]:
                return
            if supply["done"]:
                raise Conflict("Поставка WB уже закрыта")
            if kind == "supply_delete" and members:
                raise Conflict("Можно удалить только пустую поставку")
            if kind == "supply_add":
                statuses = self.statuses(config, body["orders"])
                if any(v["supplierStatus"] not in {"new", "confirm"} for v in statuses.values()):
                    raise Conflict("Добавлять можно новые задания и задания активных поставок")
                if set(members) != set(body["expected_members"]):
                    raise Conflict("Состав поставки изменился; подготовьте действие заново")
            if kind == "supply_deliver":
                if not members:
                    raise Conflict("В поставке нет заданий")
                statuses = self.statuses(config, members)
                if any(v["supplierStatus"] != "confirm" for v in statuses.values()):
                    raise Conflict("Не все задания поставки находятся на сборке")
                values = self.metas(config, members)
                if any(
                    v["decision"] != "filled" for meta in values.values() for v in meta.values()
                ):
                    raise Conflict(
                        "WB ещё не подтвердил все метаданные поставки; проверьте их статусы"
                    )
                return {"members": members, "metas": values}

    def send(self, config, kind, body):
        if kind == "expiration":
            self._call(
                config,
                "marketplace",
                "PUT",
                "/api/v3/orders/" + body["order_external_id"] + "/meta/expiration",
                body={"expiration": body["expiration"]},
            )
            return {}
        if kind == "sgtin":
            self._call(
                config,
                "marketplace",
                "PUT",
                "/api/v3/orders/" + body["order_external_id"] + "/meta/sgtin",
                body={"sgtins": body["sgtins"]},
            )
            return {}
        if kind == "supply_create":
            value = self._call(
                config,
                "marketplace",
                "POST",
                "/api/v3/supplies",
                body={"name": body["remote_name"]},
            )
            if (
                not isinstance(value, dict)
                or not isinstance(value.get("id"), str)
                or not value["id"]
            ):
                raise RemoteError(None, "wb_supply_invalid")
            return {"supply_id": value["id"]}
        if kind == "supply_add":
            self._call(
                config,
                "marketplace",
                "PATCH",
                "/api/marketplace/v3/supplies/" + quote(body["supply_id"], safe="") + "/orders",
                body={"orders": body["orders"]},
            )
        elif kind == "supply_deliver":
            self._call(
                config,
                "marketplace",
                "PATCH",
                "/api/v3/supplies/" + quote(body["supply_id"], safe="") + "/deliver",
            )
        elif kind == "supply_delete":
            self._call(
                config,
                "marketplace",
                "DELETE",
                "/api/v3/supplies/" + quote(body["supply_id"], safe=""),
            )
        else:
            raise InvalidInput("Неизвестная команда WB")
        return {}

    def reconcile(self, config, kind, body, receipt):
        self.account(config)
        if kind == "expiration":
            meta = self.metas(config, [int(body["order_external_id"])])[
                body["order_external_id"]
            ].get("expiration")
            if meta is None:
                return {"state": "conflict", "reason": "expiration_unavailable"}
            if meta["value"] and meta["value"] != body["expiration"]:
                return {"state": "conflict", "reason": "different_expiration"}
            if meta["value"] == body["expiration"] and meta["decision"] == "filled":
                return {"state": "confirmed", "metadata": meta}
            return {
                "state": "pending" if receipt.get("acknowledged") else "unknown",
                "metadata": meta,
            }
        if kind == "sgtin":
            meta = self.metas(config, [int(body["order_external_id"])])[
                body["order_external_id"]
            ].get("sgtin")
            if meta is None:
                return {"state": "conflict", "reason": "sgtin_unavailable"}
            current = meta["value"] or []
            if not isinstance(current, list) or any(not isinstance(v, str) for v in current):
                raise RemoteError(None, "wb_metadata_invalid")
            decision = meta["decision"]
            data = {"metadata": meta, "decision": decision, "order_id": body["order_external_id"]}
            if current and set(current) != set(body["sgtins"]):
                return {**data, "state": "conflict", "reason": "different_codes"}
            if not current:
                return {**data, "state": "pending" if receipt.get("acknowledged") else "unknown"}
            if decision == "filled":
                return {**data, "state": "confirmed"}
            if decision not in {None, "pending", "required"}:
                return {**data, "state": "rejected", "reason": "metadata_rejected"}
            return {**data, "state": "pending"}
        supply_id = receipt.get("supply_id") or body.get("supply_id")
        if kind == "supply_create" and not supply_id:
            cursor, seen, matches = 0, set(), []
            for _ in range(100):
                values = self._call(
                    config,
                    "marketplace",
                    "GET",
                    "/api/v3/supplies",
                    params={"limit": 1000, "next": cursor},
                )
                matches.extend(
                    v["id"]
                    for v in collection(values, "supplies")
                    if v.get("name") == body["remote_name"]
                )
                cursor = values.get("next")
                if cursor == 0:
                    break
                if type(cursor) is not int or cursor in seen:
                    raise RemoteError(None, "wb_cursor_invalid")
                seen.add(cursor)
            else:
                raise RemoteError(None, "wb_reconcile_limit")
            if len(matches) != 1:
                return {
                    "state": "unknown" if not matches else "conflict",
                    "reason": "supply_receipt_missing",
                }
            supply_id = matches[0]
        if kind == "supply_delete":
            try:
                self.supply_info(config, supply_id)
            except RemoteError as exc:
                if exc.status == 404:
                    return {"state": "confirmed", "supply_id": supply_id, "deleted": True}
                raise
            return {"state": "pending" if receipt.get("acknowledged") else "unknown"}
        value = self.supply_info(config, supply_id)
        data = {"supply_id": supply_id, "supply": value}
        if kind == "supply_create":
            if value.get("name") != body["remote_name"]:
                return {**data, "state": "conflict"}
            return {**data, "state": "confirmed"}
        if kind == "supply_deliver":
            return {
                **data,
                "state": "confirmed"
                if value["done"]
                else ("pending" if receipt.get("acknowledged") else "unknown"),
            }
        members = self.members(config, supply_id)
        present = sorted(set(body["orders"]) & set(members))
        missing = sorted(set(body["orders"]) - set(members))
        return {
            **data,
            "members": members,
            "present": present,
            "missing": missing,
            "state": "confirmed"
            if not missing
            else (
                "partial" if present else ("pending" if receipt.get("acknowledged") else "unknown")
            ),
        }
