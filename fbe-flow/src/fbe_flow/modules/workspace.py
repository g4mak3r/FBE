"""Local UI preferences and read models. No integration calls or marking writes."""

import json
import re
import sqlite3
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode
from uuid import uuid4

from pydantic import Field

from fbe_flow.core.errors import InvalidInput
from fbe_flow.core.models import Contract
from fbe_flow.modules.records import decode_record
from fbe_flow.modules.sellers import require_seller

CHANNELS = {"wb": "WB", "ozon": "Ozon", "kit": "Интернет-магазин", "chz": "Честный Знак"}
WIDGETS = {
    "assembly": "К сборке",
    "shipping": "К отгрузке",
    "attention": "Требует внимания",
    "codes": "Коды маркировки",
    "documents": "Документы соответствия",
}
ORDER_STAGES = {
    "wb": {"assembly": ("new", "confirm"), "shipping": ()},
    "ozon": {"assembly": ("awaiting_packaging",), "shipping": ("awaiting_deliver",)},
    "kit": {
        "assembly": ("NEW", "ORDER_PLACED", "WAIT_FOR_CONFIRMATION"),
        "shipping": ("WAIT_FOR_DELIVERY",),
    },
}
STATUS_LABELS = {
    "new": "Новое",
    "confirm": "На сборке",
    "open": "Открытая поставка",
    "awaiting_packaging": "Ожидает сборки",
    "awaiting_deliver": "Ожидает отгрузки",
    "NEW": "Новый",
    "ORDER_PLACED": "Заказ оформлен",
    "WAIT_FOR_CONFIRMATION": "Ожидает подтверждения",
    "WAIT_FOR_DELIVERY": "Ожидает доставки",
}


class Widget(Contract):
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    kind: str = Field(pattern=r"^(assembly|shipping|attention|codes|documents)$")
    channel: str = Field(default="all", pattern=r"^(all|wb|ozon|kit|chz)$")
    warehouse_id: str = Field(default="", max_length=120)
    status: str = Field(default="", max_length=100)


class Layout(Contract):
    widgets: list[Widget] = Field(min_length=0, max_length=12)


class ApplicationPreferences(Contract):
    operator: str = Field(default="", max_length=120)
    refresh_seconds: int = Field(default=15, ge=5, le=300)
    density: str = Field(default="compact", pattern=r"^(compact|comfortable)$")


class PrintPreferences(Contract):
    width_mm: float = Field(default=50, ge=15, le=210)
    height_mm: float = Field(default=30, ge=10, le=297)


class Organization(Contract):
    name: str = Field(min_length=1, max_length=120)
    legal_name: str = Field(default="", max_length=240)
    tin: str = Field(default="", pattern=r"^([0-9]{10}|[0-9]{12})?$")


UI_MODELS = {
    "workspace.layout": Layout,
    "application.preferences": ApplicationPreferences,
    "printing.preferences": PrintPreferences,
}


def validate_ui_setting(conn, seller, key, value):
    if key not in UI_MODELS:
        return value
    try:
        result = UI_MODELS[key].model_validate(value).model_dump()
    except ValueError as exc:
        raise InvalidInput("Проверьте параметры интерфейса") from exc
    if key == "workspace.layout":
        ids = [v["id"] for v in result["widgets"]]
        if len(ids) != len(set(ids)):
            raise InvalidInput("Виджеты должны иметь разные идентификаторы")
        for item in result["widgets"]:
            allowed = {
                "codes": {"all", "chz"},
                "documents": {"all"},
                "assembly": {"all", "wb", "ozon", "kit"},
                "shipping": {"all", "wb", "ozon", "kit"},
            }.get(item["kind"])
            if allowed and item["channel"] not in allowed:
                raise InvalidInput("Этот канал не подходит для выбранного виджета")
            if item["warehouse_id"]:
                warehouse = conn.execute(
                    "SELECT c.adapter_key FROM warehouses w JOIN connections c "
                    "ON c.seller_id=w.seller_id AND c.id=w.connection_id "
                    "WHERE w.seller_id=? AND w.id=?",
                    (seller, item["warehouse_id"]),
                ).fetchone()
                if not warehouse or item["channel"] not in {"all", warehouse[0]}:
                    raise InvalidInput("Склад не принадлежит организации или выбранному каналу")
            if item["kind"] not in {"assembly", "shipping"} and (
                item["warehouse_id"] or item["status"]
            ):
                raise InvalidInput("Склад и статус доступны для виджетов заказов")
    return result


