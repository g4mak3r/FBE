"""Seller-scoped journal: adapters own protocols, this module owns submission recovery."""

import hashlib
import json
import re
import sqlite3
from uuid import uuid4

from fbe_flow.core.errors import Conflict, InvalidInput, NotFound
from fbe_flow.core.models import OperationResult
from fbe_flow.modules.connections import connection_context
from fbe_flow.modules.records import Records


def canonical(value):
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    )


def checksum(body):
    intent = {k: v for k, v in body.items() if k not in {"preview", "preflight"}}
    return hashlib.sha256(canonical(intent).encode("utf-8")).hexdigest()


def retryable(row):
    return (
        row["state"] == "rejected"
        and not row["external_id"]
        and row["error_code"]
        in {
            "http_400",
            "http_401",
            "http_403",
            "http_404",
            "http_405",
            "http_413",
            "http_415",
            "http_422",
            "http_429",
            "upstream_api_error",
        }
    )


def safe_error_code(exc):
    value = getattr(exc, "code", "send_failed")
    return (
        value
        if isinstance(value, str) and re.fullmatch(r"[a-z0-9_]{1,80}", value)
        else "send_failed"
    )


class SubmissionJournal:
    def _selected_ids(self, seller, connection, ids, maximum=500):
        if (
            not isinstance(ids, list)
            or not 1 <= len(ids) <= maximum
            or any(not isinstance(v, str) for v in ids)
            or len(set(ids)) != len(ids)
        ):
            raise InvalidInput(f"Выберите от 1 до {maximum} разных карточек")
        with self.db.connection() as conn:
            values = []
            for product_id in ids:
                row = conn.execute(
                    "SELECT p.external_id,p.attributes_json,n.present,n.detail_available FROM "
                    "products p "
                    "JOIN nk_cards n ON n.seller_id=p.seller_id AND "
                    "n.connection_id=p.connection_id "
                    "AND n.external_id=p.external_id WHERE p.seller_id=? AND p.connection_id=? "
                    "AND p.id=?",
                    (seller, connection, product_id),
                ).fetchone()
                if row is None:
                    raise NotFound("Карточка не найдена в этом подключении")
                if (
                    not row["present"]
                    or not row["detail_available"]
                    or not json.loads(row["attributes_json"]).get("own_card")
                ):
                    raise InvalidInput("Выбрана отсутствующая или недоступная собственная карточка")
                values.append(int(row["external_id"]))
        return values

    def edit_model(self, seller, connection, ids):
        value = self._connection(seller, connection)
        adapter = self.registry.get("chz")
        return adapter.edit_model(
            adapter.config(connection_context(value)), self._selected_ids(seller, connection, ids)
        )

    def prepare(self, seller, connection, action, payload):
        value = self._connection(seller, connection)
        adapter = self.registry.get("chz")
        config = adapter.config(connection_context(value))
        if action in {"edit", "sign"}:
            payload = {
                **payload,
                "good_ids": self._selected_ids(
                    seller, connection, payload.get("product_ids"), 10 if action == "sign" else 500
                ),
            }
        if action == "edit":
            prepared = adapter.preview_edit(config, payload)
        elif action == "sign":
            prepared = adapter.prepare_signature(config, payload)
        elif action == "true":
            prepared = adapter.prepare_true(config, payload)
        elif action in {"suz_order", "suz_codes", "suz_utilisation", "suz_close"}:
            if action == "suz_order" and (
                not isinstance(payload.get("request_key"), str)
                or not 1 <= len(payload["request_key"].strip()) <= 120
            ):
                raise InvalidInput("Укажите уникальный номер задания для заказа КМ")
            prepared = adapter.prepare_suz(config, action, payload)
            if action == "suz_order":
                prepared["body"]["request_key"] = payload["request_key"].strip()
        else:
            raise InvalidInput("Неизвестный вид подготовки")
        document_id = str(uuid4())
        try:
            with self.db.connection() as conn:
                conn.execute(
                    "INSERT INTO "
                    "marking_documents(id,seller_id,connection_id,kind,title,body_json,digest) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (
                        document_id,
                        seller,
                        connection,
                        prepared["kind"],
                        prepared["title"],
                        canonical(prepared["body"]),
                        checksum(prepared["body"]),
                    ),
                )
                self._event(conn, seller, document_id, "prepared", {})
        except sqlite3.IntegrityError as exc:
            raise Conflict("Такой запрос уже есть в журнале; откройте его перед повтором") from exc
        return self.get_document(seller, document_id)

    def documents(self, seller, connection):
        self._connection(seller, connection)
        with self.db.connection() as conn:
            rows = [
                dict(r)
                for r in conn.execute(
                    "SELECT "
                    "id,seller_id,connection_id,kind,title,state,external_id,external_status,"
                    "error_code,created_at,updated_at "
                    "FROM marking_documents WHERE seller_id=? AND connection_id=? ORDER BY "
                    "created_at DESC,id LIMIT 200",
                    (seller, connection),
                )
            ]
        return [{**r, "retryable": retryable(r)} for r in rows]

    def get_document(self, seller, document_id, *, internal=False):
        with self.db.connection() as conn:
            row = conn.execute(
                "SELECT * FROM marking_documents WHERE seller_id=? AND id=?", (seller, document_id)
            ).fetchone()
            if row is None:
                raise NotFound("Документ не найден")
            value = dict(row)
            value["body"] = json.loads(value.pop("body_json"))
            result = value.pop("result_json")
            value["result"] = json.loads(result) if result else None
            wire = value.pop("wire_json")
            if internal:
                value["wire"] = json.loads(wire) if wire else None
            value["wire_saved"] = bool(wire)
            value["active_operation"] = bool(
                conn.execute(
                    "SELECT 1 FROM operations WHERE seller_id=? AND connection_id=? AND "
                    "scope_key=? AND status IN ('queued','running')",
                    (seller, row["connection_id"], document_id),
                ).fetchone()
            )
            value["retryable"] = retryable(value)
            value["events"] = [
                {
                    "state": r["state"],
                    "data": json.loads(r["data_json"]),
                    "created_at": r["created_at"],
                }
                for r in conn.execute(
                    "SELECT * FROM marking_events WHERE seller_id=? AND document_id=? ORDER BY id",
                    (seller, document_id),
                )
            ]
            return value

    def enqueue_document(self, seller, document_id, *, poll=False):
        try:
            with self.db.connection() as conn:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    "SELECT * FROM marking_documents WHERE seller_id=? AND id=?",
                    (seller, document_id),
                ).fetchone()
                if row is None:
                    raise NotFound("Документ не найден")
                if row["state"] not in (
                    {"accepted", "processing", "unknown"} if poll else {"prepared"}
                ):
                    raise Conflict("Документ уже отправлялся или не ожидает проверки")
                active = conn.execute(
                    "SELECT 1 FROM operations WHERE seller_id=? AND connection_id=? AND "
                    "scope_key=? AND status IN ('queued','running')",
                    (seller, row["connection_id"], document_id),
                ).fetchone()
                if active:
                    raise Conflict("Задание этого документа уже выполняется")
                if not poll:
                    for target in json.loads(row["body_json"]).get("targets", []):
                        conn.execute(
                            "INSERT INTO marking_targets VALUES (?,?,?,?)",
                            (seller, row["connection_id"], target, document_id),
                        )
                operation_id = self._queue(
                    conn,
                    seller,
                    row["connection_id"],
                    "document.poll" if poll else "document.submit",
                    {"document_id": document_id},
                    document_id,
                )
                self._event(
                    conn,
                    seller,
                    document_id,
                    "poll_queued" if poll else "submit_queued",
                    {"operation_id": operation_id},
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("Одна из целей уже участвует в незавершённой отправке") from exc
        return self.operations.get(seller, operation_id)

    @staticmethod
    def _event(conn, seller, document_id, state, data):
        conn.execute(
            "INSERT INTO marking_events(seller_id,document_id,state,data_json) VALUES (?,?,?,?)",
            (seller, document_id, state, canonical(data)),
        )

    def _state(self, seller, document_id, outcome, *, expected, wire=None):
        with self.db.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM marking_documents WHERE seller_id=? AND id=?", (seller, document_id)
            ).fetchone()
            if row is None:
                raise NotFound("Документ не найден")
            if row["state"] not in expected:
                raise Conflict("Состояние документа изменилось")
            state = outcome["state"]
            if state == "submitting":
                # Also protects direct worker API enqueue: preparation alone never grants a replay.
                for target in json.loads(row["body_json"]).get("targets", []):
                    existing = conn.execute(
                        "SELECT document_id FROM marking_targets WHERE seller_id=? AND "
                        "connection_id=? AND target_key=?",
                        (seller, row["connection_id"], target),
                    ).fetchone()
                    if existing and existing["document_id"] != document_id:
                        raise Conflict("Цель заблокирована другой отправкой")
                    if not existing:
                        conn.execute(
                            "INSERT INTO marking_targets VALUES (?,?,?,?)",
                            (seller, row["connection_id"], target, document_id),
                        )
            block = outcome.get("block")
            if block:
                self._save_block(conn, seller, row["connection_id"], document_id, block)
            if state in {"succeeded", "partial", "rejected", "cancelled", "prepared"}:
                conn.execute(
                    "DELETE FROM marking_targets WHERE seller_id=? AND document_id=?",
                    (seller, document_id),
                )
            conn.execute(
                "UPDATE marking_documents SET "
                "state=?,external_id=COALESCE(?,external_id),external_status=COALESCE(?,external_status),"
                "result_json=COALESCE(?,result_json),wire_json=COALESCE(?,wire_json),"
                "error_code=?,next_check_at=CASE "
                "WHEN ? IN ('accepted','processing') THEN "
                "strftime('%Y-%m-%dT%H:%M:%fZ','now','+30 seconds') ELSE NULL END,"
                "updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE seller_id=? AND id=?",
                (
                    state,
                    outcome.get("external_id"),
                    outcome.get("external_status"),
                    canonical(outcome["response"]) if "response" in outcome else None,
                    canonical(wire) if wire is not None else None,
                    outcome.get("error_code"),
                    state,
                    seller,
                    document_id,
                ),
            )
            self._event(
                conn,
                seller,
                document_id,
                state,
                {k: outcome.get(k) for k in ("external_id", "external_status", "error_code")},
            )

    @staticmethod
    def _save_block(conn, seller, connection, document_id, block):
        existing = conn.execute(
            "SELECT * FROM marking_code_blocks WHERE seller_id=? AND connection_id=? AND id=?",
            (seller, connection, block["id"]),
        ).fetchone()
        if existing:
            if (
                existing["order_id"] != block["order_id"]
                or existing["gtin"] != block["gtin"]
                or json.loads(existing["codes_json"]) != block["codes"]
            ):
                raise Conflict("СУЗ вернул другое содержимое сохранённого блока")
        else:
            conn.execute(
                "INSERT INTO "
                "marking_code_blocks(id,seller_id,connection_id,order_id,gtin,"
                "document_id,codes_json) "
                "VALUES (?,?,?,?,?,?,?)",
                (
                    block["id"],
                    seller,
                    connection,
                    block["order_id"],
                    block["gtin"],
                    document_id,
                    canonical(block["codes"]),
                ),
            )
        for cis, code in zip(block["cises"], block["codes"], strict=True):
            saved = conn.execute(
                "SELECT full_code FROM marking_codes WHERE seller_id=? AND connection_id=? AND "
                "code=?",
                (seller, connection, cis),
            ).fetchone()
            if saved and saved["full_code"] and saved["full_code"] != code:
                raise Conflict("КИ уже сохранён с другим полным КМ")
            conn.execute(
                "INSERT INTO "
                "marking_codes(id,seller_id,connection_id,code,full_code,block_id,order_id,"
                "product_group,gtin) VALUES (?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(seller_id,connection_id,code) DO UPDATE SET "
                "full_code=excluded.full_code,block_id=excluded.block_id,order_id=excluded.order_id,"
                "product_group=excluded.product_group,gtin=excluded.gtin,updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')",
                (
                    str(uuid4()),
                    seller,
                    connection,
                    cis,
                    code,
                    block["id"],
                    block["order_id"],
                    block["product_group"],
                    block["gtin"],
                ),
            )

    def execute_document(self, adapter, context, key, payload):
        document = self.get_document(
            context.seller_id, payload.get("document_id", ""), internal=True
        )
        if document["connection_id"] != context.connection_id:
            raise NotFound("Документ не принадлежит подключению")
        if key == "document.poll":
            if document["state"] not in {"accepted", "processing", "unknown"}:
                raise Conflict("Документ не ожидает проверки")
            outcome = adapter.poll(context, document)
            self._state(context.seller_id, document["id"], outcome, expected={document["state"]})
        else:
            if document["state"] != "prepared" or checksum(document["body"]) != document["digest"]:
                raise Conflict("Документ уже отправлялся или его данные изменились")
            wire = adapter.prepare_wire(context, document)
            self._state(
                context.seller_id,
                document["id"],
                {"state": "submitting"},
                expected={"prepared"},
                wire=wire,
            )
            try:
                outcome = adapter.send(context, document, wire)
                # Acknowledgement and full codes must survive a later worker-result failure.
                self._state(context.seller_id, document["id"], outcome, expected={"submitting"})
            except Exception as exc:
                self._state(
                    context.seller_id,
                    document["id"],
                    {
                        "state": "rejected"
                        if getattr(exc, "definite_rejection", False)
                        else "unknown",
                        "error_code": safe_error_code(exc),
                        "response": getattr(exc, "details", None),
                    },
                    expected={"submitting"},
                )
                raise
        batch = None
        if document["kind"] in {"nk_feed", "nk_sign"} and outcome["state"] in {
            "succeeded",
            "partial",
            "rejected",
        }:
            try:
                batch = adapter.refresh_document(context, document)
            except Exception:
                outcome["refresh_error"] = "nk_refresh_failed"
        return OperationResult(
            batch=batch,
            data={
                "document_id": document["id"],
                **{k: v for k, v in outcome.items() if k != "block"},
            },
        )

    def cancel_document(self, seller, document_id):
        with self.db.connection() as conn:
            row = conn.execute(
                "SELECT connection_id FROM marking_documents WHERE seller_id=? AND id=?",
                (seller, document_id),
            ).fetchone()
            if row is None:
                raise NotFound("Документ не найден")
            active = conn.execute(
                "SELECT 1 FROM operations WHERE seller_id=? AND connection_id=? AND "
                "scope_key=? AND status IN ('queued','running')",
                (seller, row["connection_id"], document_id),
            ).fetchone()
            if active:
                raise Conflict("Сначала дождитесь завершения задания")
        self._state(seller, document_id, {"state": "cancelled"}, expected={"prepared"})
        return self.get_document(seller, document_id)

    def retry_document(self, seller, document_id):
        document = self.get_document(seller, document_id)
        if not document["retryable"]:
            raise Conflict("Повтор доступен только после подтверждённого отказа до приёма запроса")
        self._state(seller, document_id, {"state": "prepared"}, expected={"rejected"})
        return self.get_document(seller, document_id)

    def reconcile_document(self, seller, document_id, external_id):
        document = self.get_document(seller, document_id)
        if document["state"] != "unknown":
            raise Conflict("Сверка доступна для неизвестного результата")
        connection = self._connection(seller, document["connection_id"])
        adapter, context = self.registry.get("chz"), connection_context(connection)
        outcome = adapter.reconcile(context, document, external_id)
        self._state(seller, document_id, outcome, expected={"unknown"})
        if document["kind"] in {"nk_feed", "nk_sign"}:
            try:
                batch = adapter.refresh_document(context, document)
            except Exception:
                batch = None
            with self.db.connection() as conn:
                conn.execute("BEGIN IMMEDIATE")
                if batch is not None:
                    Records(self.db).apply_in_transaction(conn, seller, connection["id"], batch)
                self._invalidate_nk(conn, seller, connection["id"], document, batch)
                if batch is None:
                    conn.execute(
                        "UPDATE marking_documents SET error_code='nk_refresh_failed' "
                        "WHERE seller_id=? AND id=?",
                        (seller, document_id),
                    )
        return self.get_document(seller, document_id)

    @staticmethod
    def _invalidate_nk(conn, seller, connection, document, batch):
        body = document["body"]
        targets = (
            list(body.get("versions", {}))
            if document["kind"] == "nk_feed"
            else [str(v["goodId"]) for v in body["xmls"]]
        )
        returned = {v.external_id for v in batch.products} if batch is not None else set()
        for external_id in targets:
            conn.execute(
                "UPDATE nk_cards SET etag=NULL,detail_available=? WHERE seller_id=? "
                "AND connection_id=? AND external_id=?",
                (int(external_id in returned), seller, connection, external_id),
            )

    def queue_due(self):
        # Read checks alone are scheduled. A submission is never scheduled here.
        with self.db.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT d.* FROM marking_documents d WHERE d.state IN ('accepted','processing') "
                "AND d.next_check_at<=strftime('%Y-%m-%dT%H:%M:%fZ','now') AND NOT EXISTS "
                "(SELECT 1 FROM operations o "
                "WHERE o.seller_id=d.seller_id AND o.connection_id=d.connection_id AND "
                "o.scope_key=d.id "
                "AND o.status IN ('queued','running')) ORDER BY d.next_check_at LIMIT 1",
            ).fetchone()
            if row:
                self._queue(
                    conn,
                    row["seller_id"],
                    row["connection_id"],
                    "document.poll",
                    {"document_id": row["id"]},
                    row["id"],
                )
                conn.execute(
                    "UPDATE marking_documents SET "
                    "next_check_at=strftime('%Y-%m-%dT%H:%M:%fZ','now','+60 seconds') WHERE "
                    "seller_id=? AND id=?",
                    (row["seller_id"], row["id"]),
                )

    def codes(self, seller, connection, offset=0, limit=100):
        self._connection(seller, connection)
        with self.db.connection() as conn:
            total = conn.execute(
                "SELECT count(*) FROM marking_codes WHERE seller_id=? AND connection_id=?",
                (seller, connection),
            ).fetchone()[0]
            rows = []
            for r in conn.execute(
                "SELECT * FROM marking_codes WHERE seller_id=? AND connection_id=? ORDER BY "
                "updated_at DESC,id LIMIT ? OFFSET ?",
                (seller, connection, limit, offset),
            ):
                value = dict(r)
                value["attributes"] = json.loads(value.pop("attributes_json"))
                value["has_full_code"] = bool(value.pop("full_code"))
                rows.append(value)
        return {"items": rows, "total": total, "offset": offset, "limit": limit}

    def code_export(self, seller, connection, block_id):
        self._connection(seller, connection)
        with self.db.connection() as conn:
            row = conn.execute(
                "SELECT * FROM marking_code_blocks WHERE seller_id=? AND connection_id=? AND id=?",
                (seller, connection, block_id),
            ).fetchone()
            if row is None:
                raise NotFound("Блок не найден")
            return {
                "block_id": row["id"],
                "order_id": row["order_id"],
                "gtin": row["gtin"],
                "codes": json.loads(row["codes_json"]),
            }

    def _recover_documents(self, conn):
        for row in conn.execute(
            "SELECT seller_id,id FROM marking_documents WHERE state='submitting'"
        ).fetchall():
            conn.execute(
                "UPDATE marking_documents SET "
                "state='unknown',error_code='process_interrupted',next_check_at=NULL,"
                "updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE seller_id=? AND id=?",
                (row["seller_id"], row["id"]),
            )
            self._event(
                conn, row["seller_id"], row["id"], "unknown", {"error_code": "process_interrupted"}
            )
        # Work interrupted before the write boundary remains a reviewable prepared draft.
        conn.execute(
            "DELETE FROM marking_targets WHERE EXISTS (SELECT 1 FROM marking_documents d WHERE "
            "d.seller_id=marking_targets.seller_id AND d.id=marking_targets.document_id AND "
            "d.state='prepared') AND NOT EXISTS (SELECT 1 FROM operations o WHERE "
            "o.seller_id=marking_targets.seller_id AND o.scope_key=marking_targets.document_id "
            "AND o.status IN ('queued','running'))"
        )

    def _fail_document(self, conn, job, code):
        row = conn.execute(
            "SELECT * FROM marking_documents WHERE seller_id=? AND connection_id=? AND id=?",
            (job["seller_id"], job["connection_id"], job["payload"].get("document_id")),
        ).fetchone()
        if row is None:
            return
        self._event(conn, row["seller_id"], row["id"], "local_step_failed", {"error_code": code})
        if row["state"] == "prepared":
            conn.execute(
                "DELETE FROM marking_targets WHERE seller_id=? AND document_id=?",
                (row["seller_id"], row["id"]),
            )
            conn.execute(
                "UPDATE marking_documents SET error_code=? WHERE seller_id=? AND id=?",
                (code, row["seller_id"], row["id"]),
            )

    def _apply_codes(self, conn, job, rows):
        for item in rows:
            info = item.get("cisInfo") or {}
            cis = info.get("requestedCis")
            if not isinstance(cis, str) or not cis:
                continue
            conn.execute(
                "INSERT INTO "
                "marking_codes(id,seller_id,connection_id,code,product_group,gtin,"
                "external_status,attributes_json) VALUES (?,?,?,?,?,?,?,?) "
                "ON CONFLICT(seller_id,connection_id,code) DO UPDATE SET "
                "external_status=excluded.external_status,attributes_json=excluded.attributes_json,"
                "updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')",
                (
                    str(uuid4()),
                    job["seller_id"],
                    job["connection_id"],
                    cis,
                    info.get("productGroup"),
                    info.get("gtin"),
                    info.get("status"),
                    canonical(item),
                ),
            )

    def validate_code_selection(self, seller, connection, ids, *, full=False):
        self._connection(seller, connection)
        if not isinstance(ids, list) or not 1 <= len(ids) <= 500 or len(set(ids)) != len(ids):
            raise InvalidInput("Выберите от 1 до 500 разных кодов")
        with self.db.connection() as conn:
            values = []
            for code_id in ids:
                row = conn.execute(
                    "SELECT * FROM marking_codes WHERE seller_id=? AND connection_id=? AND id=?",
                    (seller, connection, code_id),
                ).fetchone()
                if row is None:
                    raise NotFound("Код не найден в этом подключении")
                if full and not row["full_code"]:
                    raise InvalidInput("У выбранного КИ не сохранён полный КМ")
                values.append(dict(row))
            return values

    def utilisation_from_codes(self, seller, connection, payload):
        values = self.validate_code_selection(
            seller, connection, payload.get("code_ids"), full=True
        )
        group = payload.get("product_group")
        if any(v["product_group"] != group for v in values):
            raise InvalidInput("Выбранные коды принадлежат другой товарной группе")
        return self.prepare(
            seller,
            connection,
            "suz_utilisation",
            {
                "product_group": group,
                "document": {
                    "productGroup": group,
                    "utilisationType": "UTILISATION",
                    "sntins": [v["full_code"] for v in values],
                    "attributes": payload.get("attributes", {}),
                },
            },
        )
