"""Seller-scoped WB cache, explicit product links and durable FBS command journal."""

import json
import sqlite3
import time
from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

from pydantic import Field, ValidationError

from fbe_flow.core.errors import Conflict, FlowError, InvalidInput, NotFound
from fbe_flow.core.models import Contract, NormalizedBatch, OperationResult, Text
from fbe_flow.integrations.chz.formats import digest, gtin_text
from fbe_flow.integrations.chz.http import RemoteError
from fbe_flow.integrations.wb.adapter import order_ids
from fbe_flow.modules.catalog import check_source_binding
from fbe_flow.modules.connections import connection_context, require_connection
from fbe_flow.modules.marking import canonical
from fbe_flow.modules.records import Records, decode_record
from fbe_flow.modules.workspace import sales_predicate

KINDS = {"products", "warehouses", "orders", "supplies"}
TERMINAL = {"confirmed", "cancelled"}


class CodeSelection(Contract):
    order_id: Text
    code_ids: list[Text] = Field(min_length=1, max_length=100)


class ExpirationSelection(Contract):
    order_id: Text
    batch_id: Text


class SupplyName(Contract):
    name: Text = Field(max_length=95)


class SupplyTarget(Contract):
    supply_id: Text


class SupplySelection(SupplyTarget):
    order_ids: list[Text] = Field(min_length=1, max_length=100)


PAYLOADS = {
    "sgtin": CodeSelection,
    "expiration": ExpirationSelection,
    "supply_create": SupplyName,
    "supply_add": SupplySelection,
    "supply_deliver": SupplyTarget,
    "supply_delete": SupplyTarget,
}


def decode_action(row):
    value = dict(row)
    value["body"] = json.loads(value.pop("body_json"))
    value["result"] = json.loads(value.pop("result_json"))
    value["acknowledged"] = bool(value["acknowledged"])
    return value


