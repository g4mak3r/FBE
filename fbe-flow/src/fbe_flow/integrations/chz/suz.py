"""OMS API 3.0: dynamic authentication, emission, blocks and application reports."""

import base64
import json
import time
from datetime import datetime

from fbe_flow.core.credentials import credential_owner
from fbe_flow.core.errors import Conflict, InvalidInput
from fbe_flow.integrations.chz.formats import cis_from_code, encode_json, gtin_text, uuid_text
from fbe_flow.integrations.chz.http import RemoteError, redact


class SuzWorkflows:
    def suz_receipt(self, config, external_id):
        value = self._call(
            config,
            "suz",
            "GET",
            "/api/v3/receipts/receipt",
            params={"resultDocId": uuid_text(external_id)},
        )
        if not isinstance(value, dict) or not isinstance(value.get("results"), list):
            raise RemoteError(None, "suz_receipt_invalid")
        for receipt in value["results"]:
            if (
                not isinstance(receipt, dict)
                or not isinstance(receipt.get("details"), dict)
                or receipt["details"].get("participantInn") != config.inn
            ):
                raise RemoteError(None, "suz_receipt_mismatch")
        return value

    def reconcile_suz_document(self, config, document, external_id):
        external_id = uuid_text(external_id)
        if document["body"].get("oms_id") != config.oms_id:
            raise Conflict("Документ подготовлен для другого СУЗ")
        workflow = {
            "suz_order": "CREATE_ORDER",
            "suz_utilisation": "REPORT_UTILIZE",
            "suz_close": "CLOSE_ORDER",
        }[document["kind"]]
        order_action = document["kind"] in {"suz_order", "suz_close"}
        value = self._call(
            config,
            "suz",
            "POST",
            "/api/v3/receipts/receipt/search",
            body={
                "filter": {"orderIds" if order_action else "sourceDocIds": [external_id]},
                "limit": 100,
                "skip": 1,
            },
        )
        receipts = value.get("results") if isinstance(value, dict) else None
        if not isinstance(receipts, list):
            raise RemoteError(None, "suz_receipt_invalid")
        receipts = [
            v
            for v in receipts
            if isinstance(v, dict)
            and (order_action or v.get("sourceDocId") == external_id)
            and v.get("workflow") == workflow
            and isinstance(v.get("details"), dict)
            and v["details"].get("participantInn") == config.inn
        ]
        if len(receipts) != 1:
            raise Conflict("Квитанция не подтверждает единственный документ этого аккаунта")
        receipt = receipts[0]
        start = next(
            (v["created_at"] for v in reversed(document["events"]) if v["state"] == "submitting"),
            None,
        )
        try:
            if (
                not start
                or type(receipt.get("sourceDocDate")) is not int
                or receipt["sourceDocDate"]
                < datetime.fromisoformat(start.replace("Z", "+00:00")).timestamp() * 1000 - 300000
            ):
                raise ValueError
        except (ValueError, TypeError) as exc:
            raise Conflict(
                "Квитанция относится к более ранней или неподтверждённой отправке"
            ) from exc
        events = receipt.get("operations") or []
        if (
            not isinstance(events, list)
            or not events
            or not isinstance(events[0], dict)
            or not events[0].get("docId")
            or not isinstance(events[0].get("details"), dict)
            or events[0]["details"].get("omsId") != config.oms_id
        ):
            raise Conflict("Нет исходного документа в первом событии квитанции")
        original = self._call(
            config,
            "suz",
            "GET",
            "/api/v3/receipts/document",
            params={
                "resultDocId": uuid_text(receipt.get("resultDocId")),
                "docId": uuid_text(events[0]["docId"]),
            },
        )
        try:
            content = json.loads(original["content"])
        except (ValueError, TypeError, KeyError) as exc:
            raise Conflict("СУЗ не вернул исходное содержимое для точной сверки") from exc
        if content != document["body"]["document"]:
            raise Conflict("Содержимое документа в квитанции отличается; повтор запрещён")
        outcome = self.poll_suz(config, {**document, "external_id": external_id})
        outcome["response"] = {"status": outcome["response"], "receipt": receipt}
        return {**outcome, "external_id": external_id}

    def _client_token(self, config):
        oms_connection = uuid_text(config.oms_connection)
        uuid_text(config.oms_id)
        if not config.certificate:
            raise InvalidInput("Для СУЗ требуется сертификат УКЭП")
        key = (
            config.credential_ref,
            config.environment,
            config.inn,
            config.certificate,
            oms_connection,
            config.oms_id,
        )
        self.vault.get(credential_owner(config.credential_ref), config.credential_ref)
        with self._lock:
            cached = self._suz_tokens.get(key)
            if cached and cached[1] > time.monotonic():
                return cached[0]
            challenge = self.http.request("GET", self.urls(config)["true"] + "/auth/key").data
            if not isinstance(challenge, dict) or any(
                not isinstance(challenge.get(k), str) or not challenge[k] for k in ("uuid", "data")
            ):
                raise RemoteError(None, "auth_response_invalid")
            signature = self.signer.sign(
                challenge["data"].encode("utf-8"), config.certificate, detached=False
            )
            try:
                response = self.http.request(
                    "POST",
                    self.urls(config)["true"] + f"/auth/simpleSignIn/{oms_connection}",
                    body={"uuid": challenge["uuid"], "data": signature, "inn": config.inn},
                ).data
            except RemoteError as exc:
                exc.details = redact(exc.details, [signature, challenge["data"]])
                raise
            if (
                not isinstance(response, dict)
                or not isinstance(response.get("token"), str)
                or not response["token"]
            ):
                raise RemoteError(None, "suz_auth_invalid")
            self._suz_tokens[key] = (response["token"], time.monotonic() + 9 * 3600)
            return response["token"]

    @staticmethod
    def _oms_response(config, response):
        if (
            not isinstance(response, dict)
            or not isinstance(response.get("omsId"), str)
            or response["omsId"].lower() != config.oms_id.lower()
        ):
            raise RemoteError(None, "suz_response_mismatch")
        return response

    def suz_ping(self, config):
        return self._oms_response(config, self._call(config, "suz", "GET", "/api/v3/ping"))

    def suz_orders(self, config):
        value = self._oms_response(config, self._call(config, "suz", "GET", "/api/v3/order/list"))
        rows = value.get("orderInfos")
        if not isinstance(rows, list) or any(not isinstance(v, dict) for v in rows):
            raise RemoteError(None, "suz_orders_invalid")
        return rows

    def suz_status(self, config, payload):
        order = uuid_text(payload.get("order_id"))
        params = {"orderId": order}
        if payload.get("gtin"):
            params["gtin"] = gtin_text(payload["gtin"])
        rows = self._call(config, "suz", "GET", "/api/v3/order/status", params=params)
        if not isinstance(rows, list) or not rows:
            raise RemoteError(None, "suz_order_status_invalid")
        for row in rows:
            self._oms_response(config, row)
            if row.get("orderId", "").lower() != order or (
                payload.get("gtin") and row.get("gtin") != payload["gtin"]
            ):
                raise RemoteError(None, "suz_order_status_mismatch")
        return {"order_id": order, "buffers": rows}

    def suz_blocks(self, config, payload):
        order, gtin = uuid_text(payload.get("order_id")), gtin_text(payload.get("gtin"))
        value = self._oms_response(
            config,
            self._call(
                config,
                "suz",
                "GET",
                "/api/v3/order/codes/blocks",
                params={"orderId": order, "gtin": gtin},
            ),
        )
        if (
            value.get("orderId", "").lower() != order
            or value.get("gtin") != gtin
            or not isinstance(value.get("blocks"), list)
        ):
            raise RemoteError(None, "suz_blocks_invalid")
        try:
            ids = [uuid_text(v.get("blockId")) for v in value["blocks"]]
        except (InvalidInput, AttributeError) as exc:
            raise RemoteError(None, "suz_blocks_invalid") from exc
        if len(set(ids)) != len(ids):
            raise RemoteError(None, "suz_blocks_invalid")
        return value

    def prepare_suz(self, config, action, payload):
        self.suz_ping(config)
        group = payload.get("product_group")
        self.require_group(config, group)
        if action == "suz_order":
            value = payload.get("document")
            if (
                not isinstance(value, dict)
                or value.get("productGroup") != group
                or set(value) - {"productGroup", "products", "attributes", "serviceProviderId"}
            ):
                raise InvalidInput("Укажите тело заказа СУЗ с выбранной productGroup")
            products = value.get("products")
            if (
                not isinstance(products, list)
                or not 1 <= len(products) <= 10
                or any(not isinstance(v, dict) for v in products)
            ):
                raise InvalidInput("Заказ должен содержать от 1 до 10 товарных позиций")
            gtins = []
            for product in products:
                gtin = gtin_text(product.get("gtin"))
                gtins.append(gtin)
                quantity = product.get("quantity")
                if (
                    isinstance(quantity, bool)
                    or not isinstance(quantity, int)
                    or not 1 <= quantity <= (2000000 if len(products) == 1 else 150000)
                ):
                    raise InvalidInput("Недопустимое количество КМ в позиции СУЗ")
                if product.get("serialNumberType") not in {"OPERATOR", "SELF_MADE"}:
                    raise InvalidInput("Выберите способ генерации серийных номеров")
                if product.get("serialNumberType") == "SELF_MADE":
                    serials = product.get("serialNumbers")
                    if (
                        not isinstance(serials, list)
                        or len(serials) != quantity
                        or any(not isinstance(v, str) or not v for v in serials)
                        or len(set(serials)) != len(serials)
                    ):
                        raise InvalidInput(
                            "Количество разных серийных номеров должно совпадать с quantity"
                        )
                elif product.get("serialNumbers"):
                    raise InvalidInput("serialNumbers используются только при SELF_MADE")
                if (
                    isinstance(product.get("templateId"), bool)
                    or not isinstance(product.get("templateId"), int)
                    or product["templateId"] <= 0
                    or not isinstance(product.get("cisType"), str)
                    or not product["cisType"]
                ):
                    raise InvalidInput(
                        "Для позиции нужны templateId и cisType из спецификации группы"
                    )
                cards = self._call(config, "nk", "GET", "/v3/feed-product", params={"gtin": gtin})
                cards = cards.get("result", cards) if isinstance(cards, dict) else cards
                if not isinstance(cards, list) or not any(
                    v.get("good_mark_flag")
                    and gtin in [i.get("value") for i in v.get("identified_by", [])]
                    for v in cards
                    if isinstance(v, dict)
                ):
                    raise InvalidInput("НК не подтвердил право и готовность GTIN к эмиссии")
            if len(set(gtins)) != len(gtins):
                raise InvalidInput("GTIN в заказе не должны повторяться")
            title = f"СУЗ: заказ {sum(v['quantity'] for v in products)} КМ"
            body = {
                "document": value,
                "product_group": group,
                "targets": [f"suz-emission:{group}:{v}" for v in gtins],
            }
        elif action == "suz_codes":
            order, gtin = uuid_text(payload.get("order_id")), gtin_text(payload.get("gtin"))
            quantity = payload.get("quantity")
            if (
                isinstance(quantity, bool)
                or not isinstance(quantity, int)
                or not 1 <= quantity <= 5000
            ):
                raise InvalidInput("За один локальный блок можно получить от 1 до 5000 КМ")
            orders = [v for v in self.suz_orders(config) if v.get("orderId", "").lower() == order]
            if len(orders) != 1 or orders[0].get("productGroup") != group:
                raise InvalidInput("Заказ не принадлежит выбранной группе этого СУЗ")
            status = self.suz_status(config, {"order_id": order, "gtin": gtin})
            if any(
                v.get("bufferStatus") != "ACTIVE"
                or not isinstance(v.get("availableCodes"), int)
                or v["availableCodes"] < quantity
                for v in status["buffers"]
            ):
                raise InvalidInput("Буфер СУЗ не готов или в нём недостаточно КМ")
            blocks = self.suz_blocks(config, {"order_id": order, "gtin": gtin})
            body = {
                "order_id": order,
                "gtin": gtin,
                "quantity": quantity,
                "product_group": group,
                "baseline": [v["blockId"] for v in blocks["blocks"]],
                "targets": [f"suz-buffer:{order}:{gtin}"],
            }
            title = f"СУЗ: получить {quantity} КМ · {gtin}"
        elif action == "suz_utilisation":
            value = payload.get("document")
            if (
                not isinstance(value, dict)
                or value.get("productGroup") != group
                or value.get("utilisationType", "UTILISATION") != "UTILISATION"
                or set(value) - {"productGroup", "utilisationType", "sntins", "attributes"}
            ):
                raise InvalidInput("Укажите отчёт о нанесении СУЗ типа UTILISATION")
            codes = value.get("sntins")
            if (
                not isinstance(codes, list)
                or not 1 <= len(codes) <= 5000
                or any(not isinstance(v, str) or "\x1d" not in v for v in codes)
            ):
                raise InvalidInput(
                    "Отчёт требует от 1 до 5000 полных КМ с кодом проверки и разделителем GS"
                )
            cises = [cis_from_code(v) for v in codes]
            if len(set(cises)) != len(cises):
                raise InvalidInput("КИ не должны повторяться")
            preflight = []
            for start in range(0, len(cises), 500):
                preflight.extend(self.check_codes(config, cises[start : start + 500], group))
            if any(
                row.get("errorCode")
                or (row.get("cisInfo") or {}).get("ownerInn") != config.inn
                or (row.get("cisInfo") or {}).get("status") != "EMITTED"
                or (row.get("cisInfo") or {}).get("productGroup") != group
                or (row.get("cisInfo") or {}).get("packageType") != "UNIT"
                for row in preflight
            ):
                raise InvalidInput(
                    "Для нанесения не подтверждены владелец, группа или статус EMITTED всех КМ"
                )
            body = {
                "document": value,
                "product_group": group,
                "targets": [f"cis:{v}" for v in cises],
                "preflight": preflight,
            }
            title = f"СУЗ: нанесение {len(codes)} КМ"
        elif action == "suz_close":
            order = uuid_text(payload.get("order_id"))
            orders = [v for v in self.suz_orders(config) if v.get("orderId", "").lower() == order]
            if len(orders) != 1 or orders[0].get("productGroup") != group:
                raise InvalidInput("Заказ не принадлежит выбранной группе этого СУЗ")
            value = {"orderId": order}
            if payload.get("gtin"):
                value["gtin"] = gtin_text(payload["gtin"])
            targets = [
                f"suz-buffer:{order}:{v['gtin']}"
                for v in orders[0].get("buffers", [])
                if not value.get("gtin") or value["gtin"] == v.get("gtin")
            ]
            if not targets:
                raise InvalidInput("Заказ не содержит выбранных буферов")
            body = {"document": value, "product_group": group, "targets": targets}
            title = "СУЗ: закрытие заказа " + order
        else:
            raise InvalidInput("Неизвестная операция СУЗ")
        if "document" in body and (
            not isinstance(body["document"].get("attributes", {}), dict)
            or len(encode_json(body["document"])) > 5 * 1024 * 1024
        ):
            raise InvalidInput("attributes должны быть объектом; лимит документа 5 МиБ")
        body["oms_id"] = config.oms_id
        return {"kind": action, "title": title, "body": body}

    def prepare_suz_wire(self, config, document):
        kind, body = document["kind"], document["body"]
        if body.get("oms_id") != config.oms_id:
            raise Conflict("Документ подготовлен для другого СУЗ")
        self.suz_ping(config)
        self.require_group(config, body["product_group"])
        if kind == "suz_codes":
            fresh = self.prepare_suz(config, kind, body)
            if set(fresh["body"]["baseline"]) != set(body["baseline"]):
                raise Conflict("Список блоков изменился; подготовьте получение заново")
            return {
                "params": {
                    "orderId": body["order_id"],
                    "gtin": body["gtin"],
                    "quantity": body["quantity"],
                }
            }
        payload = {"product_group": body["product_group"], "document": body["document"]}
        if kind == "suz_close":
            payload.update(order_id=body["document"]["orderId"], gtin=body["document"].get("gtin"))
        self.prepare_suz(config, kind, payload)
        raw = encode_json(body["document"])
        signature = self.signer.sign(raw, config.certificate, detached=True)
        return {"data": base64.b64encode(raw).decode(), "signature": signature}

    def _block(self, config, document, response, *, expected_id=None):
        value = self._oms_response(config, response)
        try:
            block_id = uuid_text(value.get("blockId"))
        except InvalidInput as exc:
            raise RemoteError(None, "suz_block_invalid") from exc
        codes = value.get("codes")
        body = document["body"]
        if expected_id and block_id != expected_id:
            raise RemoteError(None, "suz_block_mismatch")
        if not isinstance(codes, list) or len(codes) != body["quantity"]:
            raise RemoteError(None, "suz_block_size_invalid")
        try:
            cises = [cis_from_code(v) for v in codes]
        except InvalidInput as exc:
            raise RemoteError(None, "suz_block_codes_invalid") from exc
        if (
            len(set(cises)) != len(cises)
            or any(v[2:16] != body["gtin"] for v in cises)
            or any("\x1d" not in v for v in codes)
        ):
            raise RemoteError(None, "suz_block_codes_invalid")
        return {
            "state": "succeeded",
            "external_id": block_id,
            "external_status": "downloaded",
            "response": {"omsId": value["omsId"], "blockId": block_id, "quantity": len(codes)},
            "block": {
                "id": block_id,
                "order_id": body["order_id"],
                "gtin": body["gtin"],
                "product_group": body["product_group"],
                "codes": codes,
                "cises": cises,
            },
        }

    def send_suz(self, config, document, wire):
        kind = document["kind"]
        if document["body"].get("oms_id") != config.oms_id:
            raise Conflict("Документ подготовлен для другого СУЗ")
        if kind == "suz_codes":
            return self._block(
                config,
                document,
                self._call(config, "suz", "GET", "/api/v3/codes", params=wire["params"]),
            )
        paths = {
            "suz_order": "/api/v3/order",
            "suz_utilisation": "/api/v3/utilisation",
            "suz_close": "/api/v3/order/close",
        }
        if kind not in paths:
            raise InvalidInput("Неподдержанный документ СУЗ")
        response = self._oms_response(
            config,
            self._call(
                config,
                "suz",
                "POST",
                paths[kind],
                body=base64.b64decode(wire["data"], validate=True),
                headers={"X-Signature": wire["signature"]},
            ),
        )
        field = "orderId" if kind == "suz_order" else "reportId"
        if kind == "suz_close":
            external_id = document["body"]["document"]["orderId"]
        else:
            try:
                external_id = uuid_text(response.get(field))
            except InvalidInput as exc:
                raise RemoteError(None, "acknowledgement_missing") from exc
        return {"state": "accepted", "external_id": external_id, "response": response}

    def poll_suz(self, config, document):
        kind, external_id = document["kind"], uuid_text(document["external_id"])
        if document["body"].get("oms_id") != config.oms_id:
            raise Conflict("Документ подготовлен для другого СУЗ")
        if kind == "suz_utilisation":
            response = self._oms_response(
                config,
                self._call(
                    config, "suz", "GET", "/api/v3/report/info", params={"reportId": external_id}
                ),
            )
            if response.get("reportId", "").lower() != external_id:
                raise RemoteError(None, "suz_report_mismatch")
            status = response.get("reportStatus")
            state = {
                "SUCCESS": "succeeded",
                "FAILED": "rejected",
                "REJECTED": "rejected",
                "PARTIALLY": "partial",
            }.get(status, "processing")
        elif kind in {"suz_order", "suz_close"}:
            rows = [
                v for v in self.suz_orders(config) if v.get("orderId", "").lower() == external_id
            ]
            if len(rows) != 1 or rows[0].get("productGroup") != document["body"]["product_group"]:
                raise RemoteError(None, "suz_order_missing")
            response = rows[0]
            status = response.get("orderStatus")
            state = (
                {"DECLINED": "rejected", "READY": "succeeded", "CLOSED": "succeeded"}.get(
                    status, "processing"
                )
                if kind == "suz_order"
                else "processing"
            )
            if kind == "suz_close":
                gtin = document["body"]["document"].get("gtin")
                buffers = [
                    v for v in response.get("buffers", []) if not gtin or v.get("gtin") == gtin
                ]
                if status == "CLOSED" or (
                    buffers and all(v.get("bufferStatus") == "CLOSED" for v in buffers)
                ):
                    state = "succeeded"
            elif state == "succeeded":
                buffers = response.get("buffers")
                expected = {v["gtin"] for v in document["body"]["document"]["products"]}
                if (
                    not isinstance(buffers, list)
                    or any(not isinstance(v, dict) for v in buffers)
                    or len(buffers) != len(expected)
                    or {v.get("gtin") for v in buffers} != expected
                ):
                    raise RemoteError(None, "suz_order_buffers_invalid")
                rejected = sum(v.get("bufferStatus") == "REJECTED" for v in buffers)
                if rejected:
                    state = "rejected" if rejected == len(buffers) else "partial"
                elif any(
                    v.get("bufferStatus") not in {"ACTIVE", "EXHAUSTED", "CLOSED"} for v in buffers
                ):
                    state = "processing"
        else:
            raise InvalidInput("Документ не ожидает обработки СУЗ")
        return {"state": state, "external_status": status, "response": response}

    def reconcile_block(self, config, document, external_id):
        block_id = uuid_text(external_id)
        body = document["body"]
        blocks = self.suz_blocks(config, body)["blocks"]
        candidates = [
            v
            for v in blocks
            if v["blockId"] not in body["baseline"] and v.get("quantity") == body["quantity"]
        ]
        # If another client issued a block, attribution is ambiguous. Keep the buffer locked.
        if len(candidates) != 1 or candidates[0]["blockId"].lower() != block_id:
            raise Conflict(
                "Нельзя однозначно связать блок с этим запросом; повторная выдача запрещена"
            )
        response = self._call(
            config, "suz", "GET", "/api/v3/order/codes/retry", params={"blockId": block_id}
        )
        return self._block(config, document, response, expected_id=block_id)
