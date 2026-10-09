"""Order-centred packing data, dated batches and printable WB/GS1 labels."""

import base64
import json
from urllib.parse import quote, urlparse
from uuid import uuid4

from fbe_flow.core.errors import Conflict, FlowError, InvalidInput, NotFound
from fbe_flow.integrations.chz.formats import cis_from_code
from fbe_flow.modules.catalog import decode
from fbe_flow.modules.connections import connection_context
from fbe_flow.modules.records import Records


def linked_product(conn, seller, connection, order):
    source = order["attributes"].get("source", {})
    return conn.execute(
        "SELECT c.* FROM products p JOIN catalog_links l ON l.seller_id=p.seller_id "
        "AND l.source_product_id=p.id JOIN catalog_products c ON c.seller_id=l.seller_id "
        "AND c.id=l.product_id WHERE p.seller_id=? AND p.connection_id=? "
        "AND p.external_id=? AND l.variant=?",
        (seller, connection, str(source.get("nmId")), str(source.get("chrtId"))),
    ).fetchone()


def order_batch(conn, seller, connection, order, batch_id):
    product = linked_product(conn, seller, connection, order)
    if product is None or decode(product).get("archived"):
        raise InvalidInput("Свяжите размер WB с активным товаром ассортимента")
    row = conn.execute(
        "SELECT * FROM catalog_batches WHERE seller_id=? AND product_id=? AND id=?",
        (seller, product["id"], batch_id),
    ).fetchone()
    if row is None or not decode(row).get("expires_on"):
        raise InvalidInput("Выберите партию этого товара с датой окончания срока годности")
    return decode(row)


def enrich_orders(conn, seller, connection, items):
    for item in items:
        source = item["attributes"].get("source", {})
        row = linked_product(conn, seller, connection, item)
        product = decode(row) if row else None
        remote = conn.execute(
            "SELECT title,attributes_json FROM products WHERE seller_id=? AND connection_id=? "
            "AND external_id=?",
            (seller, connection, str(source.get("nmId"))),
        ).fetchone()
        card = json.loads(remote["attributes_json"]).get("source", {}) if remote else {}
        photos = card.get("photos", [])
        variant = next(
            (v for v in card.get("sizes", []) if str(v.get("chrtID")) == str(source.get("chrtId"))),
            None,
        )
        image = next(
            (
                v.get("square") or v.get("c246x328") or v.get("big")
                for v in photos
                if isinstance(v, dict)
            ),
            None,
        )
        if image and (
            urlparse(image).scheme != "https"
            or not (urlparse(image).hostname or "").endswith((".wbbasket.ru", ".wbstatic.net"))
        ):
            image = None
        batches = [
            decode(v)
            for v in conn.execute(
                "SELECT * FROM catalog_batches WHERE seller_id=? AND product_id=? "
                "ORDER BY created_at DESC",
                (seller, product["id"] if product else ""),
            )
        ]
        action = conn.execute(
            "SELECT state,body_json FROM wb_actions WHERE seller_id=? AND connection_id=? "
            "AND kind='expiration' AND json_extract(body_json,'$.order_id')=? "
            "AND state!='cancelled' ORDER BY created_at DESC,id DESC LIMIT 1",
            (seller, connection, item["id"]),
        ).fetchone()
        expiry = {**json.loads(action["body_json"]), "state": action["state"]} if action else None
        for marking in item.get("marking", []):
            a = conn.execute(
                "SELECT state FROM wb_actions WHERE seller_id=? AND id=?",
                (seller, marking["action_id"]),
            ).fetchone()
            marking["state"] = a[0] if a else "unknown"
        printed = conn.execute(
            "SELECT DISTINCT j.kind FROM wb_print_jobs j,json_each(j.labels_json) l "
            "WHERE j.seller_id=? AND j.connection_id=? AND j.confirmed_at IS NOT NULL "
            "AND json_extract(l.value,'$.order_id')=?",
            (seller, connection, item["id"]),
        ).fetchall()
        item["packing"] = {
            "title": remote["title"] if remote else source.get("article", "Товар"),
            "card_loaded": remote is not None,
            "variant": variant,
            "image": image,
            "product_id": product["id"] if product else None,
            "quantity": product.get("package_quantity") if product else None,
            "batches": batches,
            "expiration": expiry,
            "printed": [v[0] for v in printed],
        }


