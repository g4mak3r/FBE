"""Seller-scoped Ozon/store workflows, durable receipts and shared KM reservations."""

import json
import re
import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from uuid import uuid4

from fbe_flow.core.errors import Conflict, FlowError, InvalidInput, NotFound
from fbe_flow.core.models import NormalizedBatch, OperationResult
from fbe_flow.integrations.chz.formats import digest, gtin_text
from fbe_flow.integrations.chz.http import RemoteError
from fbe_flow.modules.catalog import check_source_binding
from fbe_flow.modules.connections import connection_context, require_connection
from fbe_flow.modules.fulfillment import decode_action
from fbe_flow.modules.marking import canonical
from fbe_flow.modules.records import Records, decode_record

KINDS = {"products", "warehouses", "orders", "supplies", "returns"}
ACTION_KINDS = {
    "ozon": {"codes", "ship", "cancel"},
    "kit": {"codes", "confirm", "cancel", "complete", "prices", "stocks"},
}


def date_text(value):
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


class Commerce:
    def __init__(self, database, connections, operations, registry, marking, fulfillment):
        self.db, self.connections, self.operations = database, connections, operations
        self.registry, self.marking, self.fulfillment = registry, marking, fulfillment

    def _connection(self, seller, connection):
        value = self.connections.get(seller, connection)
        if value["adapter_key"] not in ACTION_KINDS:
            raise InvalidInput("Выберите подключение Ozon или магазина")
        return value

    def adapter(self, connection):
        adapter = self.registry.get(connection["adapter_key"])
        return adapter, adapter.config(connection_context(connection))

    def refresh_capabilities(self):
        for descriptor in self.registry.descriptors():
            if descriptor["key"] not in ACTION_KINDS:
                continue
            adapter = self.registry.get(descriptor["key"])
            with self.db.connection() as conn:
                for row in conn.execute(
                    "SELECT * FROM connections WHERE adapter_key=?", (descriptor["key"],)
                ).fetchall():
                    config = json.loads(row["config_json"])
                    conn.execute(
                        "UPDATE connections SET operations_json=? WHERE id=?",
                        (canonical(adapter.supported_operations(config["read_only"])), row["id"]),
                    )

    @staticmethod
    def snapshot(conn, seller, connection, key, value):
        conn.execute(
            "INSERT INTO commerce_snapshots(seller_id,connection_id,key,value_json) "
            "VALUES(?,?,?,?) "
            "ON CONFLICT(seller_id,connection_id,key) DO UPDATE SET value_json=excluded.value_json,"
            "updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')",
            (seller, connection, key, canonical(value)),
        )

    def overview(self, seller, connection):
        value = self._connection(seller, connection)
        adapter, config = self.adapter(value)
        with self.db.connection() as conn:
            snapshots = {
                r["key"]: {"value": json.loads(r["value_json"]), "updated_at": r["updated_at"]}
                for r in conn.execute(
                    "SELECT * FROM commerce_snapshots WHERE seller_id=? AND connection_id=?",
                    (seller, connection),
                )
            }
            counts = {
                k: conn.execute(
                    f"SELECT count(*) FROM {k} WHERE seller_id=? AND connection_id=?",
                    (seller, connection),
                ).fetchone()[0]
                for k in KINDS - {"returns"}
            }
            counts["returns"] = conn.execute(
                "SELECT count(*) FROM commerce_objects WHERE seller_id=? AND connection_id=? "
                "AND kind='returns'",
                (seller, connection),
            ).fetchone()[0]
        return {
            "parameters": {
                "account_id": config.account_id,
                "tin": config.tin,
                "read_only": config.read_only,
                "adapter_key": adapter.key,
                "tin_verified": adapter.key == "ozon",
            },
            "snapshots": snapshots,
            "counts": counts,
        }

    def records(self, seller, connection, kind, offset=0, limit=100, search="", status=""):
        value = self._connection(seller, connection)
        if kind not in KINDS or not 0 <= offset or not 1 <= limit <= 200:
            raise InvalidInput("Неверные параметры страницы")
        args, predicate = [seller, connection], "seller_id=? AND connection_id=?"
        if kind == "returns":
            table, field, predicate = (
                "commerce_objects",
                "value_json",
                predicate + " AND kind='returns'",
            )
        else:
            table, field = kind, "attributes_json"
        if search:
            pattern = (
                "%" + search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            )
            predicate += f" AND (external_id LIKE ? ESCAPE '\\' OR {field} LIKE ? ESCAPE '\\')"
            args.extend((pattern, pattern))
        if status and kind == "orders":
            predicate += " AND status=?"
            args.append(status)
        with self.db.connection() as conn:
            total = conn.execute(
                f"SELECT count(*) FROM {table} WHERE {predicate}", args
            ).fetchone()[0]
            rows = conn.execute(
                f"SELECT * FROM {table} WHERE {predicate} ORDER BY updated_at DESC,external_id "
                "LIMIT ? OFFSET ?",
                (*args, limit, offset),
            ).fetchall()
            items = (
                [json.loads(r["value_json"]) for r in rows]
                if kind == "returns"
                else [decode_record(r) for r in rows]
            )
            if kind == "orders":
                for item in items:
                    if value["adapter_key"] == "kit":
                        item["attributes"]["items"] = self.display_items(
                            conn, seller, connection, item["attributes"]["items"]
                        )
                    item["marking"] = [
                        dict(r)
                        for r in conn.execute(
                            "SELECT "
                            "r.code,r.action_id,a.state,json_extract(a.body_json,'$.item_id') "
                            "AS item_id FROM code_reservations r JOIN commerce_actions a ON "
                            "a.seller_id=r.seller_id AND a.id=r.action_id WHERE r.seller_id=? "
                            "AND r.order_id=? AND r.origin='commerce'",
                            (seller, item["id"]),
                        )
                    ]
        return {"items": items, "total": total, "offset": offset, "limit": limit}

    @staticmethod
    def display_items(conn, seller, connection, items):
        result = []
        for item in items:
            product = conn.execute(
                "SELECT title FROM products WHERE seller_id=? "
                "AND connection_id=? AND external_id=?",
                (seller, connection, item["variant"]),
            ).fetchone()
            result.append({**item, "title": product[0] if product else item["title"]})
        return result

    def start_sync(self, seller, connection):
        value = self._connection(seller, connection)
        with self.db.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            return self._start_sync(conn, seller, value)

    def _start_sync(self, conn, seller, connection):
        if conn.execute(
            "SELECT 1 FROM operations WHERE seller_id=? AND connection_id=? AND "
            "operation_key='commerce.sync' AND status IN ('queued','running')",
            (seller, connection["id"]),
        ).fetchone():
            raise Conflict("Синхронизация уже выполняется")
        run, now = str(uuid4()), datetime.now(UTC)
        payload = {
            "phase": "warehouses",
            "run_id": run,
            "page": 0,
            "date_from": date_text(now - timedelta(days=30)),
            "date_to": date_text(now),
        }
        job = self.marking._queue(
            conn, seller, connection["id"], "commerce.sync", payload, run + ":0"
        )
        self.snapshot(
            conn,
            seller,
            connection["id"],
            "sync",
            {"run_id": run, "page": 0, "state": "queued", "counts": {}, "coverage": "partial"},
        )
        return {"id": job, "run_id": run}

    def links(self, seller, connection):
        self._connection(seller, connection)
        with self.db.connection() as conn:
            return [
                dict(r)
                for r in conn.execute(
                    "SELECT l.*,p.title AS product_title,n.title AS chz_title FROM "
                    "commerce_links l "
                    "JOIN products p ON p.seller_id=l.seller_id AND p.id=l.product_id JOIN "
                    "products n "
                    "ON n.seller_id=l.seller_id AND n.id=l.chz_product_id WHERE l.seller_id=? AND "
                    "l.connection_id=? ORDER BY p.title,l.id",
                    (seller, connection),
                )
            ]

    def link(self, seller, connection, body):
        value = self._connection(seller, connection)
        chz = self.marking._connection(seller, body["chz_connection_id"])
        if (
            chz["config"].get("environment") != "production"
            or chz["config"]["inn"] != value["config"]["tin"]
        ):
            raise InvalidInput("Нужен производственный аккаунт ЧЗ с тем же ИНН")
        product = Records(self.db).get(seller, "products", body["product_id"])
        nk = Records(self.db).get(seller, "products", body["chz_product_id"])
        if product["connection_id"] != connection or nk["connection_id"] != chz["id"]:
            raise InvalidInput("Товары принадлежат другому подключению")
        variant, gtin = product["attributes"]["variant"], gtin_text(body["gtin"])
        if gtin not in nk["identifiers"].get("gtin", []):
            raise InvalidInput("GTIN отсутствует в выбранной карточке НК")
        group = body["product_group"]
        adapter = self.registry.get("chz")
        adapter.require_group(adapter.config(connection_context(chz)), group)
        link_id = str(uuid4())
        try:
            with self.db.connection() as conn:
                conn.execute("BEGIN IMMEDIATE")
                check_source_binding(conn, seller, product["id"], variant, gtin, group)
                card = conn.execute(
                    "SELECT present,detail_available FROM nk_cards WHERE seller_id=? AND "
                    "connection_id=? "
                    "AND external_id=?",
                    (seller, chz["id"], nk["external_id"]),
                ).fetchone()
                if not card or not card["present"] or not card["detail_available"]:
                    raise Conflict("Обновите собственную карточку НК")
                conn.execute(
                    "INSERT INTO commerce_links(id,seller_id,connection_id,product_id,variant,"
                    "chz_connection_id,chz_product_id,gtin,product_group) "
                    "VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        link_id,
                        seller,
                        connection,
                        product["id"],
                        variant,
                        chz["id"],
                        nk["id"],
                        gtin,
                        group,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("Этот товар уже связан") from exc
        return next(v for v in self.links(seller, connection) if v["id"] == link_id)

    def unlink(self, seller, connection, link_id):
        self._connection(seller, connection)
        with self.db.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute(
                "SELECT 1 FROM commerce_actions WHERE seller_id=? AND connection_id=? AND "
                "json_extract(body_json,'$.link_id')=? AND state<>'cancelled'",
                (seller, connection, link_id),
            ).fetchone():
                raise Conflict("Связь используется назначением кодов")
            if not conn.execute(
                "DELETE FROM commerce_links WHERE seller_id=? AND connection_id=? AND id=?",
                (seller, connection, link_id),
            ).rowcount:
                raise NotFound("Связь не найдена")
        return {"deleted": True}

    def order_item(self, seller, connection, order_id, item_id):
        value = self._connection(seller, connection)
        order = Records(self.db).get(seller, "orders", order_id)
        if order["connection_id"] != connection:
            raise NotFound("Заказ не найден в этом подключении")
        adapter, _ = self.adapter(value)
        item = next(
            (v for v in adapter.items(order["attributes"]["source"]) if v["item_id"] == item_id),
            None,
        )
        if not item:
            raise NotFound("Позиция заказа не найдена")
        link = next(
            (v for v in self.links(seller, connection) if v["variant"] == item["variant"]), None
        )
        if not link:
            raise InvalidInput("Сначала свяжите товар с GTIN Честного Знака")
        with self.db.connection() as conn:
            check_source_binding(
                conn,
                seller,
                link["product_id"],
                link["variant"],
                link["gtin"],
                link["product_group"],
            )
        return order, item, link

    def available_codes(self, seller, connection, order_id, item_id, offset=0, limit=100):
        _, item, link = self.order_item(seller, connection, order_id, item_id)
        predicate = (
            "seller_id=? AND connection_id=? AND gtin=? AND product_group=? AND "
            "full_code IS NOT NULL AND NOT EXISTS(SELECT 1 FROM code_reservations r "
            "WHERE r.code=marking_codes.code)"
        )
        args = (seller, link["chz_connection_id"], link["gtin"], link["product_group"])
        with self.db.connection() as conn:
            total = conn.execute(
                "SELECT count(*) FROM marking_codes WHERE " + predicate, args
            ).fetchone()[0]
            values = [
                dict(r)
                for r in conn.execute(
                    "SELECT id,code,gtin,product_group,external_status FROM marking_codes WHERE "
                    + predicate
                    + " ORDER BY updated_at,id LIMIT ? OFFSET ?",
                    (*args, limit, offset),
                )
            ]
        return {
            "items": values,
            "total": total,
            "offset": offset,
            "limit": limit,
            "required": item["quantity"],
        }

    def action(self, seller, action_id):
        with self.db.connection() as conn:
            row = conn.execute(
                "SELECT * FROM commerce_actions WHERE seller_id=? AND id=?", (seller, action_id)
            ).fetchone()
            if not row:
                raise NotFound("Действие не найдено")
            value = decode_action(row)
            value["events"] = [
                {
                    "state": r["state"],
                    "created_at": r["created_at"],
                    "data": json.loads(r["data_json"]),
                }
                for r in conn.execute(
                    "SELECT * FROM commerce_events WHERE seller_id=? AND action_id=? ORDER BY id",
                    (seller, action_id),
                )
            ]
        return value

    def actions(self, seller, connection):
        self._connection(seller, connection)
        with self.db.connection() as conn:
            return [
                decode_action(r)
                for r in conn.execute(
                    "SELECT * FROM commerce_actions WHERE seller_id=? AND connection_id=? ORDER BY "
                    "created_at DESC,id LIMIT 100",
                    (seller, connection),
                )
            ]

    @staticmethod
    def event(conn, seller, action_id, state, data):
        conn.execute(
            "INSERT INTO commerce_events(seller_id,action_id,state,data_json) VALUES(?,?,?,?)",
            (seller, action_id, state, canonical(data)),
        )

    def change(self, conn, seller, action_id, state, result=None, *, error=None, acknowledged=None):
        result = dict(result or {})
        prior = conn.execute(
            "SELECT result_json FROM commerce_actions WHERE seller_id=? AND id=?",
            (seller, action_id),
        ).fetchone()
        saved = json.loads(prior[0]) if prior else {}
        if "receipt" in saved and "receipt" not in result:
            result["receipt"] = saved["receipt"]
        due = (
            date_text(datetime.now(UTC) + timedelta(seconds=30))
            if state in {"accepted", "pending"}
            else None
        )
        conn.execute(
            "UPDATE commerce_actions SET state=?,result_json=?,error_code=?,next_check_at=?,"
            "updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE seller_id=? AND id=?",
            (state, canonical(result or {}), error, due, seller, action_id),
        )
        if acknowledged is not None:
            conn.execute(
                "UPDATE commerce_actions SET acknowledged=? WHERE seller_id=? AND id=?",
                (int(acknowledged), seller, action_id),
            )
        self.event(conn, seller, action_id, state, {"error_code": error, **(result or {})})
        if state in {"confirmed", "cancelled"}:
            conn.execute(
                "DELETE FROM commerce_targets WHERE seller_id=? AND action_id=?",
                (seller, action_id),
            )

    def prepare(self, seller, connection, kind, payload):
        value = self._connection(seller, connection)
        adapter, config = self.adapter(value)
        if kind not in ACTION_KINDS[adapter.key] or config.read_only:
            raise InvalidInput("Действие недоступно для этого подключения")
        for key in ("order_id", "item_id"):
            if key in payload and (
                not isinstance(payload[key], str) or not payload[key] or len(payload[key]) > 120
            ):
                raise InvalidInput("Неверный идентификатор позиции или заказа")
        action_id, codes, targets = str(uuid4()), [], []
        if kind == "codes":
            if set(payload) != {"order_id", "item_id", "code_ids"}:
                raise InvalidInput("Выберите позицию заказа и коды")
            order, item, link = self.order_item(
                seller, connection, payload["order_id"], payload["item_id"]
            )
            ids = payload["code_ids"]
            if (
                not isinstance(ids, list)
                or not ids
                or len(ids) > 500
                or any(not isinstance(v, str) for v in ids)
            ):
                raise InvalidInput("Выберите от 1 до 500 кодов")
            codes = self.marking.validate_code_selection(
                seller, link["chz_connection_id"], ids, full=True
            )
            if len(codes) != item["quantity"] or any(
                v["gtin"] != link["gtin"] or v["product_group"] != link["product_group"]
                for v in codes
            ):
                raise InvalidInput("По одному коду на единицу, соответствующему GTIN и группе")
            if order["status"] in {
                "CANCELLED",
                "COMPLETED",
                "DELIVERED",
                "FULL_REFUND",
                "PARTIAL_REFUND",
                "CANCELLATION_IN_PROGRESS",
                "CREATING_FINAL_RECEIPTS",
                "cancelled",
                "delivered",
            }:
                raise Conflict("Для завершённого или отменённого заказа новые КМ недоступны")
            body = {
                "order_id": order["id"],
                "order_external_id": order["external_id"],
                "order_label": str(
                    order["attributes"]["source"].get("order_number", order["external_id"])
                ),
                "item_id": item["item_id"],
                "variant": item["variant"],
                "link_id": link["id"],
                "chz_connection_id": link["chz_connection_id"],
                "gtin": link["gtin"],
                "product_group": link["product_group"],
                "code_ids": [v["id"] for v in codes],
                "sgtins": [v["full_code"] for v in codes],
                "cis": [v["code"] for v in codes],
            }
            if adapter.key == "ozon":
                body.update(adapter.prepare_codes(config, order, item, codes))
                targets = ["order:" + order["external_id"]]
            else:
                body["items"] = adapter.items(order["attributes"]["source"])
                adapter.preflight(config, kind, body)
                targets = ["item:" + order["external_id"] + ":" + item["item_id"]]
        elif kind in {"prices", "stocks"}:
            body = self.prepare_bulk(seller, value, kind, payload)
            targets = [
                kind + ":" + v["variant_id"] + ":" + str(v.get("warehouse_id", ""))
                for v in body["wire"]["items"]
            ]
        else:
            allowed = (
                {"order_id", "cancel_reason_id", "cancel_reason_message"}
                if kind == "cancel" and adapter.key == "ozon"
                else {"order_id"}
            )
            if not set(payload) <= allowed or "order_id" not in payload:
                raise InvalidInput("Проверьте поля действия")
            order = Records(self.db).get(seller, "orders", payload["order_id"])
            if order["connection_id"] != connection:
                raise NotFound("Заказ не найден в этом подключении")
            source = adapter.get_order(config, order["external_id"])
            body = {
                "order_id": order["id"],
                "order_external_id": order["external_id"],
                "order_label": str(source.get("order_number", order["external_id"])),
                "items": adapter.items(source),
            }
            if kind in {"ship", "confirm", "complete"}:
                body["marking_actions"] = self.required_marking(seller, value, order, source)
            if adapter.key == "ozon":
                if kind == "ship":
                    body["wire"] = {
                        "posting_number": order["external_id"],
                        "packages": [
                            {
                                "products": [
                                    {"product_id": int(v["variant"]), "quantity": v["quantity"]}
                                    for v in body["items"]
                                ]
                            }
                        ],
                    }
                else:
                    reason, message = (
                        payload.get("cancel_reason_id"),
                        payload.get("cancel_reason_message", ""),
                    )
                    if (
                        type(reason) is not int
                        or reason <= 0
                        or not isinstance(message, str)
                        or len(message) > 500
                    ):
                        raise InvalidInput("Выберите причину отмены Ozon")
                    body.update(cancel_reason_id=reason)
                    body["wire"] = {
                        "posting_number": order["external_id"],
                        "cancel_reason_id": reason,
                        "cancel_reason_message": message,
                    }
            adapter.preflight(config, kind, body)
            targets = ["order:" + order["external_id"]]
        try:
            with self.db.connection() as conn:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    "INSERT INTO "
                    "commerce_actions(id,seller_id,connection_id,kind,body_json,body_digest)"
                    " VALUES(?,?,?,?,?,?)",
                    (action_id, seller, connection, kind, canonical(body), digest(body)),
                )
                for target in targets:
                    conn.execute(
                        "INSERT INTO commerce_targets(seller_id,connection_id,target_key,action_id)"
                        " VALUES(?,?,?,?)",
                        (seller, connection, target, action_id),
                    )
                for code in codes:
                    conn.execute(
                        "INSERT INTO "
                        "code_reservations(code,seller_id,code_id,connection_id,order_id,"
                        "origin,action_id) VALUES(?,?,?,?,?,'commerce',?)",
                        (code["code"], seller, code["id"], connection, body["order_id"], action_id),
                    )
                self.event(conn, seller, action_id, "draft", {"kind": kind})
        except sqlite3.IntegrityError as exc:
            raise Conflict("Заказ, товар или код уже закреплены за другим действием") from exc
        return self.action(seller, action_id)

    def prepare_bulk(self, seller, connection, kind, payload):
        if (
            set(payload) != {"items"}
            or not isinstance(payload["items"], list)
            or not 1 <= len(payload["items"]) <= 100
        ):
            raise InvalidInput("Передайте от 1 до 100 элементов")
        adapter, config = self.adapter(connection)
        wire, before, pairs = [], [], set()
        for entry in payload["items"]:
            allowed = (
                {"product_id", "price", "manual_discount_price"}
                if kind == "prices"
                else {"product_id", "warehouse_id", "quantity"}
            )
            if (
                not isinstance(entry, dict)
                or not set(entry) <= allowed
                or "product_id" not in entry
                or not isinstance(entry["product_id"], str)
                or not entry["product_id"]
            ):
                raise InvalidInput("Проверьте поля элемента")
            product = Records(self.db).get(seller, "products", entry["product_id"])
            if product["connection_id"] != connection["id"]:
                raise NotFound("Товар не найден в магазине")
            item = {"variant_id": product["external_id"]}
            if kind == "stocks":
                if not isinstance(entry.get("warehouse_id"), str) or not entry["warehouse_id"]:
                    raise InvalidInput("Выберите склад магазина")
                warehouse = Records(self.db).get(
                    seller, "warehouses", entry.get("warehouse_id", "")
                )
                quantity = entry.get("quantity")
                if (
                    warehouse["connection_id"] != connection["id"]
                    or type(quantity) is not int
                    or quantity < 0
                ):
                    raise InvalidInput("Проверьте склад и абсолютный остаток")
                item.update(warehouse_id=warehouse["external_id"], quantity=quantity)
            else:
                for field in ("price", "manual_discount_price"):
                    if field not in entry:
                        continue
                    price = entry[field]
                    if price is not None:
                        if not isinstance(price, str) or not re.fullmatch(
                            r"[0-9]{1,15}(?:\.[0-9]{1,2})?", price
                        ):
                            raise InvalidInput("Цена: до 15 цифр и двух знаков после точки")
                        try:
                            number = Decimal(str(price))
                        except (InvalidOperation, ValueError) as exc:
                            raise InvalidInput("Неверная цена") from exc
                        if (
                            not isinstance(price, str)
                            or not number.is_finite()
                            or number <= 0
                            or number.as_tuple().exponent < -2
                        ):
                            raise InvalidInput("Цена: положительная строка с точностью до копеек")
                        price = format(number, "f")
                    item[field] = price
                if len(item) == 1:
                    raise InvalidInput("Укажите изменяемую цену")
            pair = (item["variant_id"], item.get("warehouse_id"))
            if pair in pairs:
                raise InvalidInput("Элементы не должны повторяться")
            pairs.add(pair)
            source = adapter.get_variant(config, product["external_id"])
            wire.append(item)
            before.append(adapter.field_value(source, kind, item))
        body = {"wire": {"items": wire}, "before": before}
        adapter.preflight(config, kind, body)
        return body

    def required_marking(self, seller, connection, order, source):
        adapter, config = self.adapter(connection)
        links = {v["variant"]: v for v in self.links(seller, connection["id"])}
        required = set(
            str(v)
            for v in source.get("requirements", {}).get("products_requiring_mandatory_mark", [])
        )
        action_ids = []
        for item in adapter.items(source):
            linked = item["variant"] in links
            if adapter.key == "kit":
                if item["deleted"]:
                    raise Conflict("Удалённую позицию заказа обработайте в кабинете KIT")
                product = adapter.get_variant(config, item["variant"])
                if type(product.get("requires_marking")) is not bool:
                    raise Conflict("KIT не подтвердил требование маркировки товара")
                required_item = product["requires_marking"]
            else:
                required_item = item["variant"] in required
            if not linked and not required_item:
                continue
            if not linked:
                raise Conflict("Маркируемый товар должен быть связан с ЧЗ")
            with self.db.connection() as conn:
                rows = conn.execute(
                    "SELECT * FROM commerce_actions WHERE seller_id=? AND connection_id=? AND "
                    "kind='codes' "
                    "AND state='confirmed' AND json_extract(body_json,'$.order_id')=? "
                    "AND json_extract(body_json,'$.item_id')=?",
                    (seller, connection["id"], order["id"], item["item_id"]),
                ).fetchall()
            if (
                len(rows) != 1
                or len(json.loads(rows[0]["body_json"])["code_ids"]) != item["quantity"]
            ):
                raise Conflict("Коды каждой маркируемой позиции должны быть подтверждены системой")
            action_ids.append(rows[0]["id"])
        return action_ids

    def check_codes(self, seller, connection, body):
        chz = self.marking._connection(seller, body["chz_connection_id"])
        if chz["config"]["inn"] != connection["config"]["tin"]:
            raise Conflict("ИНН подключения и ЧЗ отличаются")
        link = next(
            (v for v in self.links(seller, connection["id"]) if v["id"] == body["link_id"]), None
        )
        if not link or any(
            link[k] != body[k] for k in ("variant", "chz_connection_id", "gtin", "product_group")
        ):
            raise Conflict("Связь товара с ЧЗ изменилась")
        with self.db.connection() as conn:
            check_source_binding(
                conn,
                seller,
                link["product_id"],
                link["variant"],
                link["gtin"],
                link["product_group"],
            )
        values = self.fulfillment._check_chz(seller, body)
        with self.db.connection() as conn:
            self.marking._apply_codes(
                conn, {"seller_id": seller, "connection_id": chz["id"]}, values
            )
        return values

    def enqueue(self, seller, action_id, reconcile=False):
        action = self.action(seller, action_id)
        value = self._connection(seller, action["connection_id"])
        if not reconcile and value["config"]["read_only"]:
            raise InvalidInput("Подключение разрешает только чтение")
        with self.db.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            current = conn.execute(
                "SELECT state FROM commerce_actions WHERE seller_id=? AND id=?", (seller, action_id)
            ).fetchone()[0]
            if current != action["state"]:
                raise Conflict("Действие изменилось")
            if (not reconcile and current != "draft") or (
                reconcile and current in {"draft", "queued", "submitting", "cancelled", "confirmed"}
            ):
                raise Conflict("Это действие сейчас нельзя отправить или сверить")
            if conn.execute(
                "SELECT 1 FROM operations WHERE seller_id=? AND scope_key=? "
                "AND status IN ('queued','running')",
                (seller, action_id),
            ).fetchone():
                raise Conflict("Действие уже выполняется")
            if not reconcile:
                self.change(conn, seller, action_id, "queued")
            job = self.marking._queue(
                conn,
                seller,
                value["id"],
                "commerce.reconcile" if reconcile else "commerce.command",
                {"action_id": action_id},
                action_id,
            )
        return self.operations.get(seller, job)

    def cancel(self, seller, action_id):
        action = self.action(seller, action_id)
        safe = action["state"] == "draft" or (
            action["state"] == "rejected"
            and not action["acknowledged"]
            and (action["result"].get("before_send") or action["result"].get("definite_rejection"))
        )
        if not safe:
            raise Conflict("Нельзя освободить коды после передачи или неопределённого результата")
        with self.db.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            current = conn.execute(
                "SELECT state FROM commerce_actions WHERE seller_id=? AND id=?", (seller, action_id)
            ).fetchone()[0]
            if current != action["state"]:
                raise Conflict("Действие уже изменилось")
            self.change(conn, seller, action_id, "cancelled")
            conn.execute(
                "DELETE FROM code_reservations WHERE seller_id=? AND action_id=? AND "
                "origin='commerce'",
                (seller, action_id),
            )
        return self.action(seller, action_id)

    def order_details(self, seller, connection, order_id):
        value = self._connection(seller, connection)
        order = Records(self.db).get(seller, "orders", order_id)
        if order["connection_id"] != connection:
            raise NotFound("Заказ не найден в этом подключении")
        adapter, config = self.adapter(value)
        source = adapter.get_order(config, order["external_id"])
        Records(self.db).apply(seller, connection, NormalizedBatch(orders=(adapter.order(source),)))
        items = adapter.items(source)
        if adapter.key == "kit":
            with self.db.connection() as conn:
                items = self.display_items(conn, seller, connection, items)
        return {"source": source, "items": items}

    def execute(self, adapter, context, key, payload):
        seller, connection = context.seller_id, context.connection_id
        scope = (
            str(payload.get("run_id")) + ":" + str(payload.get("page"))
            if key == "commerce.sync"
            else payload.get("action_id")
        )
        with self.db.connection() as conn:
            running = conn.execute(
                "SELECT 1 FROM operations WHERE seller_id=? AND connection_id=? AND "
                "operation_key=? "
                "AND scope_key=? AND status='running' AND payload_json=?",
                (seller, connection, key, scope, canonical(payload)),
            ).fetchone()
        if not running:
            raise Conflict("Требуется действие из журнала FBE")
        value = self._connection(seller, connection)
        config = adapter.config(context)
        if key == "commerce.sync":
            with self.db.connection() as conn:
                row = conn.execute(
                    "SELECT value_json FROM commerce_snapshots WHERE seller_id=? "
                    "AND connection_id=? AND key='sync'",
                    (seller, connection),
                ).fetchone()
            if not row or json.loads(row[0]).get("run_id") != payload["run_id"]:
                raise Conflict("Синхронизация была заменена")
            result = adapter.sync(config, payload)
            if adapter.key == "kit":
                self.check_pagination_page(seller, connection, payload, result)
            return result.model_copy(
                update={"data": {**result.data, "sync": {**payload, **result.data["sync"]}}}
            )
        if set(payload) != {"action_id"} or key not in {"commerce.command", "commerce.reconcile"}:
            raise InvalidInput("Неверная операция")
        action = self.action(seller, payload["action_id"])
        if action["connection_id"] != connection or digest(action["body"]) != action["body_digest"]:
            raise Conflict("Действие принадлежит другому подключению или изменилось")
        body, kind = action["body"], action["kind"]
        if key == "commerce.command":
            if action["state"] != "queued" or config.read_only:
                raise Conflict("Действие не готово к отправке")
            try:
                adapter.account(config)
                if kind == "codes":
                    self.check_codes(seller, value, body)
                    if adapter.key == "kit":
                        adapter.preflight(config, kind, body)
                        outcome = adapter.reconcile(config, kind, body, False)
                        with self.db.connection() as conn:
                            self.change(conn, seller, action["id"], outcome["state"], outcome)
                        return OperationResult(
                            batch=self.outcome_batch(adapter, outcome),
                            data={"action_id": action["id"], **outcome},
                        )
                for marking_id in body.get("marking_actions", []):
                    marking = self.action(seller, marking_id)
                    if marking["state"] != "confirmed" or marking["connection_id"] != connection:
                        raise Conflict("Подтверждение КМ изменилось")
                    self.check_codes(seller, value, marking["body"])
                    if (
                        adapter.reconcile(config, "codes", marking["body"], True)["state"]
                        != "confirmed"
                    ):
                        raise Conflict("Коды заказа больше не подтверждены внешней системой")
                adapter.preflight(config, kind, body)
                existing = adapter.reconcile(config, kind, body, False)
                if existing["state"] == "confirmed":
                    with self.db.connection() as conn:
                        self.change(
                            conn,
                            seller,
                            action["id"],
                            "confirmed",
                            {**existing, "already_present": True},
                        )
                    return OperationResult(
                        batch=self.outcome_batch(adapter, existing),
                        data={"action_id": action["id"], **existing},
                    )
                if kind == "codes":
                    if existing["state"] == "conflict":
                        raise Conflict("Состав КМ во внешней системе изменился")
                    actual = [
                        m
                        for p in existing.get("metadata", {}).get("products", [])
                        if str(p["product_id"]) == body["variant"]
                        for e in p["exemplars"]
                        for m in e.get("marks", [])
                        if m["mark_type"] == "mandatory_mark"
                    ]
                    if actual:
                        with self.db.connection() as conn:
                            self.change(
                                conn,
                                seller,
                                action["id"],
                                existing["state"],
                                {**existing, "already_present": True},
                                acknowledged=True,
                            )
                        return OperationResult(data={"action_id": action["id"], **existing})
                    # Full payload must still match immediately before submitting.
                    adapter.preflight(config, kind, body)
            except Exception as exc:
                with self.db.connection() as conn:
                    self.change(
                        conn,
                        seller,
                        action["id"],
                        "rejected",
                        {
                            "before_send": True,
                            "reason": str(exc)
                            if isinstance(exc, FlowError)
                            else "Не удалось проверить внешний API",
                        },
                        error="commerce_preflight_failed",
                    )
                raise
            with self.db.connection() as conn:
                conn.execute("BEGIN IMMEDIATE")
                updated = conn.execute(
                    "UPDATE commerce_actions SET state='submitting' "
                    "WHERE seller_id=? AND id=? AND state='queued'",
                    (seller, action["id"]),
                )
                if updated.rowcount != 1:
                    raise Conflict("Действие уже изменилось")
                self.event(
                    conn, seller, action["id"], "submitting", {"digest": action["body_digest"]}
                )
            try:
                receipt = adapter.send(config, kind, body)
            except Exception as exc:
                definite = isinstance(exc, RemoteError) and exc.definite_rejection
                with self.db.connection() as conn:
                    self.change(
                        conn,
                        seller,
                        action["id"],
                        "rejected" if definite else "unknown",
                        {"definite_rejection": definite},
                        error=getattr(exc, "code", "commerce_send_failed"),
                    )
                raise
            with self.db.connection() as conn:
                self.change(conn, seller, action["id"], "accepted", receipt, acknowledged=True)
            acknowledged = True
        else:
            if action["state"] in {"draft", "queued", "submitting", "cancelled"}:
                raise Conflict("Действие пока нельзя сверять")
            acknowledged = action["acknowledged"]
        outcome = adapter.reconcile(config, kind, body, acknowledged)
        with self.db.connection() as conn:
            self.change(conn, seller, action["id"], outcome["state"], outcome)
        batch = self.outcome_batch(adapter, outcome)
        return OperationResult(batch=batch, data={"action_id": action["id"], **outcome})

    @staticmethod
    def outcome_batch(adapter, outcome):
        if outcome.get("source"):
            return NormalizedBatch(orders=(adapter.order(outcome["source"]),))
        if outcome.get("product_sources"):
            return NormalizedBatch(
                products=tuple(adapter.product(v) for v in outcome["product_sources"])
            )
        return None

    def check_pagination_page(self, seller, connection, payload, result):
        phase = payload["phase"]
        identifiers = [v.external_id for v in getattr(result.batch, phase)]
        with self.db.connection() as conn:
            row = conn.execute(
                "SELECT value_json FROM commerce_snapshots WHERE seller_id=? AND "
                "connection_id=? AND key=?",
                (seller, connection, "page-" + phase),
            ).fetchone()
        if (
            row
            and payload.get("source_page", 1) > 1
            and json.loads(row[0]) == identifiers
            and identifiers
        ):
            raise Conflict("KIT повторил предыдущую страницу; покрытие неполное")

    def apply_result(self, conn, job, result):
        value = require_connection(conn, job["seller_id"], job["connection_id"])
        if value["adapter_key"] not in ACTION_KINDS:
            return
        seller, connection = job["seller_id"], job["connection_id"]
        for entry in result.data.get("objects", []):
            conn.execute(
                "INSERT INTO commerce_objects(seller_id,connection_id,kind,external_id,value_json)"
                " VALUES(?,?,?,?,?) ON CONFLICT(seller_id,connection_id,kind,external_id) DO "
                "UPDATE "
                "SET "
                "value_json=excluded.value_json,updated_at=strftime('%Y-%m-%dT%H:%M:%fZ',"
                "'now')",
                (
                    seller,
                    connection,
                    entry["kind"],
                    entry["external_id"],
                    canonical(entry["value"]),
                ),
            )
        progress = result.data.get("sync")
        if not progress:
            return
        row = conn.execute(
            "SELECT value_json FROM commerce_snapshots WHERE seller_id=? AND connection_id=?"
            " AND key='sync'",
            (seller, connection),
        ).fetchone()
        prior = json.loads(row[0])
        if prior["run_id"] != progress["run_id"]:
            raise Conflict("Синхронизация была заменена")
        counts = prior.get("counts", {})
        counts[progress["phase"]] = counts.get(progress["phase"], 0) + progress["count"]
        following = progress["following"]
        snapshot = {
            **progress,
            "counts": counts,
            "state": "running" if following else "succeeded",
            "coverage": "partial" if following else "complete",
        }
        self.snapshot(conn, seller, connection, "sync", snapshot)
        if value["adapter_key"] == "kit":
            self.snapshot(
                conn,
                seller,
                connection,
                "page-" + progress["phase"],
                [v.external_id for v in getattr(result.batch, progress["phase"])],
            )
        if following:
            if progress["page"] >= 3000:
                raise Conflict("Достигнут предел страниц; данные прочитаны не полностью")
            payload = {
                **following,
                "run_id": progress["run_id"],
                "page": progress["page"] + 1,
                "date_from": progress["date_from"],
                "date_to": progress["date_to"],
            }
            self.marking._queue(
                conn,
                seller,
                connection,
                "commerce.sync",
                payload,
                progress["run_id"] + ":" + str(payload["page"]),
            )
        else:
            self.snapshot(conn, seller, connection, "last_sync", snapshot)

    def fail(self, conn, job, code):
        value = require_connection(conn, job["seller_id"], job["connection_id"])
        if value["adapter_key"] not in ACTION_KINDS:
            return
        if job["operation_key"] == "commerce.sync":
            row = conn.execute(
                "SELECT value_json FROM commerce_snapshots WHERE seller_id=? AND connection_id=?"
                " AND key='sync'",
                (job["seller_id"], job["connection_id"]),
            ).fetchone()
            if row and json.loads(row[0]).get("run_id") == job["payload"].get("run_id"):
                self.snapshot(
                    conn,
                    job["seller_id"],
                    job["connection_id"],
                    "sync",
                    {**json.loads(row[0]), "state": "failed", "coverage": "partial", "error": code},
                )
        elif job["payload"].get("action_id") == job["scope_key"]:
            row = conn.execute(
                "SELECT * FROM commerce_actions WHERE seller_id=? AND connection_id=? AND id=?",
                (job["seller_id"], job["connection_id"], job["scope_key"]),
            ).fetchone()
            if row:
                if row["state"] == "submitting":
                    self.change(conn, job["seller_id"], row["id"], "unknown", error=code)
                elif row["state"] == "queued":
                    self.change(
                        conn,
                        job["seller_id"],
                        row["id"],
                        "rejected",
                        {"before_send": True},
                        error=code,
                    )
                else:
                    self.event(
                        conn, job["seller_id"], row["id"], "local_step_failed", {"error_code": code}
                    )

    def recover(self, conn):
        for row in conn.execute(
            "SELECT * FROM commerce_actions WHERE state IN ('queued','submitting')"
        ).fetchall():
            if row["state"] == "submitting":
                self.change(
                    conn, row["seller_id"], row["id"], "unknown", error="process_interrupted"
                )
            elif not conn.execute(
                "SELECT 1 FROM operations WHERE seller_id=? AND scope_key=? AND status='queued'",
                (row["seller_id"], row["id"]),
            ).fetchone():
                self.change(conn, row["seller_id"], row["id"], "draft", error="process_interrupted")
        for row in conn.execute("SELECT * FROM commerce_snapshots WHERE key='sync'").fetchall():
            progress = json.loads(row["value_json"])
            if (
                progress.get("state") in {"running", "queued"}
                and not conn.execute(
                    "SELECT 1 FROM operations WHERE seller_id=? AND connection_id=? AND "
                    "operation_key='commerce.sync' AND status IN ('queued','running')",
                    (row["seller_id"], row["connection_id"]),
                ).fetchone()
            ):
                self.snapshot(
                    conn,
                    row["seller_id"],
                    row["connection_id"],
                    "sync",
                    {**progress, "state": "interrupted", "coverage": "partial"},
                )

    def queue_due(self):
        with self.db.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            for row in conn.execute(
                "SELECT * FROM commerce_actions WHERE state IN ('accepted','pending') AND "
                "next_check_at<=? LIMIT 10",
                (date_text(datetime.now(UTC)),),
            ).fetchall():
                if not conn.execute(
                    "SELECT 1 FROM operations WHERE seller_id=? AND scope_key=? "
                    "AND status IN ('queued','running')",
                    (row["seller_id"], row["id"]),
                ).fetchone():
                    self.marking._queue(
                        conn,
                        row["seller_id"],
                        row["connection_id"],
                        "commerce.reconcile",
                        {"action_id": row["id"]},
                        row["id"],
                    )
                    conn.execute(
                        "UPDATE commerce_actions SET next_check_at=? WHERE id=?",
                        (date_text(datetime.now(UTC) + timedelta(seconds=30)), row["id"]),
                    )
