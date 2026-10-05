"""Seller-scoped canonical assortment and atomic local reviewable changes."""

import base64
import hashlib
import json
import operator
import sqlite3
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from uuid import uuid4

from pydantic import ValidationError

from fbe_flow.core.catalog_models import (
    CatalogBatch,
    CatalogDocument,
    CatalogProduct,
    ClassificationRule,
    normalize_gtin,
)
from fbe_flow.core.errors import Conflict, InvalidInput, NotFound
from fbe_flow.core.models import NormalizedBatch
from fbe_flow.modules.connections import connection_context, require_connection
from fbe_flow.modules.records import Records, decode_record
from fbe_flow.modules.sellers import require_seller


def encode(value):
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    )


def digest(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()


def validate(model, value):
    try:
        return model.model_validate(value).model_dump(mode="json")
    except ValidationError as exc:
        messages = [f"{'.'.join(map(str, v['loc']))}: {v['msg']}" for v in exc.errors()]
        raise InvalidInput("; ".join(messages)) from exc


def decode(row):
    if row is None:
        raise NotFound("Запись ассортимента не найдена")
    value = dict(row)
    data = json.loads(value.pop("data_json"))
    return {**data, **value}


def require_product(conn, seller, product_id):
    return decode(
        conn.execute(
            "SELECT * FROM catalog_products WHERE seller_id=? AND id=?", (seller, product_id)
        ).fetchone()
    )


def clean_product(value):
    return {k: value[k] for k in CatalogProduct.model_fields if k in value}


def check_source_binding(conn, seller, source_id, variant, gtin, group):
    row = conn.execute(
        "SELECT p.* FROM catalog_links l JOIN catalog_products p ON p.seller_id=l.seller_id "
        "AND p.id=l.product_id WHERE l.seller_id=? AND l.source_product_id=? AND l.variant=?",
        (seller, source_id, str(variant)),
    ).fetchone()
    if row:
        product = decode(row)
        if product["archived"] or gtin not in product["gtins"]:
            raise Conflict("Связь ЧЗ не соответствует GTIN активного товара ассортимента")
        if product["product_group"] and product["product_group"] != group:
            raise Conflict("Группа ЧЗ отличается от группы в ассортименте")


class Catalog:
    def __init__(self, database, connections, registry, marking):
        self.db, self.connections, self.registry, self.marking = (
            database,
            connections,
            registry,
            marking,
        )

    @staticmethod
    def event(conn, seller, product, kind, value, batch=None, code=None):
        conn.execute(
            "INSERT INTO catalog_events(id,seller_id,product_id,batch_id,code_id,kind,data_json)"
            " VALUES(?,?,?,?,?,?,?)",
            (str(uuid4()), seller, product, batch, code, kind, encode(value)),
        )

    @staticmethod
    def page(offset, limit, search=""):
        if offset < 0 or not 1 <= limit <= 200 or len(search) > 150:
            raise InvalidInput("Неверные параметры страницы")
        return "%" + search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"

    def list(self, seller, offset=0, limit=100, search="", archived=False):
        pattern = self.page(offset, limit, search)
        where = (
            "seller_id=? AND json_extract(data_json,'$.archived')=? AND "
            "(title LIKE ? ESCAPE '\\' OR sku LIKE ? ESCAPE '\\' OR "
            "data_json LIKE ? ESCAPE '\\')"
        )
        args = (seller, int(archived), pattern, pattern, pattern)
        with self.db.connection() as conn:
            require_seller(conn, seller)
            total = conn.execute(
                "SELECT count(*) FROM catalog_products WHERE " + where, args
            ).fetchone()[0]
            items = [
                decode(r)
                for r in conn.execute(
                    "SELECT * FROM catalog_products WHERE "
                    + where
                    + " ORDER BY title,id LIMIT ? OFFSET ?",
                    (*args, limit, offset),
                )
            ]
        return {"items": items, "total": total, "offset": offset, "limit": limit}

    def stats(self, seller):
        with self.db.connection() as conn:
            require_seller(conn, seller)
            row = conn.execute(
                "SELECT count(*) AS total,sum(json_extract(data_json,'$.archived')=0) AS active,"
                "sum(json_array_length(data_json,'$.gtins')=0) AS without_gtin,"
                "sum(json_extract(data_json,'$.tnved') IS NULL) AS without_tnved "
                "FROM catalog_products WHERE seller_id=?",
                (seller,),
            ).fetchone()
            result = {k: v or 0 for k, v in dict(row).items()}
            result["unlinked_sources"] = conn.execute(
                "SELECT count(*) FROM products p WHERE p.seller_id=? AND NOT EXISTS "
                "(SELECT 1 FROM catalog_links l WHERE l.seller_id=p.seller_id "
                "AND l.source_product_id=p.id)",
                (seller,),
            ).fetchone()[0]
            return result

    def save_product(self, seller, data, product_id=None, revision=None, origin="editor"):
        try:
            with self.db.connection() as conn:
                conn.execute("BEGIN IMMEDIATE")
                return self.save_product_in(conn, seller, data, product_id, revision, origin)
        except sqlite3.IntegrityError as exc:
            raise Conflict("Артикул или GTIN уже используется у этого продавца") from exc

    def save_product_in(
        self, conn, seller, data, product_id=None, revision=None, origin="editor", create_id=None
    ):
        require_seller(conn, seller)
        data = validate(CatalogProduct, data)
        before = require_product(conn, seller, product_id) if product_id else None
        if before:
            if type(revision) is not int or before["revision"] != revision:
                raise Conflict("Товар изменился. Обновите карточку перед сохранением")
            if before["gtins"] != data["gtins"] or before["product_group"] != data["product_group"]:
                if conn.execute(
                    "SELECT 1 FROM catalog_batches b JOIN catalog_units u "
                    "ON u.seller_id=b.seller_id AND u.batch_id=b.id "
                    "WHERE b.seller_id=? AND b.product_id=? LIMIT 1",
                    (seller, product_id),
                ).fetchone():
                    raise Conflict("У товара есть экземпляры с кодами; GTIN и группу менять нельзя")
                for link in conn.execute(
                    "SELECT source_product_id,variant FROM catalog_links "
                    "WHERE seller_id=? AND product_id=?",
                    (seller, product_id),
                ):
                    for table, key in (("wb_links", "chrt_id"), ("commerce_links", "variant")):
                        used = conn.execute(
                            f"SELECT gtin,product_group FROM {table} WHERE seller_id=? "
                            f"AND product_id=? AND {key}=?",
                            (seller, link["source_product_id"], link["variant"]),
                        ).fetchone()
                        if used and (
                            used["gtin"] not in data["gtins"]
                            or data["product_group"] not in {None, used["product_group"]}
                        ):
                            raise Conflict("Изменение противоречит действующей связи площадки с ЧЗ")
            if clean_product(before) == data:
                return before
            conn.execute(
                "UPDATE catalog_products SET sku=?,title=?,data_json=?,revision=revision+1,"
                "updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE seller_id=? AND id=?",
                (data["sku"], data["title"], encode(data), seller, product_id),
            )
            conn.execute(
                "DELETE FROM catalog_identifiers WHERE seller_id=? AND product_id=?",
                (seller, product_id),
            )
        else:
            product_id = create_id or str(uuid4())
            conn.execute(
                "INSERT INTO catalog_products(id,seller_id,sku,title,data_json) VALUES(?,?,?,?,?)",
                (product_id, seller, data["sku"], data["title"], encode(data)),
            )
        for kind, values in (("gtin", data["gtins"]), ("barcode", data["barcodes"])):
            conn.executemany(
                "INSERT INTO catalog_identifiers(seller_id,product_id,kind,value) VALUES(?,?,?,?)",
                [(seller, product_id, kind, v) for v in dict.fromkeys(values)],
            )
        self.event(
            conn,
            seller,
            product_id,
            "product.saved",
            {
                "origin": origin,
                "before": clean_product(before) if before else None,
                "after": data,
            },
        )
        return require_product(conn, seller, product_id)

    def product(self, seller, product_id):
        with self.db.connection() as conn:
            return require_product(conn, seller, product_id)

    def detail(self, seller, product_id):
        result = self.product(seller, product_id)
        result.update(
            links=self.links(seller, product_id),
            documents=self.documents(seller, product_id),
            batches=self.batches(seller, product_id),
        )
        with self.db.connection() as conn:
            result["events"] = [
                decode_record(r)
                for r in conn.execute(
                    "SELECT * FROM catalog_events WHERE seller_id=? AND product_id=? "
                    "ORDER BY created_at DESC,id DESC LIMIT 100",
                    (seller, product_id),
                )
            ]
            row = conn.execute(
                "SELECT * FROM catalog_checks WHERE seller_id=? AND product_id=? "
                "ORDER BY created_at DESC,id DESC LIMIT 1",
                (seller, product_id),
            ).fetchone()
            result["check"] = decode_record(row) if row else None
        if result["check"]:
            check = result["check"]
            connection = self.connections.get(seller, check["connection_id"])
            created = datetime.fromisoformat(check["created_at"].replace("Z", "+00:00"))
            check["stale"] = (
                check["product_revision"] != result["revision"]
                or check["rules_digest"] != digest(self.rules(seller))
                or datetime.now(UTC) - created > timedelta(hours=24)
                or check["value"].get("checked_on") != date.today().isoformat()
                or check["value"]["observed"].get("connection_digest")
                != digest(connection["config"])
            )
        return result

    def sources(self, seller, connection=None, offset=0, limit=100, search="", unlinked=False):
        pattern = self.page(offset, limit, search)
        where = (
            "p.seller_id=? AND (p.title LIKE ? ESCAPE '\\' OR "
            "p.external_id LIKE ? ESCAPE '\\' OR COALESCE(p.sku,'') LIKE ? ESCAPE '\\')"
        )
        args = [seller, pattern, pattern, pattern]
        if connection:
            where += " AND p.connection_id=?"
            args.append(connection)
        if unlinked:
            where += (
                " AND NOT EXISTS (SELECT 1 FROM catalog_links l WHERE l.seller_id=p.seller_id "
                "AND l.source_product_id=p.id)"
            )
        with self.db.connection() as conn:
            require_seller(conn, seller)
            if connection:
                require_connection(conn, seller, connection)
            total = conn.execute("SELECT count(*) FROM products p WHERE " + where, args).fetchone()[
                0
            ]
            items = [
                decode_record(r)
                for r in conn.execute(
                    "SELECT p.*,c.adapter_key,c.name AS connection_name FROM products p "
                    "JOIN connections c ON c.seller_id=p.seller_id AND c.id=p.connection_id WHERE "
                    + where
                    + " ORDER BY p.title,p.id LIMIT ? OFFSET ?",
                    (*args, limit, offset),
                )
            ]
            for item in items:
                item["links"] = [
                    dict(r)
                    for r in conn.execute(
                        "SELECT product_id,variant FROM catalog_links "
                        "WHERE seller_id=? AND source_product_id=?",
                        (seller, item["id"]),
                    )
                ]
                item["variants"] = self.variants(item, item["adapter_key"])
                suggestions = {}
                for variant in item["variants"]:
                    for values in variant.get("identifiers", {}).values():
                        for value in values:
                            try:
                                code = normalize_gtin(value)
                            except (ValueError, TypeError):
                                continue
                            for row in conn.execute(
                                "SELECT p.id,p.title,p.sku FROM catalog_identifiers i "
                                "JOIN catalog_products p ON p.seller_id=i.seller_id "
                                "AND p.id=i.product_id WHERE i.seller_id=? AND i.kind='gtin' "
                                "AND i.value=?",
                                (seller, code),
                            ):
                                suggestions[row["id"]] = dict(row)
                item["suggestions"] = list(suggestions.values())
        return {"items": items, "total": total, "offset": offset, "limit": limit}

    def variants(self, source, key):
        reader = getattr(self.registry.get(key), "catalog_variants", None)
        return (
            reader(source)
            if reader
            else [
                {
                    "key": "",
                    "label": source["title"],
                    "fields": {
                        "title": source["title"],
                        "sku": source["sku"] or source["external_id"],
                    },
                    "identifiers": source["identifiers"],
                }
            ]
        )

    def refresh_source(self, seller, source_id):
        source = Records(self.db).get(seller, "products", source_id)
        connection = self.connections.get(seller, source["connection_id"])
        reader = getattr(self.registry.get(connection["adapter_key"]), "catalog_details", None)
        if not reader:
            raise InvalidInput("Обновите эту карточку синхронизацией соответствующего модуля")
        product = reader(connection_context(connection), source)
        if product.external_id != source["external_id"]:
            raise Conflict("API вернул другой товар")
        with self.db.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            current = conn.execute(
                "SELECT updated_at FROM products WHERE seller_id=? AND id=?", (seller, source_id)
            ).fetchone()
            if not current or current["updated_at"] != source["updated_at"]:
                raise Conflict("Исходная карточка изменилась во время чтения")
            if require_connection(conn, seller, connection["id"])["config"] != connection["config"]:
                raise Conflict("Подключение изменилось во время чтения")
            Records(self.db).apply_in_transaction(
                conn, seller, connection["id"], NormalizedBatch(products=(product,))
            )
        return Records(self.db).get(seller, "products", source_id)

    def links(self, seller, product_id):
        with self.db.connection() as conn:
            require_product(conn, seller, product_id)
            return [
                decode_record(r)
                for r in conn.execute(
                    "SELECT l.*,p.title,p.external_id,p.connection_id,"
                    "p.category_json,c.adapter_key,"
                    "c.name AS connection_name FROM catalog_links l JOIN products p "
                    "ON p.seller_id=l.seller_id AND p.id=l.source_product_id JOIN connections c "
                    "ON c.seller_id=p.seller_id AND c.id=p.connection_id "
                    "WHERE l.seller_id=? AND l.product_id=?",
                    (seller, product_id),
                )
            ]

    def link_in(self, conn, seller, product_id, source_id, variant, overrides=None):
        product = require_product(conn, seller, product_id)
        row = conn.execute(
            "SELECT * FROM products WHERE seller_id=? AND id=?", (seller, source_id)
        ).fetchone()
        if not row:
            raise NotFound("Исходная карточка не найдена")
        source = decode_record(row)
        connection = require_connection(conn, seller, source["connection_id"])
        chosen = next(
            (v for v in self.variants(source, connection["adapter_key"]) if v["key"] == variant),
            None,
        )
        if not chosen:
            raise Conflict("Вариант отсутствует в актуальной исходной карточке")
        if product["archived"]:
            raise Conflict("Восстановите товар из архива перед связыванием")
        known = []
        for value in chosen.get("identifiers", {}).get("gtin", []):
            try:
                known.append(normalize_gtin(value))
            except (ValueError, TypeError):
                pass
        if known and not set(known).intersection(product["gtins"]):
            raise Conflict("GTIN исходной карточки отличается от GTIN товара")
        for table, key in (("wb_links", "chrt_id"), ("commerce_links", "variant")):
            bound = conn.execute(
                f"SELECT gtin,product_group FROM {table} WHERE seller_id=? "
                f"AND product_id=? AND {key}=?",
                (seller, source_id, variant),
            ).fetchone()
            if bound and (
                bound["gtin"] not in product["gtins"]
                or product["product_group"] not in {None, bound["product_group"]}
            ):
                raise Conflict("Привязка противоречит действующей связи площадки с ЧЗ")
        previous = conn.execute(
            "SELECT * FROM catalog_links WHERE seller_id=? AND source_product_id=? AND variant=?",
            (seller, source_id, variant),
        ).fetchone()
        overrides = overrides or {}
        if not isinstance(overrides, dict) or len(encode(overrides)) > 100000:
            raise InvalidInput("Слишком много данных площадки")
        if previous:
            if previous["product_id"] != product_id:
                raise Conflict("Исходный вариант уже связан с другим товаром")
            if json.loads(previous["overrides_json"]) == overrides:
                return dict(previous)
            link_id = previous["id"]
            conn.execute(
                "UPDATE catalog_links SET overrides_json=? WHERE seller_id=? AND id=?",
                (encode(overrides), seller, link_id),
            )
        else:
            link_id = str(uuid4())
            conn.execute(
                "INSERT INTO catalog_links(id,seller_id,product_id,source_product_id,variant,"
                "overrides_json) VALUES(?,?,?,?,?,?)",
                (link_id, seller, product_id, source_id, variant, encode(overrides)),
            )
        self.event(
            conn,
            seller,
            product_id,
            "source.linked",
            {
                "source_product_id": source_id,
                "variant": variant,
                "overrides": overrides,
            },
        )
        return {"id": link_id, "product_id": product_id}

    def link(self, seller, product_id, source_id, variant="", overrides=None):
        with self.db.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            return self.link_in(conn, seller, product_id, source_id, variant, overrides)

    def adopt(self, seller, data, source_id, variant):
        try:
            with self.db.connection() as conn:
                conn.execute("BEGIN IMMEDIATE")
                product = self.save_product_in(conn, seller, data, origin="source-review")
                self.link_in(conn, seller, product["id"], source_id, variant)
                return product
        except sqlite3.IntegrityError as exc:
            raise Conflict("Артикул или GTIN существует; выберите существующий товар") from exc

    def unlink(self, seller, product_id, link_id):
        with self.db.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            require_product(conn, seller, product_id)
            row = conn.execute(
                "SELECT * FROM catalog_links WHERE seller_id=? AND product_id=? AND id=?",
                (seller, product_id, link_id),
            ).fetchone()
            if not row:
                raise NotFound("Связь не найдена")
            for table, key in (("wb_links", "chrt_id"), ("commerce_links", "variant")):
                if conn.execute(
                    f"SELECT 1 FROM {table} WHERE seller_id=? AND product_id=? AND {key}=?",
                    (seller, row["source_product_id"], row["variant"]),
                ).fetchone():
                    raise Conflict("Связь используется модулем ЧЗ/отгрузок")
            conn.execute("DELETE FROM catalog_links WHERE seller_id=? AND id=?", (seller, link_id))
            self.event(conn, seller, product_id, "source.unlinked", {"link_id": link_id})
        return {"deleted": True}

    @staticmethod
    def documents_in(conn, seller, product_id=None):
        where, args = "seller_id=?", [seller]
        if product_id:
            where += (
                " AND id IN (SELECT document_id FROM catalog_document_products "
                "WHERE seller_id=? AND product_id=?)"
            )
            args.extend([seller, product_id])
        items = [
            decode(r)
            for r in conn.execute(
                "SELECT * FROM catalog_documents WHERE " + where + " ORDER BY created_at,id", args
            )
        ]
        files = {}
        for row in conn.execute(
            "SELECT id,document_id,filename,mime,digest,length(content) AS size "
            "FROM catalog_files WHERE seller_id=?",
            (seller,),
        ):
            files.setdefault(row["document_id"], []).append(dict(row))
        for item in items:
            item["files"] = files.get(item["id"], [])
            item["expired"] = bool(
                item["expires_on"] and item["expires_on"] < date.today().isoformat()
            )
        return items

    def documents(self, seller, product_id=None):
        with self.db.connection() as conn:
            require_seller(conn, seller)
            if product_id:
                require_product(conn, seller, product_id)
            return self.documents_in(conn, seller, product_id)

    def save_document_in(self, conn, seller, data, document_id=None, revision=None, create_id=None):
        data = validate(CatalogDocument, data)
        require_seller(conn, seller)
        for product in data["product_ids"]:
            require_product(conn, seller, product)
        before = None
        if document_id:
            before = decode(
                conn.execute(
                    "SELECT * FROM catalog_documents WHERE seller_id=? AND id=?",
                    (seller, document_id),
                ).fetchone()
            )
            if type(revision) is not int or before["revision"] != revision:
                raise Conflict("Документ изменился. Обновите данные")
            if all(before[k] == v for k, v in data.items()):
                return before
            conn.execute(
                "UPDATE catalog_documents SET data_json=?,revision=revision+1,"
                "updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE seller_id=? AND id=?",
                (encode(data), seller, document_id),
            )
            conn.execute(
                "DELETE FROM catalog_document_products WHERE seller_id=? AND document_id=?",
                (seller, document_id),
            )
        else:
            document_id = create_id or str(uuid4())
            conn.execute(
                "INSERT INTO catalog_documents(id,seller_id,data_json) VALUES(?,?,?)",
                (document_id, seller, encode(data)),
            )
        conn.executemany(
            "INSERT INTO catalog_document_products(seller_id,document_id,product_id) VALUES(?,?,?)",
            [(seller, document_id, p) for p in data["product_ids"]],
        )
        for product in set(data["product_ids"] + (before["product_ids"] if before else [])):
            self.event(
                conn,
                seller,
                product,
                "document.saved",
                {
                    "document_id": document_id,
                    "data": data,
                },
            )
        return decode(
            conn.execute(
                "SELECT * FROM catalog_documents WHERE seller_id=? AND id=?", (seller, document_id)
            ).fetchone()
        )

    def save_document(self, seller, data, document_id=None, revision=None):
        with self.db.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            return self.save_document_in(conn, seller, data, document_id, revision)

    def add_file(self, seller, document_id, filename, content):
        if not 1 <= len(content) <= 10 * 1024 * 1024 or not 1 <= len(filename) <= 200:
            raise InvalidInput("Файл: до 10 МБ; имя: до 200 символов")
        if any(c in filename for c in ("/", "\\", "\r", "\n", "\x00")):
            raise InvalidInput("Недопустимое имя файла")
        signatures = [
            (b"%PDF-", "application/pdf"),
            (b"\x89PNG\r\n\x1a\n", "image/png"),
            (b"\xff\xd8\xff", "image/jpeg"),
        ]
        mime = next((mime for prefix, mime in signatures if content.startswith(prefix)), None)
        if not mime:
            raise InvalidInput("Документ должен быть PDF, PNG или JPEG")
        file_id = str(uuid4())
        with self.db.connection() as conn:
            if not conn.execute(
                "SELECT 1 FROM catalog_documents WHERE seller_id=? AND id=?",
                (seller, document_id),
            ).fetchone():
                raise NotFound("Документ не найден")
            conn.execute(
                "INSERT INTO catalog_files(id,seller_id,document_id,filename,mime,digest,content)"
                " VALUES(?,?,?,?,?,?,?)",
                (
                    file_id,
                    seller,
                    document_id,
                    filename,
                    mime,
                    hashlib.sha256(content).hexdigest(),
                    content,
                ),
            )
        return {"id": file_id, "filename": filename, "size": len(content)}

    def file(self, seller, file_id):
        with self.db.connection() as conn:
            row = conn.execute(
                "SELECT * FROM catalog_files WHERE seller_id=? AND id=?", (seller, file_id)
            ).fetchone()
            if not row:
                raise NotFound("Файл не найден")
            return dict(row)

    def save_batch_in(self, conn, seller, data, batch_id=None, revision=None, create_id=None):
        data = validate(CatalogBatch, data)
        product = require_product(conn, seller, data["product_id"])
        if product["archived"]:
            raise Conflict("Товар находится в архиве")
        if batch_id:
            before = decode(
                conn.execute(
                    "SELECT * FROM catalog_batches WHERE seller_id=? AND id=?", (seller, batch_id)
                ).fetchone()
            )
            if (
                type(revision) is not int
                or before["revision"] != revision
                or before["product_id"] != data["product_id"]
            ):
                raise Conflict("Партия изменилась или относится к другому товару")
            count = conn.execute(
                "SELECT count(*) FROM catalog_units WHERE seller_id=? AND batch_id=?",
                (seller, batch_id),
            ).fetchone()[0]
            if count > data["quantity"]:
                raise Conflict("Количество партии меньше числа назначенных кодов")
            if all(before[k] == v for k, v in data.items()):
                return before
            conn.execute(
                "UPDATE catalog_batches SET data_json=?,revision=revision+1 "
                "WHERE seller_id=? AND id=?",
                (encode(data), seller, batch_id),
            )
        else:
            batch_id = create_id or str(uuid4())
            conn.execute(
                "INSERT INTO catalog_batches(id,seller_id,product_id,data_json) VALUES(?,?,?,?)",
                (batch_id, seller, data["product_id"], encode(data)),
            )
        self.event(
            conn,
            seller,
            data["product_id"],
            "batch.saved",
            {"batch_id": batch_id, "data": data},
            batch_id,
        )
        return decode(
            conn.execute(
                "SELECT * FROM catalog_batches WHERE seller_id=? AND id=?", (seller, batch_id)
            ).fetchone()
        )

    def save_batch(self, seller, data, batch_id=None, revision=None):
        with self.db.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            return self.save_batch_in(conn, seller, data, batch_id, revision)

    @staticmethod
    def batches_in(conn, seller, product_id=None):
        where, args = "seller_id=?", [seller]
        if product_id:
            where += " AND product_id=?"
            args.append(product_id)
        items = [
            decode(r)
            for r in conn.execute(
                "SELECT * FROM catalog_batches WHERE " + where + " ORDER BY created_at,id", args
            )
        ]
        counts = dict(
            conn.execute(
                "SELECT batch_id,count(*) FROM catalog_units WHERE seller_id=? GROUP BY batch_id",
                (seller,),
            ).fetchall()
        )
        events = {}
        for row in conn.execute(
            "SELECT batch_id,kind,count(DISTINCT code_id) AS count FROM catalog_events "
            "WHERE seller_id=? AND code_id IS NOT NULL GROUP BY batch_id,kind",
            (seller,),
        ):
            events.setdefault(row["batch_id"], {})[row["kind"]] = row["count"]
        for item in items:
            item.update(assigned=counts.get(item["id"], 0), local_events=events.get(item["id"], {}))
        return items

    def batches(self, seller, product_id=None):
        with self.db.connection() as conn:
            require_seller(conn, seller)
            if product_id:
                require_product(conn, seller, product_id)
            return self.batches_in(conn, seller, product_id)

    def assign_codes(self, seller, batch_id, code_ids):
        self.code_selection(code_ids)
        try:
            with self.db.connection() as conn:
                conn.execute("BEGIN IMMEDIATE")
                batch = decode(
                    conn.execute(
                        "SELECT * FROM catalog_batches WHERE seller_id=? AND id=?",
                        (seller, batch_id),
                    ).fetchone()
                )
                product = require_product(conn, seller, batch["product_id"])
                if product["archived"]:
                    raise Conflict("Товар находится в архиве")
                count = conn.execute(
                    "SELECT count(*) FROM catalog_units WHERE seller_id=? AND batch_id=?",
                    (seller, batch_id),
                ).fetchone()[0]
                if count + len(code_ids) > batch["quantity"]:
                    raise Conflict("В партии недостаточно неназначенных экземпляров")
                for code_id in code_ids:
                    code = conn.execute(
                        "SELECT * FROM marking_codes WHERE seller_id=? AND id=?", (seller, code_id)
                    ).fetchone()
                    if not code:
                        raise NotFound("Код не найден")
                    if code["gtin"] not in product["gtins"] or product["product_group"] not in {
                        None,
                        code["product_group"],
                    }:
                        raise Conflict("Код относится к другому товару или группе")
                    conn.execute(
                        "INSERT INTO catalog_units(seller_id,batch_id,code_id) VALUES(?,?,?)",
                        (seller, batch_id, code_id),
                    )
                    self.event(conn, seller, product["id"], "code.assigned", {}, batch_id, code_id)
        except sqlite3.IntegrityError as exc:
            raise Conflict("Код уже назначен экземпляру партии") from exc
        return {"assigned": len(code_ids)}

    @staticmethod
    def code_selection(code_ids):
        if (
            not isinstance(code_ids, list)
            or not 1 <= len(code_ids) <= 500
            or any(not isinstance(v, str) for v in code_ids)
            or len(set(code_ids)) != len(code_ids)
        ):
            raise InvalidInput("Выберите от 1 до 500 разных кодов")

    def batch_codes(self, seller, batch_id, offset=0, limit=100):
        self.page(offset, limit)
        with self.db.connection() as conn:
            batch = decode(
                conn.execute(
                    "SELECT * FROM catalog_batches WHERE seller_id=? AND id=?", (seller, batch_id)
                ).fetchone()
            )
            total = conn.execute(
                "SELECT count(*) FROM catalog_units WHERE seller_id=? AND batch_id=?",
                (seller, batch_id),
            ).fetchone()[0]
            items = [
                decode_record(r)
                for r in conn.execute(
                    "SELECT c.id,c.code,c.gtin,c.product_group,c.external_status,c.attributes_json "
                    "FROM catalog_units u JOIN marking_codes c ON c.seller_id=u.seller_id "
                    "AND c.id=u.code_id WHERE u.seller_id=? AND u.batch_id=? "
                    "ORDER BY c.id LIMIT ? OFFSET ?",
                    (seller, batch_id, limit, offset),
                )
            ]
            for item in items:
                item["local_events"] = [
                    decode_record(r)
                    for r in conn.execute(
                        "SELECT kind,created_at,data_json FROM catalog_events "
                        "WHERE seller_id=? AND batch_id=? AND code_id=? ORDER BY created_at,id",
                        (seller, batch_id, item["id"]),
                    )
                ]
            return {
                "batch": batch,
                "items": items,
                "total": total,
                "offset": offset,
                "limit": limit,
            }

    def available_codes(self, seller, batch_id, connection_id=None, offset=0, limit=100):
        self.page(offset, limit)
        with self.db.connection() as conn:
            batch = decode(
                conn.execute(
                    "SELECT * FROM catalog_batches WHERE seller_id=? AND id=?", (seller, batch_id)
                ).fetchone()
            )
            product = require_product(conn, seller, batch["product_id"])
            if (
                connection_id
                and require_connection(conn, seller, connection_id)["adapter_key"] != "chz"
            ):
                raise InvalidInput("Выберите подключение ЧЗ")
            where = (
                "c.seller_id=? AND c.gtin IN ("
                + ",".join("?" for _ in product["gtins"])
                + ") AND NOT EXISTS (SELECT 1 FROM catalog_units u "
                "WHERE u.seller_id=c.seller_id AND u.code_id=c.id)"
            )
            args = [seller, *product["gtins"]]
            if connection_id:
                where += " AND c.connection_id=?"
                args.append(connection_id)
            if product["product_group"]:
                where += " AND c.product_group=?"
                args.append(product["product_group"])
            total = conn.execute(
                "SELECT count(*) FROM marking_codes c WHERE " + where, args
            ).fetchone()[0]
            items = [
                dict(r)
                for r in conn.execute(
                    "SELECT c.id,c.code,c.gtin,c.connection_id,c.external_status "
                    "FROM marking_codes c WHERE " + where + " ORDER BY c.id LIMIT ? OFFSET ?",
                    (*args, limit, offset),
                )
            ]
            return {"items": items, "total": total, "offset": offset, "limit": limit}

    def record_code_event(self, seller, batch_id, code_ids, kind, actor, note=""):
        self.code_selection(code_ids)
        if (
            kind not in {"printed", "applied", "quality_checked"}
            or not actor.strip()
            or len(actor) > 240
            or len(note) > 2000
        ):
            raise InvalidInput("Укажите действие и ответственного оператора")
        with self.db.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            batch = decode(
                conn.execute(
                    "SELECT * FROM catalog_batches WHERE seller_id=? AND id=?", (seller, batch_id)
                ).fetchone()
            )
            for code_id in code_ids:
                if not conn.execute(
                    "SELECT 1 FROM catalog_units WHERE seller_id=? AND batch_id=? AND code_id=?",
                    (seller, batch_id, code_id),
                ).fetchone():
                    raise Conflict("Код не назначен этой партии")
                self.event(
                    conn,
                    seller,
                    batch["product_id"],
                    kind,
                    {"actor": actor.strip(), "note": note, "confirmation": "operator"},
                    batch_id,
                    code_id,
                )
        return {"recorded": len(code_ids), "external_status_changed": False}

    def rules(self, seller):
        with self.db.connection() as conn:
            require_seller(conn, seller)
            return [
                decode(r)
                for r in conn.execute(
                    "SELECT * FROM catalog_rules WHERE seller_id=? ORDER BY id",
                    (seller,),
                )
            ]

    def save_rule_in(self, conn, seller, data, rule_id=None, revision=None, create_id=None):
        data = validate(ClassificationRule, data)
        require_seller(conn, seller)
        if rule_id:
            old = decode(
                conn.execute(
                    "SELECT * FROM catalog_rules WHERE seller_id=? AND id=?", (seller, rule_id)
                ).fetchone()
            )
            if type(revision) is not int or old["revision"] != revision:
                raise Conflict("Правило изменилось. Обновите данные")
            if all(old[k] == v for k, v in data.items()):
                return old
            conn.execute(
                "UPDATE catalog_rules SET data_json=?,revision=revision+1 "
                "WHERE seller_id=? AND id=?",
                (encode(data), seller, rule_id),
            )
        else:
            rule_id = create_id or str(uuid4())
            conn.execute(
                "INSERT INTO catalog_rules(id,seller_id,data_json) VALUES(?,?,?)",
                (rule_id, seller, encode(data)),
            )
        return decode(
            conn.execute(
                "SELECT * FROM catalog_rules WHERE seller_id=? AND id=?", (seller, rule_id)
            ).fetchone()
        )

    def save_rule(self, seller, data, rule_id=None, revision=None):
        with self.db.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            return self.save_rule_in(conn, seller, data, rule_id, revision)

    def classify(self, seller, product, on_date=None):
        on_date = on_date or date.today().isoformat()
        try:
            date.fromisoformat(on_date)
        except (TypeError, ValueError) as exc:
            raise InvalidInput("Неверная дата классификации") from exc
        candidates, incomplete = [], []
        numeric = {
            "volume_ml",
            "net_weight_g",
            "gross_weight_g",
            "length_mm",
            "width_mm",
            "height_mm",
            "package_quantity",
            "shelf_life_days",
        }
        for rule in self.rules(seller):
            if (
                not rule["enabled"]
                or rule["valid_from"] > on_date
                or (rule["valid_until"] and rule["valid_until"] < on_date)
            ):
                continue
            if not product.get("tnved"):
                incomplete.append(rule["id"])
                continue
            if not any(product["tnved"].startswith(v) for v in rule["tnved_prefixes"]):
                continue
            if rule["okpd2_prefixes"]:
                if not product.get("okpd2"):
                    incomplete.append(rule["id"])
                    continue
                if not any(product["okpd2"].startswith(v) for v in rule["okpd2_prefixes"]):
                    continue
            match = True
            for condition in rule["conditions"]:
                key = condition["field"]
                value = (
                    product.get("attributes", {}).get(key[11:])
                    if key.startswith("attributes.")
                    else product.get(key)
                )
                if value is None:
                    incomplete.append(rule["id"])
                    match = False
                    break
                expected, op = condition["value"], condition["operator"]
                try:
                    if op not in {"eq", "ne"} or key in numeric:
                        value, expected = Decimal(str(value)), Decimal(str(expected))
                    result = {
                        "eq": operator.eq,
                        "ne": operator.ne,
                        "gt": operator.gt,
                        "ge": operator.ge,
                        "lt": operator.lt,
                        "le": operator.le,
                    }[op](value, expected)
                except (TypeError, ValueError, InvalidOperation):
                    incomplete.append(rule["id"])
                    result = False
                if not result:
                    match = False
                    break
            if match:
                candidates.append(rule)
        base = {"product_group": None, "marking_required": None}
        if incomplete:
            return {
                **base,
                "state": "incomplete",
                "rules": candidates,
                "missing_rule_inputs": sorted(set(incomplete)),
            }
        if not candidates:
            return {**base, "state": "unknown", "rules": []}
        priority = max(v["priority"] for v in candidates)
        selected = [v for v in candidates if v["priority"] == priority]
        outcomes = {(v["product_group"], v["marking_required"]) for v in selected}
        if len(outcomes) != 1:
            return {**base, "state": "ambiguous", "rules": selected}
        group, required = next(iter(outcomes))
        return {
            "state": "matched",
            "rules": selected,
            "product_group": group,
            "marking_required": required,
        }

    def refresh_schema(self, seller, connection_id, category):
        connection = self.connections.get(seller, connection_id)
        if not category or len(category) > 240:
            raise InvalidInput("Укажите идентификатор категории")
        method = getattr(self.registry.get(connection["adapter_key"]), "catalog_schema", None)
        if not method:
            raise InvalidInput("У этого адаптера нет справочника характеристик")
        value = method(connection_context(connection), category)
        if not isinstance(value, dict) or not isinstance(value.get("attributes"), list):
            raise InvalidInput("Адаптер вернул неверную схему")
        with self.db.connection() as conn:
            current = require_connection(conn, seller, connection_id)
            if current["config"] != connection["config"]:
                raise Conflict("Подключение изменилось во время чтения")
            conn.execute(
                "INSERT INTO catalog_schemas(seller_id,connection_id,category_key,value_json)"
                " VALUES(?,?,?,?) ON CONFLICT(seller_id,connection_id,category_key) DO UPDATE "
                "SET value_json=excluded.value_json,"
                "updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')",
                (seller, connection_id, category, encode(value)),
            )
        return value

    def schemas(self, seller):
        with self.db.connection() as conn:
            require_seller(conn, seller)
            return [
                decode_record(r)
                for r in conn.execute(
                    "SELECT * FROM catalog_schemas WHERE seller_id=? "
                    "ORDER BY connection_id,category_key",
                    (seller,),
                )
            ]

    def dictionary(self, seller, connection_id, category, attribute_id, cursor=0):
        connection = self.connections.get(seller, connection_id)
        method = getattr(self.registry.get(connection["adapter_key"]), "catalog_dictionary", None)
        if (
            not method
            or type(attribute_id) is not int
            or attribute_id <= 0
            or type(cursor) is not int
            or cursor < 0
        ):
            raise InvalidInput("Укажите характеристику со словарём")
        return method(connection_context(connection), category, attribute_id, cursor)

    def check(self, seller, product_id, connection_id):
        product = self.product(seller, product_id)
        connection = self.marking._connection(seller, connection_id)
        method = getattr(self.registry.get("chz"), "catalog_check", None)
        if not method:
            raise InvalidInput("Адаптер ЧЗ не поддерживает сверку ассортимента")
        rules = self.rules(seller)
        checked_on = date.today().isoformat()
        classification = self.classify(seller, product, checked_on)
        observed = method(connection_context(connection), clean_product(product))
        observed["connection_digest"] = digest(connection["config"])
        issues = list(observed.get("issues", []))
        registry_groups = observed.get("tnved_groups", [])
        expected = (
            classification["product_group"]
            or product["product_group"]
            or (registry_groups[0] if len(registry_groups) == 1 else None)
        )
        if classification["state"] != "matched":
            issues.append(
                {
                    "code": "classification_" + classification["state"],
                    "message": "Правила не дали однозначной классификации",
                }
            )
        if (
            classification["product_group"]
            and product["product_group"]
            and classification["product_group"] != product["product_group"]
        ):
            issues.append(
                {
                    "code": "declared_group_mismatch",
                    "message": "Группа товара отличается от результата правила",
                }
            )
        if not expected:
            issues.append({"code": "group_missing", "message": "Не определена группа ЧЗ"})
        elif expected not in observed.get("account_groups", []):
            issues.append(
                {
                    "code": "group_not_connected",
                    "message": "Товарная группа не подключена к аккаунту ЧЗ",
                }
            )
        if expected and registry_groups and expected not in registry_groups:
            issues.append(
                {
                    "code": "tnved_group_mismatch",
                    "message": "Группа отличается от справочника ТН ВЭД ЧЗ",
                }
            )
        for card in observed.get("cards", []):
            if expected and card["product_groups"] and expected not in card["product_groups"]:
                issues.append(
                    {
                        "code": "nk_group_mismatch",
                        "gtin": card["gtin"],
                        "message": "Группа карточки НК отличается от ожидаемой",
                    }
                )
            for field in ("tnved", "okpd2"):
                if product[field] and card.get(field) and product[field] not in card[field]:
                    issues.append(
                        {
                            "code": "nk_" + field + "_mismatch",
                            "gtin": card["gtin"],
                            "message": "Поле " + field + " отличается в Нацкаталоге",
                        }
                    )
        result = {
            "state": "matched" if not issues else "needs_review",
            "checked_on": checked_on,
            "classification": classification,
            "observed": observed,
            "issues": issues,
            "suggested_group": expected,
            "automatic_changes": False,
        }
        check_id = str(uuid4())
        with self.db.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            current = require_product(conn, seller, product_id)
            config = require_connection(conn, seller, connection_id)["config"]
            if (
                current["revision"] != product["revision"]
                or digest(self.rules(seller)) != digest(rules)
                or digest(config) != observed["connection_digest"]
            ):
                raise Conflict("Данные изменились во время сверки. Повторите чтение")
            conn.execute(
                "INSERT INTO catalog_checks(id,seller_id,product_id,connection_id,"
                "product_revision,rules_digest,value_json) VALUES(?,?,?,?,?,?,?)",
                (
                    check_id,
                    seller,
                    product_id,
                    connection_id,
                    product["revision"],
                    digest(rules),
                    encode(result),
                ),
            )
            self.event(
                conn,
                seller,
                product_id,
                "classification.checked",
                {"check_id": check_id, "state": result["state"]},
            )
        return {"id": check_id, **result}

    def export_data(self, seller):
        with self.db.connection() as conn:
            conn.execute("BEGIN")
            require_seller(conn, seller)
            return {
                "products": [
                    decode(r)
                    for r in conn.execute(
                        "SELECT * FROM catalog_products WHERE seller_id=? ORDER BY sku,id",
                        (seller,),
                    )
                ],
                "documents": self.documents_in(conn, seller),
                "batches": self.batches_in(conn, seller),
                "links": [
                    decode_record(r)
                    for r in conn.execute(
                        "SELECT * FROM catalog_links WHERE seller_id=? ORDER BY id", (seller,)
                    )
                ],
                "rules": [
                    decode(r)
                    for r in conn.execute(
                        "SELECT * FROM catalog_rules WHERE seller_id=? ORDER BY id", (seller,)
                    )
                ],
            }

    def prepare_import(self, seller, operations, errors, warnings, source_digest):
        import_id = str(uuid4())
        plan = {"operations": operations, "errors": errors, "warnings": warnings}
        with self.db.connection() as conn:
            require_seller(conn, seller)
            conn.execute(
                "INSERT INTO catalog_imports(id,seller_id,digest,plan_json) VALUES(?,?,?,?)",
                (import_id, seller, source_digest, encode(plan)),
            )
        return {"id": import_id, "digest": source_digest, "state": "prepared", **plan}

    def import_plan(self, seller, import_id):
        with self.db.connection() as conn:
            row = conn.execute(
                "SELECT * FROM catalog_imports WHERE seller_id=? AND id=?", (seller, import_id)
            ).fetchone()
            if not row:
                raise NotFound("Предпросмотр импорта не найден")
            return {
                "id": row["id"],
                "state": row["state"],
                "digest": row["digest"],
                **json.loads(row["plan_json"]),
            }

    def run_import_operation(self, conn, seller, operation):
        kind, data = operation["kind"], operation["data"]
        entity_id, revision = operation.get("id"), operation.get("revision")
        new_id = entity_id if operation.get("new") else None
        if new_id:
            entity_id = None
        if kind == "product":
            return self.save_product_in(conn, seller, data, entity_id, revision, "xlsx", new_id)
        if kind == "document":
            return self.save_document_in(conn, seller, data, entity_id, revision, new_id)
        if kind == "batch":
            return self.save_batch_in(conn, seller, data, entity_id, revision, new_id)
        if kind == "rule":
            return self.save_rule_in(conn, seller, data, entity_id, revision, new_id)
        if kind == "link":
            row = conn.execute(
                "SELECT * FROM catalog_links WHERE seller_id=? AND source_product_id=? "
                "AND variant=?",
                (seller, data["source_product_id"], data["variant"]),
            ).fetchone()
            if "before_link" in operation and operation["before_link"] != (
                decode_record(row) if row else None
            ):
                raise Conflict("Связь изменилась после предпросмотра")
            return self.link_in(
                conn,
                seller,
                data["product_id"],
                data["source_product_id"],
                data["variant"],
                data.get("overrides"),
            )
        raise InvalidInput("Неизвестное действие импорта")

    def validate_import_operations(self, seller, operations):
        errors = []
        with self.db.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("SAVEPOINT preview")
            try:
                for operation in operations:
                    conn.execute("SAVEPOINT row_preview")
                    try:
                        self.run_import_operation(conn, seller, operation)
                        conn.execute("RELEASE row_preview")
                    except (InvalidInput, Conflict, NotFound, sqlite3.IntegrityError) as exc:
                        conn.execute("ROLLBACK TO row_preview")
                        conn.execute("RELEASE row_preview")
                        errors.append(
                            {
                                "sheet": operation["sheet"],
                                "row": operation["row"],
                                "column": None,
                                "message": str(exc)
                                if not isinstance(exc, sqlite3.IntegrityError)
                                else "Конфликт GTIN/артикула",
                            }
                        )
            finally:
                conn.execute("ROLLBACK TO preview")
                conn.execute("RELEASE preview")
        return errors

    def apply_import(self, seller, import_id, expected_digest):
        try:
            with self.db.connection() as conn:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    "SELECT * FROM catalog_imports WHERE seller_id=? AND id=?", (seller, import_id)
                ).fetchone()
                if not row:
                    raise NotFound("Предпросмотр импорта не найден")
                if row["digest"] != expected_digest or row["state"] != "prepared":
                    raise Conflict("Импорт уже обработан или его содержимое изменилось")
                plan = json.loads(row["plan_json"])
                if plan["errors"]:
                    raise Conflict("Исправьте ошибки файла и создайте новый предпросмотр")
                counts = {}
                for operation in plan["operations"]:
                    self.run_import_operation(conn, seller, operation)
                    kind = operation["kind"]
                    counts[kind] = counts.get(kind, 0) + 1
                conn.execute(
                    "UPDATE catalog_imports SET state='applied',"
                    "applied_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE seller_id=? AND id=?",
                    (seller, import_id),
                )
                return {"state": "applied", "counts": counts}
        except sqlite3.IntegrityError as exc:
            raise Conflict("Конфликт артикула, GTIN или связи. Весь импорт отменён") from exc

    def cancel_import(self, seller, import_id):
        with self.db.connection() as conn:
            changed = conn.execute(
                "UPDATE catalog_imports SET state='cancelled' "
                "WHERE seller_id=? AND id=? AND state='prepared'",
                (seller, import_id),
            )
            if not changed.rowcount:
                raise Conflict("Импорт обработан или не найден")
        return {"state": "cancelled"}


def decode_upload(value, maximum=16 * 1024 * 1024):
    if not isinstance(value, str) or len(value) > maximum * 4 // 3 + 8:
        raise InvalidInput("Размер файла превышает допустимый")
    try:
        result = base64.b64decode(value, validate=True)
    except (ValueError, TypeError) as exc:
        raise InvalidInput("Неверные данные файла") from exc
    if not result or len(result) > maximum:
        raise InvalidInput("Неверный размер файла")
    return result