def selected_orders(flow, seller, connection, ids):
    wb = flow._connection(seller, connection)
    if not 1 <= len(ids) <= 100 or len(set(ids)) != len(ids):
        raise InvalidInput("Выберите от 1 до 100 разных заданий")
    orders = [Records(flow.db).get(seller, "orders", v) for v in ids]
    if any(
        v["connection_id"] != connection
        or v["attributes"].get("source", {}).get("deliveryType") != "fbs"
        for v in orders
    ):
        raise InvalidInput("Выберите задания FBS одного аккаунта")
    return wb, orders


def bulk_expiration(flow, seller, connection, entries):
    selected_orders(flow, seller, connection, [v["order_id"] for v in entries])
    results = []
    for entry in entries:
        try:
            action = flow.prepare(seller, connection, "expiration", entry)
            flow.enqueue(seller, action["id"])
            results.append(
                {"order_id": entry["order_id"], "action_id": action["id"], "state": "queued"}
            )
        except FlowError as exc:
            results.append({"order_id": entry["order_id"], "state": "error", "error": str(exc)})
    return results


def bulk_codes(flow, seller, connection, ids):
    _, orders = selected_orders(flow, seller, connection, ids)
    results = []
    for order in orders:
        try:
            with flow.db.connection() as conn:
                row = linked_product(conn, seller, connection, order)
                if row is None or decode(row).get("archived"):
                    raise InvalidInput("Свяжите заказ с активным товаром ассортимента")
                quantity = decode(row)["package_quantity"]
                expiry = conn.execute(
                    "SELECT body_json FROM wb_actions WHERE seller_id=? AND connection_id=? "
                    "AND kind='expiration' AND state='confirmed' "
                    "AND json_extract(body_json,'$.order_id')=? "
                    "ORDER BY created_at DESC LIMIT 1",
                    (seller, connection, order["id"]),
                ).fetchone()
                batch_id = json.loads(expiry[0])["batch_id"] if expiry else None
                batch_codes = (
                    {
                        v[0]
                        for v in conn.execute(
                            "SELECT code_id FROM catalog_units WHERE seller_id=? AND batch_id=?",
                            (seller, batch_id),
                        )
                    }
                    if batch_id
                    else None
                )
            codes = flow.available_codes(seller, connection, order["id"], limit=200)["items"]
            codes = [v for v in codes if batch_codes is None or v["id"] in batch_codes]
            if quantity > len(codes):
                raise InvalidInput(
                    "Недостаточно свободных КМ подходящей партии; откройте выбор кодов"
                )
            action = flow.prepare(
                seller,
                connection,
                "sgtin",
                {"order_id": order["id"], "code_ids": [v["id"] for v in codes[:quantity]]},
            )
            flow.enqueue(seller, action["id"])
            results.append({"order_id": order["id"], "action_id": action["id"], "state": "queued"})
        except FlowError as exc:
            results.append({"order_id": order["id"], "state": "error", "error": str(exc)})
    return results


def datamatrix_svg(full):
    import zxingcpp as zxing

    raw = full.removeprefix("]d2")
    cis = cis_from_code(raw)
    tails = raw[len(cis) :].split("\x1d")[1:]
    if not tails or any(not v[:2].isdigit() or len(v) < 3 for v in tails):
        raise InvalidInput("Для печати нужен полный КМ с криптографической частью")
    fields = [("01", cis[2:16]), ("21", cis[18:]), *[(v[:2], v[2:]) for v in tails]]
    # Zint accepts GS1 element strings. Read the generated symbol back, including
    # FNC1 and every GS separator, before ever offering it to the operator.
    for left, right in [("[", "]"), ("(", ")")]:
        if any(left in value or right in value for _, value in fields):
            continue
        try:
            barcode = zxing.create_barcode(
                "".join(left + key + right + value for key, value in fields),
                zxing.BarcodeFormat.DataMatrix,
                gs1=True,
                force_square=True,
            )
            decoded = zxing.read_barcode(
                zxing.write_barcode_to_image(barcode, scale=4), text_mode=zxing.TextMode.Plain
            )
            if (
                decoded
                and decoded.symbology_identifier == "]d2"
                and decoded.bytes == raw.encode("ascii")
            ):
                return zxing.write_barcode_to_svg(barcode, scale=4)
        except (ValueError, UnicodeError):
            continue
    raise InvalidInput("Не удалось построить DataMatrix с точным содержимым КМ")