class Fulfillment:
    def __init__(self, database, connections, operations, registry, marking):
        self.db, self.connections, self.operations = database, connections, operations
        self.registry, self.marking = registry, marking

    def _connection(self, seller, connection):
        value = self.connections.get(seller, connection)
        if value["adapter_key"] != "wb":
            raise InvalidInput("Выберите подключение WB")
        return value

    def refresh_capabilities(self):
        if not any(v["key"] == "wb" for v in self.registry.descriptors()):
            return
        adapter = self.registry.get("wb")
        with self.db.connection() as conn:
            for row in conn.execute(
                "SELECT seller_id,id,config_json FROM connections WHERE adapter_key='wb'"
            ).fetchall():
                config = json.loads(row["config_json"])
                conn.execute(
                    "UPDATE connections SET operations_json=? WHERE seller_id=? AND id=?",
                    (
                        canonical(adapter.supported_operations(config["read_only"])),
                        row["seller_id"],
                        row["id"],
                    ),
                )

    @staticmethod
    def snapshot(conn, seller, connection, key, value):
        conn.execute(
            "INSERT INTO wb_snapshots(seller_id,connection_id,key,value_json) VALUES (?,?,?,?) "
            "ON CONFLICT(seller_id,connection_id,key) DO UPDATE SET value_json=excluded.value_json,"
            "updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')",
            (seller, connection, key, canonical(value)),
        )

    def overview(self, seller, connection):
        value = self._connection(seller, connection)
        with self.db.connection() as conn:
            snapshots = {
                r["key"]: {"value": json.loads(r["value_json"]), "updated_at": r["updated_at"]}
                for r in conn.execute(
                    "SELECT * FROM wb_snapshots WHERE seller_id=? AND connection_id=?",
                    (seller, connection),
                )
            }
            counts = {
                kind: conn.execute(
                    f"SELECT count(*) FROM {kind} WHERE seller_id=? AND connection_id=?",
                    (seller, connection),
                ).fetchone()[0]
                for kind in KINDS
            }
        with self.db.connection() as conn:
            stages = {
                r[0]: r[1]
                for r in conn.execute(
                    "SELECT status,count(*) FROM orders WHERE seller_id=? AND connection_id=? "
                    "AND json_extract(attributes_json,'$.source.deliveryType')='fbs' "
                    "GROUP BY status",
                    (seller, connection),
                )
            }
        return {
            "stages": stages,
            "parameters": {
                "tin": value["config"]["tin"],
                "account_id": value["external_account_id"],
                "read_only": value["config"]["read_only"],
            },
            "snapshots": snapshots,
            "counts": counts,
        }

    def records(
        self,
        seller,
        connection,
        kind,
        offset=0,
        limit=100,
        search="",
        supply=None,
        stage="",
        status="",
        warehouse="",
    ):
        self._connection(seller, connection)
        if kind not in KINDS or offset < 0 or not 1 <= limit <= 200 or len(search) > 150:
            raise InvalidInput("Неверные параметры страницы WB")
        predicate = "seller_id=? AND connection_id=?"
        args = [seller, connection]
        if search:
            predicate += (
                " AND (external_id LIKE ? ESCAPE '\\' OR attributes_json LIKE ? ESCAPE '\\')"
            )
            pattern = (
                "%" + search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            )
            args += [pattern, pattern]
        if supply is not None:
            if kind != "orders":
                raise InvalidInput("Фильтр поставки доступен для заданий")
            predicate += " AND supply_external_id=?"
            args.append(supply)
        extra, values = sales_predicate("wb", kind, stage, status, warehouse)
        predicate += extra
        args.extend(values)
        with self.db.connection() as conn:
            total = conn.execute(f"SELECT count(*) FROM {kind} WHERE {predicate}", args).fetchone()[
                0
            ]
            items = [
                decode_record(r)
                for r in conn.execute(
                    f"SELECT * FROM {kind} WHERE {predicate} "
                    "ORDER BY updated_at DESC,external_id,id LIMIT ? OFFSET ?",
                    (*args, limit, offset),
                )
            ]
            if kind == "orders":
                for item in items:
                    assignments = conn.execute(
                        "SELECT a.action_id,c.code,c.external_status,c.product_group FROM "
                        "wb_code_assignments a "
                        "JOIN marking_codes c ON c.seller_id=a.seller_id AND c.id=a.code_id "
                        "WHERE a.seller_id=? AND a.order_id=?",
                        (seller, item["id"]),
                    ).fetchall()
                    item["marking"] = [dict(r) for r in assignments]
                from fbe_flow.modules.packing import enrich_orders

                enrich_orders(conn, seller, connection, items)
        return {"items": items, "total": total, "offset": offset, "limit": limit}

    def start_sync(self, seller, connection):
        self._connection(seller, connection)
        with self.db.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            return self._start_sync(conn, seller, connection)

    def _start_sync(self, conn, seller, connection):
        if conn.execute(
            "SELECT 1 FROM operations WHERE seller_id=? AND connection_id=? AND "
            "operation_key='wb.sync' AND status IN ('queued','running')",
            (seller, connection),
        ).fetchone():
            raise Conflict("Синхронизация WB уже выполняется")
        run = str(uuid4())
        end = int(time.time())
        payload = {
            "phase": "warehouses",
            "run_id": run,
            "page": 0,
            "date_from": end - 30 * 86400,
            "date_to": end,
        }
        operation = self.marking._queue(conn, seller, connection, "wb.sync", payload, run + ":0")
        self.snapshot(
            conn,
            seller,
            connection,
            "sync",
            {"run_id": run, "state": "queued", "page": 0, "counts": {}},
        )
        return {"id": operation, "run_id": run}

    def links(self, seller, connection):
        self._connection(seller, connection)
        with self.db.connection() as conn:
            return [
                dict(r)
                for r in conn.execute(
                    "SELECT l.*,p.title AS product_title,p.external_id AS product_external_id,"
                    "n.title AS chz_title FROM wb_links l "
                    "JOIN products p ON p.seller_id=l.seller_id AND p.id=l.product_id "
                    "JOIN products n ON n.seller_id=l.seller_id AND n.id=l.chz_product_id "
                    "WHERE l.seller_id=? AND l.connection_id=? ORDER BY p.title,l.chrt_id",
                    (seller, connection),
                )
            ]

    def order_link(self, seller, connection, order_id):
        self._connection(seller, connection)
        order = Records(self.db).get(seller, "orders", order_id)
        if order["connection_id"] != connection:
            raise NotFound("Задание WB не найдено в этом подключении")
        source = order["attributes"]["source"]
        with self.db.connection() as conn:
            row = conn.execute(
                "SELECT l.* FROM wb_links l JOIN products p ON p.seller_id=l.seller_id "
                "AND p.id=l.product_id WHERE l.seller_id=? AND l.connection_id=? "
                "AND l.chrt_id=? AND p.external_id=?",
                (seller, connection, str(source.get("chrtId")), str(source.get("nmId"))),
            ).fetchone()
            if row:
                check_source_binding(
                    conn,
                    seller,
                    row["product_id"],
                    row["chrt_id"],
                    row["gtin"],
                    row["product_group"],
                )
        if not row:
            raise InvalidInput("Сначала свяжите размер товара WB с GTIN Честного Знака")
        return order, dict(row)

    def available_codes(self, seller, connection, order_id, offset=0, limit=100):
        _, link = self.order_link(seller, connection, order_id)
        predicate = (
            "c.seller_id=? AND c.connection_id=? AND c.gtin=? AND c.product_group=? "
            "AND c.full_code IS NOT NULL AND NOT EXISTS (SELECT 1 FROM code_reservations a "
            "WHERE a.code=c.code)"
        )
        args = (seller, link["chz_connection_id"], link["gtin"], link["product_group"])
        with self.db.connection() as conn:
            total = conn.execute(
                "SELECT count(*) FROM marking_codes c WHERE " + predicate, args
            ).fetchone()[0]
            items = [
                dict(r)
                for r in conn.execute(
                    "SELECT c.id,c.code,c.gtin,c.product_group,c.external_status "
                    "FROM marking_codes c "
                    "WHERE " + predicate + " ORDER BY c.updated_at,c.id LIMIT ? OFFSET ?",
                    (*args, limit, offset),
                )
            ]
        return {"items": items, "total": total, "offset": offset, "limit": limit}

    def link(self, seller, connection, payload):
        wb = self._connection(seller, connection)
        chz = self.marking._connection(seller, payload["chz_connection_id"])
        if (
            chz["config"].get("environment") != "production"
            or chz["config"]["inn"] != wb["config"]["tin"]
        ):
            raise InvalidInput("WB нужно связать с производственным аккаунтом ЧЗ с тем же ИНН")
        records = Records(self.db)
        product = records.get(seller, "products", payload["product_id"])
        nk = records.get(seller, "products", payload["chz_product_id"])
        if product["connection_id"] != connection or nk["connection_id"] != chz["id"]:
            raise InvalidInput("Товары принадлежат другому подключению")
        variant = str(payload["chrt_id"])
        if variant not in {
            str(v.get("chrtID")) for v in product["attributes"]["source"].get("sizes", [])
        }:
            raise InvalidInput("Размер chrtID не принадлежит выбранной карточке WB")
        code = gtin_text(payload["gtin"])
        if code not in nk["identifiers"].get("gtin", []):
            raise InvalidInput("GTIN отсутствует в выбранной карточке НК")
        group = payload["product_group"]
        if not isinstance(group, str) or not group.strip():
            raise InvalidInput("Выберите товарную группу ЧЗ")
        adapter = self.registry.get("chz")
        adapter.require_group(adapter.config(connection_context(chz)), group)
        with self.db.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            check_source_binding(conn, seller, product["id"], variant, code, group)
            present = conn.execute(
                "SELECT present,detail_available FROM nk_cards WHERE seller_id=? AND "
                "connection_id=? AND external_id=?",
                (seller, chz["id"], nk["external_id"]),
            ).fetchone()
            if not present or not present["present"] or not present["detail_available"]:
                raise Conflict("Обновите собственную карточку НК перед связыванием")
            existing = conn.execute(
                "SELECT id FROM wb_links WHERE seller_id=? AND connection_id=? AND "
                "product_id=? AND chrt_id=?",
                (seller, connection, product["id"], variant),
            ).fetchone()
            if existing:
                raise Conflict("Этот размер WB уже связан; сначала удалите прежнюю связь")
            link_id = str(uuid4())
            conn.execute(
                "INSERT INTO wb_links(id,seller_id,connection_id,product_id,chrt_id,chz"
                "_connection_id,chz_product_id,gtin,product_group) VALUES "
                "(?,?,?,?,?,?,?,?,?)",
                (
                    link_id,
                    seller,
                    connection,
                    product["id"],
                    variant,
                    chz["id"],
                    nk["id"],
                    code,
                    group,
                ),
            )
        return next(v for v in self.links(seller, connection) if v["id"] == link_id)

    def unlink(self, seller, connection, link_id):
        self._connection(seller, connection)
        with self.db.connection() as conn:
            if not conn.execute(
                "DELETE FROM wb_links WHERE seller_id=? AND connection_id=? AND id=?",
                (seller, connection, link_id),
            ).rowcount:
                raise NotFound("Связь не найдена")
        return {"deleted": True}

    def actions(self, seller, connection):
        self._connection(seller, connection)
        with self.db.connection() as conn:
            return [
                decode_action(r)
                for r in conn.execute(
                    "SELECT * FROM wb_actions WHERE seller_id=? AND connection_id=? ORDER "
                    "BY created_at DESC,id LIMIT 100",
                    (seller, connection),
                )
            ]

    def action(self, seller, action_id):
        with self.db.connection() as conn:
            row = conn.execute(
                "SELECT * FROM wb_actions WHERE seller_id=? AND id=?", (seller, action_id)
            ).fetchone()
            if row is None:
                raise NotFound("Действие WB не найдено")
            value = decode_action(row)
            value["events"] = [
                dict(r) | {"data": json.loads(r["data_json"])}
                for r in conn.execute(
                    "SELECT state,data_json,created_at FROM wb_events WHERE seller_id=? AND"
                    " action_id=? ORDER BY id",
                    (seller, action_id),
                )
            ]
            return value

    @staticmethod
    def event(conn, seller, action_id, state, data):
        conn.execute(
            "INSERT INTO wb_events(seller_id,action_id,state,data_json) VALUES (?,?,?,?)",
            (seller, action_id, state, canonical(data)),
        )

    def _change(
        self, conn, seller, action_id, state, result=None, *, error=None, acknowledged=None
    ):
        args = [state, canonical(result or {}), error, seller, action_id]
        conn.execute(
            "UPDATE wb_actions SET state=?,result_json=?,error_code=?,updated_at=st"
            "rftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE seller_id=? AND id=?",
            args,
        )
        if acknowledged is not None:
            conn.execute(
                "UPDATE wb_actions SET acknowledged=? WHERE seller_id=? AND id=?",
                (int(acknowledged), seller, action_id),
            )
        due = (
            (datetime.now(UTC) + timedelta(seconds=30))
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
            if state in {"accepted", "pending"}
            else None
        )
        conn.execute(
            "UPDATE wb_actions SET next_check_at=? WHERE seller_id=? AND id=?",
            (due, seller, action_id),
        )
        self.event(conn, seller, action_id, state, {"error_code": error, **(result or {})})
        if state in TERMINAL or state == "partial":
            conn.execute(
                "DELETE FROM wb_action_targets WHERE seller_id=? AND action_id=?",
                (seller, action_id),
            )

    def prepare(self, seller, connection, kind, payload):
        wb = self._connection(seller, connection)
        try:
            payload = PAYLOADS[kind].model_validate(payload).model_dump()
        except (KeyError, ValidationError) as exc:
            raise InvalidInput("Проверьте поля действия WB") from exc
        if wb["config"]["read_only"]:
            raise InvalidInput("Токен WB разрешает только чтение")
        action_id = str(uuid4())
        body, targets, codes = {}, [], []
        records = Records(self.db)
        if kind == "sgtin":
            order = records.get(seller, "orders", payload["order_id"])
            if (
                order["connection_id"] != connection
                or order["attributes"]["source"].get("deliveryType") != "fbs"
            ):
                raise InvalidInput("Выберите задание FBS этого подключения")
            source = order["attributes"]["source"]
            if order["status"] != "confirm":
                raise InvalidInput(
                    "Сначала добавьте задание в активную поставку WB и обновите данные"
                )
            links = self.links(seller, connection)
            link = next(
                (
                    v
                    for v in links
                    if v["chrt_id"] == str(source.get("chrtId"))
                    and records.get(seller, "products", v["product_id"])["external_id"]
                    == str(source.get("nmId"))
                ),
                None,
            )
            if not link:
                raise InvalidInput("Сначала свяжите размер товара WB с GTIN Честного Знака")
            with self.db.connection() as conn:
                check_source_binding(
                    conn,
                    seller,
                    link["product_id"],
                    link["chrt_id"],
                    link["gtin"],
                    link["product_group"],
                )
            codes = self.marking.validate_code_selection(
                seller, link["chz_connection_id"], payload["code_ids"], full=True
            )
            if len(codes) > 100:
                raise InvalidInput("WB принимает не более 100 кодов на задание")
            if any(
                v["gtin"] != link["gtin"] or v["product_group"] != link["product_group"]
                for v in codes
            ):
                raise InvalidInput("Коды должны соответствовать GTIN и группе связанного товара")
            body = {
                "order_id": order["id"],
                "order_external_id": order["external_id"],
                "chz_connection_id": link["chz_connection_id"],
                "gtin": link["gtin"],
                "product_group": link["product_group"],
                "code_ids": [v["id"] for v in codes],
                "sgtins": [v["full_code"] for v in codes],
                "cis": [v["code"] for v in codes],
                "link_id": link["id"],
            }
            targets = ["order:" + order["external_id"]]
        elif kind == "expiration":
            from fbe_flow.modules.packing import order_batch

            order = records.get(seller, "orders", payload["order_id"])
            if order["connection_id"] != connection or order["status"] != "confirm":
                raise InvalidInput("Срок годности доступен для заданий этого аккаунта на сборке")
            if order["attributes"]["source"].get("deliveryType") != "fbs":
                raise InvalidInput("Выберите задание FBS")
            with self.db.connection() as conn:
                batch = order_batch(conn, seller, connection, order, payload["batch_id"])
            expires = date.fromisoformat(batch["expires_on"])
            if expires < date.today() + timedelta(days=30):
                raise InvalidInput("WB требует не менее 30 дней до окончания срока годности")
            body = {
                "order_id": order["id"],
                "order_external_id": order["external_id"],
                "batch_id": batch["id"],
                "batch_name": batch["name"],
                "expiration": expires.strftime("%d.%m.%Y"),
            }
            targets = ["order:" + order["external_id"]]
        elif kind == "supply_create":
            name = payload.get("name", "").strip()
            if not 1 <= len(name) <= 95:
                raise InvalidInput("Название поставки: от 1 до 95 символов")
            body = {"name": name, "remote_name": name + " [FBE " + action_id[:24] + "]"}
            targets = ["create:" + action_id]
        elif kind in {"supply_add", "supply_deliver", "supply_delete"}:
            supply = records.get(seller, "supplies", payload["supply_id"])
            if supply["connection_id"] != connection:
                raise InvalidInput("Поставка принадлежит другому подключению")
            body = {"supply_id": supply["external_id"]}
            targets = ["supply:" + supply["external_id"]]
            adapter = self.registry.get("wb")
            config = adapter.config(connection_context(wb))
            if kind == "supply_add":
                selected = payload.get("order_ids")
                if (
                    not isinstance(selected, list)
                    or not 1 <= len(selected) <= 100
                    or len(set(selected)) != len(selected)
                ):
                    raise InvalidInput("Выберите от 1 до 100 разных заданий")
                orders = [records.get(seller, "orders", v) for v in selected]
                if any(
                    v["connection_id"] != connection
                    or v["attributes"]["source"].get("deliveryType") != "fbs"
                    for v in orders
                ):
                    raise InvalidInput("Выберите задания FBS этого подключения")
                members = adapter.members(config, supply["external_id"])
                with self.db.connection() as conn:
                    existing = [
                        decode_record(r)
                        for r in conn.execute(
                            "SELECT * FROM orders WHERE seller_id=? AND connection_id=? AND "
                            "external_id IN (SELECT value FROM json_each(?))",
                            (seller, connection, canonical([str(v) for v in members])),
                        )
                    ]
                if len(existing) != len(members):
                    raise Conflict("Обновите задания WB: состав поставки не полностью загружен")
                all_orders = orders + existing
                for key in ("warehouseId", "cargoType", "crossBorderType"):
                    values = [v["attributes"]["source"].get(key) for v in all_orders]
                    if any(v is None for v in values) or len(set(values)) > 1:
                        raise InvalidInput(
                            "Поставка требует одинаковые склад, габаритный и трансграничный типы"
                        )
                flags = [
                    bool(v["attributes"]["source"].get("options", {}).get("isB2B", False))
                    for v in all_orders
                ]
                if len(set(flags)) > 1:
                    raise InvalidInput("Задания B2B и B2C должны быть в разных поставках")
                body.update(
                    orders=order_ids([v["external_id"] for v in orders], 100),
                    expected_members=members,
                )
                targets += ["order:" + v["external_id"] for v in orders]
        else:
            raise InvalidInput("Неизвестное действие WB")
        try:
            with self.db.connection() as conn:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    "INSERT INTO "
                    "wb_actions(id,seller_id,connection_id,kind,body_json,body_digest) "
                    "VALUES (?,?,?,?,?,?)",
                    (action_id, seller, connection, kind, canonical(body), digest(body)),
                )
                for target in targets:
                    conn.execute(
                        "INSERT INTO "
                        "wb_action_targets(seller_id,connection_id,target_key,action_id) VALUES"
                        " (?,?,?,?)",
                        (seller, connection, target, action_id),
                    )
                for code in codes:
                    conn.execute(
                        "INSERT INTO wb_code_assignments(seller_id,code,code_id,connection_id,o"
                        "rder_id,action_id) VALUES (?,?,?,?,?,?)",
                        (seller, code["code"], code["id"], connection, body["order_id"], action_id),
                    )
                self.event(conn, seller, action_id, "draft", {"kind": kind})
        except sqlite3.IntegrityError as exc:
            raise Conflict("Задание, поставка или код уже закреплены за другим действием") from exc
        return self.action(seller, action_id)

    def enqueue(self, seller, action_id, *, reconcile=False):
        with self.db.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM wb_actions WHERE seller_id=? AND id=?", (seller, action_id)
            ).fetchone()
            if row is None:
                raise NotFound("Действие WB не найдено")
            value = decode_action(row)
            if (
                value["state"] in TERMINAL
                or (not reconcile and value["state"] != "draft")
                or (reconcile and value["state"] in {"draft", "queued", "submitting"})
            ):
                raise Conflict("Действие недоступно в текущем состоянии")
            if conn.execute(
                "SELECT 1 FROM operations WHERE seller_id=? AND scope_key=? AND status "
                "IN ('queued','running')",
                (seller, action_id),
            ).fetchone():
                raise Conflict("Действие уже выполняется")
            connection = require_connection(conn, seller, value["connection_id"])
            if not reconcile and connection["config"]["read_only"]:
                raise InvalidInput("Токен WB разрешает только чтение")
            operation = self.marking._queue(
                conn,
                seller,
                value["connection_id"],
                "wb.reconcile" if reconcile else "wb.command",
                {"action_id": action_id},
                action_id,
            )
            if not reconcile:
                self._change(conn, seller, action_id, "queued")
        return self.operations.get(seller, operation)

    def cancel(self, seller, action_id):
        with self.db.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM wb_actions WHERE seller_id=? AND id=?", (seller, action_id)
            ).fetchone()
            if row is None:
                raise NotFound("Действие WB не найдено")
            if row["state"] not in {"draft", "rejected"} or row["acknowledged"]:
                raise Conflict("Отменить можно черновик или однозначно отклонённую отправку")
            conn.execute(
                "DELETE FROM wb_code_assignments WHERE seller_id=? AND action_id=?",
                (seller, action_id),
            )
            self._change(conn, seller, action_id, "cancelled")
        return self.action(seller, action_id)

    def retry_codes(self, seller, action_id):
        value = self.action(seller, action_id)
        if value["kind"] != "sgtin" or value["state"] != "unknown" or value["acknowledged"]:
            raise Conflict("Повтор доступен только для передачи кодов с потерянным ответом")
        wb = self._connection(seller, value["connection_id"])
        adapter = self.registry.get("wb")
        config = adapter.config(connection_context(wb))
        # PUT of the same saved set is idempotent. Never repeat a supply creation.
        adapter.preflight(config, "sgtin", value["body"])
        outcome = adapter.reconcile(config, "sgtin", value["body"], {})
        with self.db.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            current = conn.execute(
                "SELECT state,body_digest FROM wb_actions WHERE seller_id=? AND id=?",
                (seller, action_id),
            ).fetchone()
            if (
                not current
                or current["state"] != "unknown"
                or current["body_digest"] != value["body_digest"]
            ):
                raise Conflict("Состояние действия уже изменилось")
            if outcome["state"] != "unknown":
                self._change(
                    conn,
                    seller,
                    action_id,
                    outcome["state"],
                    outcome,
                    acknowledged=bool(outcome.get("metadata", {}).get("value")),
                )
                return {"action_id": action_id, "state": outcome["state"]}
            if conn.execute(
                "SELECT 1 FROM operations WHERE seller_id=? AND scope_key=? "
                "AND status IN ('queued','running')",
                (seller, action_id),
            ).fetchone():
                raise Conflict("Действие уже выполняется")
            self.event(
                conn, seller, action_id, "retry_requested", {"previous_result": value["result"]}
            )
            self._change(conn, seller, action_id, "queued")
            operation = self.marking._queue(
                conn,
                seller,
                value["connection_id"],
                "wb.command",
                {"action_id": action_id},
                action_id,
            )
        return self.operations.get(seller, operation)

    def supply_details(self, seller, connection, supply_id):
        wb = self._connection(seller, connection)
        supply = Records(self.db).get(seller, "supplies", supply_id)
        if supply["connection_id"] != connection:
            raise NotFound("Поставка не найдена в этом подключении")
        adapter = self.registry.get("wb")
        config = adapter.config(connection_context(wb))
        source = adapter.supply_info(config, supply["external_id"])
        ids = adapter.members(config, supply["external_id"])
        statuses, metas = adapter.statuses(config, ids), adapter.metas(config, ids)
        with self.db.connection() as conn:
            cached = {
                r["external_id"]: decode_record(r)
                for r in conn.execute(
                    "SELECT * FROM orders WHERE seller_id=? AND connection_id=? "
                    "AND external_id IN (SELECT value FROM json_each(?))",
                    (seller, connection, canonical([str(v) for v in ids])),
                )
            }
        return {
            "supply": source,
            "orders": [
                {
                    "external_id": str(v),
                    "status": statuses[str(v)]["supplierStatus"],
                    "wb_status": statuses[str(v)].get("wbStatus"),
                    "metadata": metas[str(v)],
                    "cached": cached.get(str(v)),
                }
                for v in ids
            ],
        }

    def _check_chz(self, seller, body):
        with self.db.connection() as conn:
            link = conn.execute(
                "SELECT * FROM wb_links WHERE seller_id=? AND id=?",
                (seller, body.get("link_id", "")),
            ).fetchone()
            if link:
                check_source_binding(
                    conn,
                    seller,
                    link["product_id"],
                    link["chrt_id"],
                    body["gtin"],
                    body["product_group"],
                )
        chz = self.marking._connection(seller, body["chz_connection_id"])
        wb_codes = self.marking.validate_code_selection(
            seller, chz["id"], body["code_ids"], full=True
        )
        if [v["full_code"] for v in wb_codes] != body["sgtins"] or [
            v["code"] for v in wb_codes
        ] != body["cis"]:
            raise Conflict("Сохранённые коды изменились; подготовьте передачу заново")
        adapter = self.registry.get("chz")
        config = adapter.config(connection_context(chz))
        if config.environment != "production":
            raise InvalidInput("В WB нельзя передать коды тестового контура ЧЗ")
        values = adapter.check_codes(config, body["sgtins"], body["product_group"])
        for row in values:
            info = row.get("cisInfo") or {}
            if (
                row.get("errorCode")
                or info.get("status") != "INTRODUCED"
                or info.get("ownerInn") != config.inn
                or info.get("gtin") != body["gtin"]
                or info.get("productGroup") != body["product_group"]
                or info.get("packageType") != "UNIT"
                or info.get("statusEx")
            ):
                raise Conflict(
                    "Код должен быть введён в оборот, принадлежать продавцу и "
                    "соответствовать товару"
                )
        return values

    def _check_supply_codes(self, seller, connection, readiness):
        if not readiness:
            return
        with self.db.connection() as conn:
            actions = conn.execute(
                "SELECT DISTINCT w.body_json,w.body_digest FROM wb_code_assignments a "
                "JOIN wb_actions w ON w.seller_id=a.seller_id AND w.id=a.action_id "
                "JOIN orders o ON o.seller_id=a.seller_id AND o.id=a.order_id "
                "WHERE a.seller_id=? AND a.connection_id=? AND o.external_id IN "
                "(SELECT value FROM json_each(?))",
                (seller, connection, canonical([str(v) for v in readiness["members"]])),
            ).fetchall()
        groups = {}
        for row in actions:
            body = json.loads(row["body_json"])
            if digest(body) != row["body_digest"]:
                raise Conflict("Назначение кодов поставки изменилось")
            meta = readiness["metas"][body["order_external_id"]].get("sgtin")
            if not meta or set(meta["value"] or []) != set(body["sgtins"]):
                raise Conflict("Назначенные коды поставки отличаются от метаданных WB")
            key = (body["chz_connection_id"], body["gtin"], body["product_group"])
            group = groups.setdefault(
                key, {k: v for k, v in body.items() if k not in {"code_ids", "sgtins", "cis"}}
            )
            for field in ("code_ids", "sgtins", "cis"):
                group.setdefault(field, []).extend(body[field])
        for group in groups.values():
            for start in range(0, len(group["code_ids"]), 500):
                body = {
                    **group,
                    **{k: group[k][start : start + 500] for k in ("code_ids", "sgtins", "cis")},
                }
                values = self._check_chz(seller, body)
                with self.db.connection() as conn:
                    self.marking._apply_codes(
                        conn,
                        {"seller_id": seller, "connection_id": body["chz_connection_id"]},
                        values,
                    )

    def execute(self, adapter, context, key, payload):
        expected_scope = (
            str(payload.get("run_id")) + ":" + str(payload.get("page"))
            if key == "wb.sync"
            else payload.get("action_id")
        )
        with self.db.connection() as conn:
            running = conn.execute(
                "SELECT 1 FROM operations WHERE seller_id=? AND connection_id=? "
                "AND operation_key=? AND scope_key=? AND status='running' AND payload_json=?",
                (context.seller_id, context.connection_id, key, expected_scope, canonical(payload)),
            ).fetchone()
        if not running:
            raise InvalidInput("Запустите действие через соответствующий раздел WB")
        if key == "wb.sync":
            with self.db.connection() as conn:
                row = conn.execute(
                    "SELECT value_json FROM wb_snapshots WHERE seller_id=? AND "
                    "connection_id=? AND key='sync'",
                    (context.seller_id, context.connection_id),
                ).fetchone()
                if not row or json.loads(row[0]).get("run_id") != payload.get("run_id"):
                    raise InvalidInput("Запустите синхронизацию из раздела Wildberries")
            result = adapter.execute(context, key, payload)
            # Deleted warehouses and older supplies need not be returned by WB. Keep their raw IDs.
            with self.db.connection() as conn:
                warehouses = {
                    r[0]
                    for r in conn.execute(
                        "SELECT external_id FROM warehouses WHERE seller_id=? AND connection_id=?",
                        (context.seller_id, context.connection_id),
                    )
                }
                supplies = {
                    r[0]
                    for r in conn.execute(
                        "SELECT external_id FROM supplies WHERE seller_id=? AND connection_id=?",
                        (context.seller_id, context.connection_id),
                    )
                }
            if result.batch and result.batch.orders:
                batch = result.batch.model_copy(
                    update={
                        "orders": tuple(
                            v.model_copy(
                                update={
                                    "warehouse_external_id": v.warehouse_external_id
                                    if v.warehouse_external_id in warehouses
                                    else None,
                                    "supply_external_id": v.supply_external_id
                                    if v.supply_external_id in supplies
                                    else None,
                                }
                            )
                            for v in result.batch.orders
                        )
                    }
                )
                result = result.model_copy(update={"batch": batch})
            return result
        if key not in {"wb.command", "wb.reconcile"}:
            raise InvalidInput("Неизвестная операция WB")
        seller = context.seller_id
        value = self.action(seller, payload.get("action_id"))
        if (
            value["connection_id"] != context.connection_id
            or digest(value["body"]) != value["body_digest"]
        ):
            raise InvalidInput("Действие WB не соответствует подключению или изменилось")
        config = adapter.config(context)
        body, kind = value["body"], value["kind"]
        if (
            kind == "sgtin"
            and config.tin
            != self.marking._connection(seller, body["chz_connection_id"])["config"]["inn"]
        ):
            raise InvalidInput("ИНН аккаунтов WB и ЧЗ не совпадает")
        if key == "wb.command":
            if value["state"] != "queued":
                raise Conflict("Действие уже отправлялось; сначала сверяйте результат")
            try:
                readiness = adapter.preflight(config, kind, body)
                if kind == "supply_deliver":
                    self._check_supply_codes(seller, context.connection_id, readiness)
                if kind == "sgtin":
                    codes = self._check_chz(seller, body)
                    with self.db.connection() as conn:
                        self.marking._apply_codes(
                            conn,
                            {"seller_id": seller, "connection_id": body["chz_connection_id"]},
                            codes,
                        )
            except Exception as exc:
                with self.db.connection() as conn:
                    self._change(
                        conn,
                        seller,
                        value["id"],
                        "rejected",
                        {
                            "before_send": True,
                            "reason": str(exc)
                            if isinstance(exc, FlowError)
                            else "Не удалось проверить внешний API; обновите доступ "
                            "и повторите подготовку",
                        },
                        error="wb_preflight_failed",
                    )
                raise
            # Check current remote state before a write; exact matches need no repeated mutation.
            existing = (
                adapter.reconcile(config, kind, body, {}) if kind != "supply_create" else None
            )
            if kind == "sgtin" and existing:
                if existing["state"] == "conflict":
                    with self.db.connection() as conn:
                        self._change(
                            conn,
                            seller,
                            value["id"],
                            "rejected",
                            {**existing, "before_send": True},
                            error="wb_preflight_changed",
                        )
                    raise Conflict("Метаданные WB изменились; перезапись кодов запрещена")
                if existing.get("metadata", {}).get("value"):
                    with self.db.connection() as conn:
                        self._change(
                            conn,
                            seller,
                            value["id"],
                            existing["state"],
                            {**existing, "already_present": True},
                            acknowledged=True,
                        )
                    return OperationResult(data={"action_id": value["id"], **existing})
            if existing and existing["state"] == "confirmed":
                with self.db.connection() as conn:
                    self._change(conn, seller, value["id"], "confirmed", existing)
                return OperationResult(data={"action_id": value["id"], **existing})
            with self.db.connection() as conn:
                conn.execute("BEGIN IMMEDIATE")
                changed = conn.execute(
                    "UPDATE wb_actions SET state='submitting' WHERE seller_id=? AND id=? "
                    "AND state='queued'",
                    (seller, value["id"]),
                )
                if changed.rowcount != 1:
                    raise Conflict("Состояние действия изменилось")
                self.event(
                    conn, seller, value["id"], "submitting", {"body_digest": value["body_digest"]}
                )
            try:
                receipt = adapter.send(config, kind, body)
            except Exception as exc:
                definite = isinstance(exc, RemoteError) and (
                    exc.definite_rejection or exc.status == 402
                )
                with self.db.connection() as conn:
                    self._change(
                        conn,
                        seller,
                        value["id"],
                        "rejected" if definite else "unknown",
                        {"definite_rejection": definite},
                        error=getattr(exc, "code", "wb_send_failed"),
                    )
                raise
            receipt["acknowledged"] = True
            with self.db.connection() as conn:
                self._change(conn, seller, value["id"], "accepted", receipt, acknowledged=True)
        elif value["state"] in {"draft", "queued", "submitting", "cancelled"}:
            raise Conflict("Это действие пока нельзя сверять")
        else:
            receipt = {**value["result"], "acknowledged": value["acknowledged"]}
        outcome = adapter.reconcile(config, kind, body, receipt)
        with self.db.connection() as conn:
            self._change(conn, seller, value["id"], outcome["state"], {**receipt, **outcome})
        batch = (
            NormalizedBatch(supplies=(adapter.supply(outcome["supply"]),))
            if outcome.get("supply")
            else None
        )
        return OperationResult(batch=batch, data={"action_id": value["id"], **outcome})

    def apply_result(self, conn, job, result):
        if require_connection(conn, job["seller_id"], job["connection_id"])["adapter_key"] != "wb":
            return
        seller, connection = job["seller_id"], job["connection_id"]
        if progress := result.data.get("sync"):
            row = conn.execute(
                "SELECT value_json FROM wb_snapshots WHERE seller_id=? AND "
                "connection_id=? AND key='sync'",
                (seller, connection),
            ).fetchone()
            prior = json.loads(row[0])
            if prior.get("run_id") != progress["run_id"]:
                raise Conflict("Синхронизация WB была заменена")
            following = progress["following"]
            counts = prior.get("counts", {})
            counts[progress["phase"]] = counts.get(progress["phase"], 0) + progress["count"]
            self.snapshot(
                conn,
                seller,
                connection,
                "sync",
                {**progress, "counts": counts, "state": "running" if following else "succeeded"},
            )
            if following:
                if progress["page"] >= 2000:
                    raise Conflict("Достигнут предел страниц WB; сузьте область данных")
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
                    "wb.sync",
                    payload,
                    progress["run_id"] + ":" + str(payload["page"]),
                )
            else:
                self.snapshot(
                    conn,
                    seller,
                    connection,
                    "last_sync",
                    {
                        "run_id": progress["run_id"],
                        "counts": counts,
                        "date_from": progress["date_from"],
                        "date_to": progress["date_to"],
                    },
                )
        if result.data.get("deleted"):
            conn.execute(
                "UPDATE supplies SET status='deleted' WHERE seller_id=? AND "
                "connection_id=? AND external_id=?",
                (seller, connection, result.data["supply_id"]),
            )
        if result.data.get("action_id") and result.data.get("state") in {"confirmed", "partial"}:
            if not conn.execute(
                "SELECT 1 FROM operations WHERE seller_id=? AND connection_id=? AND "
                "operation_key='wb.sync' AND status IN ('queued','running')",
                (seller, connection),
            ).fetchone():
                self._start_sync(conn, seller, connection)

    def fail(self, conn, job, code):
        if require_connection(conn, job["seller_id"], job["connection_id"])["adapter_key"] != "wb":
            return
        if job["operation_key"] == "wb.sync":
            row = conn.execute(
                "SELECT value_json FROM wb_snapshots WHERE seller_id=? AND connection_id=? "
                "AND key='sync'",
                (job["seller_id"], job["connection_id"]),
            ).fetchone()
            if (
                not row
                or json.loads(row[0]).get("run_id") != job["payload"].get("run_id")
                or job["scope_key"]
                != str(job["payload"].get("run_id")) + ":" + str(job["payload"].get("page"))
            ):
                return
            self.snapshot(
                conn,
                job["seller_id"],
                job["connection_id"],
                "sync",
                {"state": "failed", "run_id": job["payload"].get("run_id"), "error": code},
            )
        elif job["payload"].get("action_id"):
            if job["scope_key"] != job["payload"]["action_id"]:
                return
            action = conn.execute(
                "SELECT state FROM wb_actions WHERE seller_id=? AND connection_id=? AND id=?",
                (job["seller_id"], job["connection_id"], job["payload"]["action_id"]),
            ).fetchone()
            if action:
                if action["state"] == "submitting":
                    self._change(
                        conn, job["seller_id"], job["payload"]["action_id"], "unknown", error=code
                    )
                elif action["state"] == "queued" and job["operation_key"] == "wb.command":
                    self._change(
                        conn,
                        job["seller_id"],
                        job["payload"]["action_id"],
                        "rejected",
                        {"before_send": True},
                        error=code,
                    )
                else:
                    self.event(
                        conn,
                        job["seller_id"],
                        job["payload"]["action_id"],
                        "local_step_failed",
                        {"error_code": code},
                    )

    def recover(self, conn):
        for row in conn.execute(
            "SELECT * FROM wb_actions WHERE state IN ('queued','submitting')"
        ).fetchall():
            if row["state"] == "submitting":
                self._change(
                    conn, row["seller_id"], row["id"], "unknown", error="process_interrupted"
                )
            elif not conn.execute(
                "SELECT 1 FROM operations WHERE seller_id=? AND scope_key=? AND status='queued'",
                (row["seller_id"], row["id"]),
            ).fetchone():
                self._change(
                    conn, row["seller_id"], row["id"], "draft", error="process_interrupted"
                )
        for row in conn.execute("SELECT * FROM wb_snapshots WHERE key='sync'").fetchall():
            progress = json.loads(row["value_json"])
            if (
                progress.get("state") in {"queued", "running"}
                and not conn.execute(
                    "SELECT 1 FROM operations WHERE seller_id=? AND connection_id=? AND "
                    "operation_key='wb.sync' AND status IN ('queued','running')",
                    (row["seller_id"], row["connection_id"]),
                ).fetchone()
            ):
                self.snapshot(
                    conn,
                    row["seller_id"],
                    row["connection_id"],
                    "sync",
                    {**progress, "state": "interrupted"},
                )

    def queue_due(self):
        now = datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        with self.db.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            for row in conn.execute(
                "SELECT seller_id,connection_id,id FROM wb_actions WHERE state IN "
                "('accepted','pending') AND next_check_at<=? LIMIT 10",
                (now,),
            ).fetchall():
                if not conn.execute(
                    "SELECT 1 FROM operations WHERE seller_id=? AND scope_key=? AND status "
                    "IN ('queued','running')",
                    (row["seller_id"], row["id"]),
                ).fetchone():
                    self.marking._queue(
                        conn,
                        row["seller_id"],
                        row["connection_id"],
                        "wb.reconcile",
                        {"action_id": row["id"]},
                        row["id"],
                    )
                    due = (
                        (datetime.now(UTC) + timedelta(seconds=30))
                        .isoformat(timespec="milliseconds")
                        .replace("+00:00", "Z")
                    )
                    conn.execute(
                        "UPDATE wb_actions SET next_check_at=? WHERE seller_id=? AND id=?",
                        (due, row["seller_id"], row["id"]),
                    )