def sales_predicate(adapter, kind, stage="", status="", warehouse=""):
    clauses, args = [], []
    if stage:
        if stage not in {"assembly", "shipping"}:
            raise InvalidInput("Неизвестный этап заказов")
        if adapter == "wb" and kind == "supplies" and stage == "shipping":
            clauses.append("status='open'")
        elif kind == "orders":
            states = ORDER_STAGES.get(adapter, {}).get(stage, ())
            clauses.append("status IN (" + ",".join("?" for _ in states) + ")" if states else "0")
            args.extend(states)
            if adapter == "wb":
                clauses.append("json_extract(attributes_json,'$.source.deliveryType')='fbs'")
        else:
            raise InvalidInput("Этот фильтр доступен для заказов и поставок WB")
    if status:
        if kind not in {"orders", "supplies"}:
            raise InvalidInput("Статус доступен для заказов и отгрузок")
        clauses.append("status=?")
        args.append(status)
    if warehouse:
        if kind not in {"orders", "supplies"}:
            raise InvalidInput("Склад доступен для заказов и отгрузок")
        clauses.append("warehouse_external_id=?")
        args.append(warehouse)
    return "".join(" AND " + c for c in clauses), args


class Workspace:
    def __init__(self, database):
        self.db = database

    def preferences(self, conn, seller):
        values = {
            r["key"]: json.loads(r["value_json"])
            for r in conn.execute("SELECT * FROM settings WHERE seller_id=?", (seller,))
        }
        return values

    def organization(self, seller, body):
        value = Organization.model_validate(body).model_dump()
        value["name"] = value["name"].strip()
        if not value["name"]:
            raise InvalidInput("Укажите название организации")
        with self.db.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            require_seller(conn, seller)
            conn.execute("UPDATE sellers SET name=? WHERE id=?", (value["name"], seller))
            conn.execute(
                "INSERT INTO settings VALUES(?, 'organization.profile', ?) "
                "ON CONFLICT(seller_id,key) DO UPDATE SET value_json=excluded.value_json",
                (seller, json.dumps(value, ensure_ascii=False)),
            )
        return value

    def dashboard(self, seller):
        with self.db.connection() as conn:
            conn.execute("BEGIN")
            require_seller(conn, seller)
            settings = self.preferences(conn, seller)
            layout = settings.get(
                "workspace.layout",
                {
                    "widgets": [
                        {"id": key, "kind": key} for key in ("assembly", "shipping", "attention")
                    ]
                },
            )
            layout = Layout.model_validate(layout).model_dump()
            connections = [
                dict(r)
                for r in conn.execute(
                    "SELECT id,name,adapter_key FROM connections WHERE seller_id=? "
                    "ORDER BY name,id",
                    (seller,),
                )
            ]
            warehouses = [
                dict(r)
                for r in conn.execute(
                    "SELECT w.id,w.name,w.connection_id,w.external_id,c.adapter_key "
                    "FROM warehouses w JOIN connections c ON c.seller_id=w.seller_id "
                    "AND c.id=w.connection_id WHERE w.seller_id=? ORDER BY w.name,w.id",
                    (seller,),
                )
            ]
            widgets = []
            for item in layout["widgets"]:
                selected = [c for c in connections if item["channel"] in {"all", c["adapter_key"]}]
                warehouse = next((w for w in warehouses if w["id"] == item["warehouse_id"]), None)
                if item["warehouse_id"] and not warehouse:
                    widgets.append(
                        {
                            **item,
                            "title": WIDGETS[item["kind"]],
                            "rows": [],
                            "message": "Склад больше не доступен. Измените настройку виджета.",
                        }
                    )
                    continue
                if warehouse:
                    selected = [c for c in selected if c["id"] == warehouse["connection_id"]]
                rows = (
                    self.order_rows(conn, seller, selected, item, warehouse)
                    if item["kind"] in {"assembly", "shipping"}
                    else self.attention_rows(conn, seller, selected, item)
                )
                widgets.append({**item, "title": WIDGETS[item["kind"]], "rows": rows})
            return {
                "layout": layout,
                "widgets": widgets,
                "connections": connections,
                "warehouses": warehouses,
                "available_widgets": WIDGETS,
                "stage_statuses": {
                    stage: {
                        key: [
                            {"value": status, "label": STATUS_LABELS[status]}
                            for status in (
                                ("open",) if key == "wb" and stage == "shipping" else stages[stage]
                            )
                        ]
                        for key, stages in ORDER_STAGES.items()
                    }
                    for stage in ("assembly", "shipping")
                },
                "generated_at": datetime.now(UTC).isoformat(),
            }

    def order_rows(self, conn, seller, connections, widget, warehouse):
        rows = []
        for connection in connections:
            key, cid = connection["adapter_key"], connection["id"]
            if key not in ORDER_STAGES:
                continue
            kind = "supplies" if key == "wb" and widget["kind"] == "shipping" else "orders"
            clause, args = sales_predicate(
                key,
                kind,
                widget["kind"],
                widget["status"],
                warehouse["external_id"] if warehouse else "",
            )
            where = "seller_id=? AND connection_id=?" + clause
            count = conn.execute(
                f"SELECT count(*) FROM {kind} WHERE {where}", (seller, cid, *args)
            ).fetchone()[0]
            items = [
                decode_record(r)
                for r in conn.execute(
                    f"SELECT * FROM {kind} WHERE {where} ORDER BY updated_at DESC,id LIMIT 4",
                    (seller, cid, *args),
                )
            ]
            table = "wb_snapshots" if key == "wb" else "commerce_snapshots"
            snapshots = {
                r["key"]: r
                for r in conn.execute(
                    f"SELECT * FROM {table} WHERE seller_id=? AND connection_id=? "
                    "AND key IN ('last_sync','sync')",
                    (seller, cid),
                )
            }
            last, sync = snapshots.get("last_sync"), snapshots.get("sync")
            sync_data = json.loads(sync["value_json"]) if sync else {}
            state = sync_data.get("state", "never")
            freshness = (
                "error"
                if state in {"failed", "interrupted"}
                else "updating"
                if state in {"queued", "running"}
                else "complete"
                if last
                else "never"
            )
            if last and freshness == "complete":
                then = datetime.fromisoformat(last["updated_at"].replace("Z", "+00:00"))
                if datetime.now(UTC) - then > timedelta(minutes=15):
                    freshness = "stale"
            query = {
                "channel": key,
                "connection": cid,
                "kind": kind,
                "stage": widget["kind"],
                "search": "",
                "offset": "0",
            }
            if widget["status"]:
                query["status"] = widget["status"]
            if warehouse:
                query["warehouse"] = warehouse["external_id"]
            rows.append(
                {
                    "connection_id": cid,
                    "channel": key,
                    "name": connection["name"],
                    "count": count if last or items else None,
                    "freshness": freshness,
                    "updated_at": last["updated_at"] if last else None,
                    "href": f"/sellers/{seller}/sales?" + urlencode(query),
                    "hint": "Открытые поставки WB" if kind == "supplies" else "Заказы",
                    "examples": [self.order_example(v) for v in items],
                }
            )
        return rows

    @staticmethod
    def order_example(item):
        source = item["attributes"].get("source", {})
        deadline = (
            source.get("shipment_date")
            or source.get("deliverBy")
            or source.get("delivery_date")
            or source.get("deliveryDate")
        )
        return {
            "number": str(source.get("order_number") or source.get("name") or item["external_id"]),
            "deadline": deadline if isinstance(deadline, str) else None,
        }

    def attention_rows(self, conn, seller, connections, widget):
        key = widget["kind"]
        if key == "codes":
            rows = []
            for c in connections:
                if c["adapter_key"] != "chz":
                    continue
                for r in conn.execute(
                    "SELECT m.gtin,m.external_status,count(*) AS count FROM marking_codes m "
                    "WHERE m.seller_id=? AND m.connection_id=? GROUP BY m.gtin,m.external_status "
                    "ORDER BY m.gtin LIMIT 8",
                    (seller, c["id"]),
                ):
                    rows.append(
                        {
                            "name": r["gtin"] + " · " + (r["external_status"] or "Не проверен"),
                            "count": r["count"],
                            "href": f"/sellers/{seller}/marking?"
                            + urlencode({"tab": "codes", "connection": c["id"]}),
                            "hint": "Сохраненные коды; резерв и владелец проверяются "
                            "при назначении",
                        }
                    )
            return rows
        if key == "documents":
            today = datetime.now(UTC).date()
            rows = []
            for r in conn.execute(
                "SELECT d.data_json,min(dp.product_id) AS product_id FROM catalog_documents d "
                "LEFT JOIN catalog_document_products dp ON dp.seller_id=d.seller_id "
                "AND dp.document_id=d.id WHERE d.seller_id=? AND "
                "json_extract(d.data_json,'$.expires_on')<=? "
                "GROUP BY d.id ORDER BY json_extract(d.data_json,'$.expires_on') LIMIT 8",
                (seller, (today + timedelta(days=30)).isoformat()),
            ):
                d = json.loads(r["data_json"])
                rows.append(
                    {
                        "name": d["number"],
                        "hint": "Действует до " + d["expires_on"],
                        "href": f"/sellers/{seller}/catalog?product={r['product_id']}"
                        if r["product_id"]
                        else f"/sellers/{seller}/catalog",
                    }
                )
            return rows
        ids = [c["id"] for c in connections]
        if not ids:
            return []
        marks = ",".join("?" for _ in ids)
        rows = []
        for c in connections:
            channel = c["adapter_key"]
            table = {
                "wb": "wb_actions",
                "ozon": "commerce_actions",
                "kit": "commerce_actions",
                "chz": "marking_documents",
            }.get(channel)
            if not table:
                continue
            states = (
                ("unknown", "partial", "rejected")
                if channel == "chz"
                else ("unknown", "conflict", "partial", "rejected", "awaiting_manual")
            )
            count = conn.execute(
                f"SELECT count(*) FROM {table} WHERE seller_id=? AND connection_id=? "
                "AND state IN (" + ",".join("?" for _ in states) + ")",
                (seller, c["id"], *states),
            ).fetchone()[0]
            if count:
                path = "marking" if channel == "chz" else "sales"
                query = (
                    {"connection": c["id"], "tab": "history"}
                    if channel == "chz"
                    else {"channel": channel, "connection": c["id"], "history": "1"}
                )
                rows.append(
                    {
                        "name": c["name"] + " · требует сверки",
                        "count": count,
                        "href": f"/sellers/{seller}/{path}?" + urlencode(query),
                        "hint": "Откройте журнал и сверку",
                    }
                )
        failed = conn.execute(
            f"SELECT count(*) FROM operations o WHERE seller_id=? AND connection_id IN ({marks}) "
            "AND status IN ('failed','interrupted') AND created_at>=? AND NOT EXISTS "
            "(SELECT 1 FROM operations newer WHERE newer.seller_id=o.seller_id "
            "AND newer.connection_id=o.connection_id AND newer.operation_key=o.operation_key "
            "AND newer.scope_key=o.scope_key AND (newer.created_at>o.created_at OR "
            "(newer.created_at=o.created_at AND newer.rowid>o.rowid)))",
            (seller, *ids, (datetime.now(UTC) - timedelta(days=7)).isoformat()),
        ).fetchone()[0]
        if failed:
            rows.append(
                {
                    "name": "Неудавшиеся фоновые задачи",
                    "count": failed,
                    "href": f"/sellers/{seller}/overview?activity=1",
                    "hint": "За последние 7 дней",
                }
            )
        missing = conn.execute(
            f"SELECT count(*) FROM products p WHERE p.seller_id=? AND p.connection_id IN ({marks}) "
            "AND NOT EXISTS (SELECT 1 FROM catalog_links l WHERE l.seller_id=p.seller_id "
            "AND l.source_product_id=p.id) AND p.connection_id IN "
            "(SELECT id FROM connections WHERE seller_id=? AND adapter_key IN ('wb','ozon','kit'))",
            (seller, *ids, seller),
        ).fetchone()[0]
        if missing:
            rows.append(
                {
                    "name": "Карточки без связи с ассортиментом",
                    "count": missing,
                    "href": f"/sellers/{seller}/catalog?tab=sources&unlinked=1",
                    "hint": "Проверьте сопоставление товаров",
                }
            )
        return rows

    def backup(self):
        directory = self.db.path.parent / "backups"
        directory.mkdir(exist_ok=True)
        name = datetime.now(UTC).strftime("flow-%Y%m%dT%H%M%S-") + uuid4().hex[:8] + ".sqlite3"
        path = directory / name
        with self.db.connection() as source, sqlite3.connect(path) as target:
            source.backup(target)
        return {"filename": name, "size_bytes": path.stat().st_size}

    def backup_path(self, name):
        if not re.fullmatch(r"flow-\d{8}T\d{6}-[a-f0-9]{8}\.sqlite3", name):
            raise InvalidInput("Неверное имя резервной копии")
        return self.db.path.parent / "backups" / name