def png_label(value):
    try:
        data = base64.b64decode(value, validate=True)
    except (TypeError, ValueError) as exc:
        raise InvalidInput("WB вернул поврежденную этикетку") from exc
    if len(data) > 2_000_000 or not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise InvalidInput("WB не вернул PNG этикетки")
    return "data:image/png;base64," + base64.b64encode(data).decode()


def create_print_job(flow, seller, connection, kind, ids, supply_id=None):
    wb = flow._connection(seller, connection)
    adapter = flow.registry.get("wb")
    config = adapter.config(connection_context(wb))
    labels = []
    if kind == "supply":
        supply = Records(flow.db).get(seller, "supplies", supply_id)
        if supply["connection_id"] != connection:
            raise NotFound("Поставка не найдена")
        info = adapter.supply_info(config, supply["external_id"])
        if not info["done"]:
            raise Conflict("QR поставки доступен после перевода в доставку")
        value = adapter._call(
            config,
            "marketplace",
            "GET",
            "/api/v3/supplies/" + quote(supply["external_id"], safe="") + "/barcode",
            params={"type": "png"},
        )
        labels = [
            {
                "image": png_label(value.get("file")),
                "title": info.get("name") or supply["external_id"],
                "reference": supply["external_id"],
            }
        ]
    else:
        _, orders = selected_orders(flow, seller, connection, ids)
        if kind == "orders":
            remote_ids = [int(v["external_id"]) for v in orders]
            statuses = adapter.statuses(config, remote_ids)
            if any(v["supplierStatus"] not in {"confirm", "complete"} for v in statuses.values()):
                raise Conflict("Сначала добавьте выбранные задания в поставку")
            data = adapter._call(
                config,
                "marketplace",
                "POST",
                "/api/v3/orders/stickers",
                params={"type": "png", "width": 58, "height": 40},
                body={"orders": remote_ids},
            )
            stickers = data.get("stickers", [])
            if len(stickers) != len(orders) or {str(v.get("orderId")) for v in stickers} != {
                v["external_id"] for v in orders
            }:
                raise InvalidInput("WB вернул неполный комплект этикеток; печать остановлена")
            by_id = {str(v["orderId"]): v for v in stickers}
            labels = [
                {
                    "order_id": v["id"],
                    "reference": "#" + v["external_id"],
                    "title": v["attributes"]["source"].get("article", ""),
                    "image": png_label(by_id[v["external_id"]].get("file")),
                }
                for v in orders
            ]
        elif kind == "codes":
            with flow.db.connection() as conn:
                for order in orders:
                    codes = conn.execute(
                        "SELECT c.*,a.state FROM wb_code_assignments b JOIN marking_codes c "
                        "ON c.seller_id=b.seller_id AND c.id=b.code_id JOIN wb_actions a "
                        "ON a.seller_id=b.seller_id AND a.id=b.action_id WHERE b.seller_id=? "
                        "AND b.connection_id=? AND b.order_id=?",
                        (seller, connection, order["id"]),
                    ).fetchall()
                    if not codes or any(c["state"] != "confirmed" for c in codes):
                        raise Conflict(
                            "Дождитесь подтверждения КИЗов в WB для всех выбранных заданий"
                        )
                    for code in codes:
                        svg = datamatrix_svg(code["full_code"])
                        labels.append(
                            {
                                "order_id": order["id"],
                                "code_id": code["id"],
                                "reference": "#" + order["external_id"],
                                "title": order["attributes"]["source"].get("article", ""),
                                "code": code["code"],
                                "image": "data:image/svg+xml;base64,"
                                + base64.b64encode(svg.encode()).decode(),
                            }
                        )
        else:
            raise InvalidInput("Неизвестный вид этикеток")
    identifier = str(uuid4())
    with flow.db.connection() as conn:
        conn.execute(
            "INSERT INTO wb_print_jobs(id,seller_id,connection_id,kind,labels_json) "
            "VALUES(?,?,?,?,?)",
            (identifier, seller, connection, kind, json.dumps(labels, ensure_ascii=False)),
        )
    return {"id": identifier, "count": len(labels)}


def print_job(flow, seller, job_id):
    with flow.db.connection() as conn:
        row = conn.execute(
            "SELECT * FROM wb_print_jobs WHERE seller_id=? AND id=?", (seller, job_id)
        ).fetchone()
    if row is None:
        raise NotFound("Задание печати не найдено")
    return {**dict(row), "labels": json.loads(row["labels_json"])}
