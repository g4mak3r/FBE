"""NK changes and True API documents. Preparation never sends a business mutation."""

import base64
import json
from datetime import date

from fbe_flow.core.errors import Conflict, InvalidInput
from fbe_flow.integrations.chz.formats import (
    cis_from_code,
    digest,
    encode_json,
    positive_ids,
    uuid_text,
)
from fbe_flow.integrations.chz.http import RemoteError
from fbe_flow.integrations.chz.suz import SuzWorkflows


def unwrap(value):
    return value.get("result", value) if isinstance(value, dict) else value


def card_content(card):
    # State flags change during publication, descriptive data must remain the same.
    return {k: card.get(k) for k in ("good_name", "identified_by", "categories", "good_attrs")}


class ChzWorkflows(SuzWorkflows):
    def require_group(self, config, group):
        if not isinstance(group, str) or group not in self.account(config).get("productGroups", []):
            raise InvalidInput("Товарная группа не подключена к аккаунту")

    def attribute_model(self, config, card):
        categories = card.get("categories") or []
        if card.get("is_set"):
            params = {"is_set": "true"}
        elif len(categories) == 1 and categories[0].get("cat_id"):
            params = {"cat_id": categories[0]["cat_id"]}
        else:
            raise InvalidInput("Карточке нужна однозначная категория НК")
        model = unwrap(
            self._call(config, "nk", "GET", "/v3/attributes", params={**params, "attr_type": "a"})
        )
        if not isinstance(model, list) or any(
            not isinstance(v, dict) or not v.get("attr_id") for v in model
        ):
            raise RemoteError(None, "nk_attributes_invalid")
        return model

    def edit_model(self, config, ids):
        cards = self._require_cards(config, positive_ids(ids))
        cache = {}
        models = []
        for card in cards:
            key = digest({"categories": card.get("categories"), "is_set": card.get("is_set")})
            if key not in cache:
                cache[key] = self.attribute_model(config, card)
            models.append(cache[key])
        common = set.intersection(
            *(
                set(str(v["attr_id"]) for v in model if v.get("attr_type") != "b")
                for model in models
            )
        )
        return {"attributes": [v for v in models[0] if str(v["attr_id"]) in common], "cards": cards}

    def _require_cards(self, config, ids):
        cards = self._own_cards(config, ids)
        if {str(v["good_id"]) for v in cards} != set(map(str, ids)):
            raise InvalidInput("НК не вернул все выбранные собственные карточки")
        return cards

    def preview_edit(self, config, payload):
        ids = positive_ids(payload.get("good_ids"))
        patch = payload.get("attributes")
        if not isinstance(patch, list) or not 1 <= len(patch) <= 100:
            raise InvalidInput("Укажите от 1 до 100 изменений атрибутов")
        allowed = {"attr_id", "attr_value", "attr_value_type", "gtin", "delete"}
        for change in patch:
            if (
                not isinstance(change, dict)
                or set(change) - allowed
                or not str(change.get("attr_id", "")).isdigit()
                or not isinstance(change.get("attr_value"), str)
                or not 1 <= len(change["attr_value"]) <= 10000
                or ("delete" in change and not isinstance(change["delete"], bool))
            ):
                raise InvalidInput("Проверьте ID, значение и параметры атрибута")
        if len({digest(v) for v in patch}) != len(patch):
            raise InvalidInput("Изменения не должны повторяться")
        cards = self._require_cards(config, ids)
        entries, previews, versions, models = [], [], {}, {}
        for card in cards:
            states = card.get("good_detailed_status") or [card.get("good_status")]
            if not states or any(
                s not in {"draft", "errors", "notsigned", "published"} for s in states
            ):
                raise InvalidInput("Карточка в архиве, на модерации или в неизвестном статусе")
            model_key = digest({"categories": card.get("categories"), "is_set": card.get("is_set")})
            if model_key not in models:
                models[model_key] = {
                    str(v["attr_id"]): v for v in self.attribute_model(config, card)
                }
            model = models[model_key]
            changes = []
            for item in patch:
                attr = model.get(str(item["attr_id"]))
                if not attr or attr.get("attr_type") == "b":
                    raise InvalidInput("Атрибут заблокирован или отсутствует в категории")
                same = [
                    v
                    for v in patch
                    if str(v["attr_id"]) == str(item["attr_id"]) and not v.get("delete")
                ]
                if not attr.get("attr_multiplicity") and len(same) > 1:
                    raise InvalidInput("Атрибут допускает только одно значение")
                if attr.get("attr_multiplicity_type") == "unique" and len(
                    {v.get("attr_value_type") for v in same}
                ) != len(same):
                    raise InvalidInput("Типы значений множественного атрибута должны различаться")
                if item.get("attr_value_type") and item["attr_value_type"] not in (
                    attr.get("attr_value_type") or []
                ):
                    raise InvalidInput("Тип значения отсутствует в справочнике НК")
                presets = attr.get("attr_preset") or []
                if attr.get("attr_preset_only") and presets and item["attr_value"] not in presets:
                    raise InvalidInput("Выберите значение из справочника НК")
                current = [
                    v.get("attr_value")
                    for v in card.get("good_attrs", [])
                    if str(v.get("attr_id")) == str(item["attr_id"])
                    and v.get("attr_value_type") == item.get("attr_value_type")
                    and v.get("gtin") == item.get("gtin")
                ]
                if item.get("delete") and item["attr_value"] not in current:
                    raise InvalidInput("Удаляемое значение отсутствует в карточке")
                changes.append(
                    {
                        "name": attr.get("attr_name"),
                        "before": current,
                        "after": item,
                        "reference_url": attr.get("preset_url"),
                    }
                )
            entries.append(
                {
                    "good_id": card["good_id"],
                    "good_attrs": patch,
                    "moderation": bool(payload.get("moderation")),
                }
            )
            previews.append(
                {
                    "good_id": card["good_id"],
                    "title": card.get("good_name"),
                    "status": states,
                    "changes": changes,
                }
            )
            versions[str(card["good_id"])] = digest(card)
        return {
            "kind": "nk_feed",
            "title": f"Изменение {len(ids)} карточек",
            "body": {
                "entries": entries,
                "versions": versions,
                "preview": previews,
                "targets": [f"nk:{v}" for v in ids],
            },
        }

    def prepare_signature(self, config, payload):
        ids = positive_ids(payload.get("good_ids"), maximum=10)
        cards = self._require_cards(config, ids)
        agreement = payload.get("publication_agreement", False)
        response = unwrap(
            self._call(
                config,
                "nk",
                "POST",
                "/v3/feed-product-document",
                body={"goodIds": ids, "publicationAgreement": agreement},
            )
        )
        xmls = response.get("xmls") if isinstance(response, dict) else None
        if (
            not isinstance(xmls, list)
            or response.get("errors")
            or len(xmls) != len(ids)
            or any(
                not isinstance(v, dict) or not isinstance(v.get("xml"), str) or not v["xml"]
                for v in xmls
            )
            or {v.get("goodId") for v in xmls} != set(ids)
        ):
            raise InvalidInput("НК не подготовил XML для всех выбранных карточек")
        return {
            "kind": "nk_sign",
            "title": f"Публикация {len(ids)} карточек",
            "body": {
                "xmls": xmls,
                "publication_agreement": agreement,
                "contents": {str(v["good_id"]): digest(card_content(v)) for v in cards},
                "targets": [f"nk:{v}" for v in ids],
            },
        }

    def check_codes(self, config, values, group):
        if not isinstance(values, list) or not 1 <= len(values) <= 500:
            raise InvalidInput("Укажите от 1 до 500 кодов")
        codes = [cis_from_code(v) for v in values]
        if len(set(codes)) != len(codes):
            raise InvalidInput("Коды не должны повторяться")
        self.require_group(config, group)
        rows = self._call(config, "true", "POST", "/cises/info", params={"pg": group}, body=codes)
        if (
            not isinstance(rows, list)
            or len(rows) != len(codes)
            or any(not isinstance(v, dict) for v in rows)
        ):
            raise RemoteError(None, "codes_response_invalid")
        returned = [(v.get("cisInfo") or {}).get("requestedCis") for v in rows]
        if set(returned) != set(codes):
            raise RemoteError(None, "codes_response_mismatch")
        return rows

    def prepare_true(self, config, payload):
        kind, body, group = (
            payload.get("type"),
            payload.get("document"),
            payload.get("product_group"),
        )
        if kind not in {"LP_INTRODUCE_GOODS", "LK_RECEIPT"}:
            raise InvalidInput("Доступны Производство РФ и Вывод из оборота в JSON")
        if not isinstance(body, dict) or len(encode_json(body)) > 5 * 1024 * 1024:
            raise InvalidInput("Нужен JSON документа размером до 5 МиБ")
        required = (
            {"participant_inn", "producer_inn", "owner_inn", "production_type", "products"}
            if kind == "LP_INTRODUCE_GOODS"
            else {"inn", "action", "action_date", "products"}
        )
        if any(body.get(k) in (None, "", []) for k in required):
            raise InvalidInput("Не заполнены обязательные поля документа")
        participant = (
            body.get("participant_inn") if kind == "LP_INTRODUCE_GOODS" else body.get("inn")
        )
        if participant != config.inn or (
            kind == "LP_INTRODUCE_GOODS" and body["owner_inn"] != config.inn
        ):
            raise InvalidInput("ИНН отправителя/владельца не совпадает с подключением")
        if kind == "LP_INTRODUCE_GOODS":
            if body["production_type"] not in {"OWN_PRODUCTION", "CONTRACT_PRODUCTION"}:
                raise InvalidInput("Проверьте тип производства")
        else:
            if body["action"] == "OTHER" and not body.get("withdrawal_type_other"):
                raise InvalidInput("Укажите описание причины OTHER")
            if body.get("document_type") and any(
                not body.get(k) for k in ("document_number", "document_date")
            ):
                raise InvalidInput("Для первичного документа нужны номер и дата")
        for key in ("action_date", "production_date", "document_date"):
            if key in body:
                try:
                    if date.fromisoformat(body[key]) > date.today():
                        raise ValueError
                except (ValueError, TypeError) as exc:
                    raise InvalidInput(
                        f"{key}: укажите дату ГГГГ-ММ-ДД не позже сегодняшней"
                    ) from exc
        products = body["products"]
        field = "uit_code" if kind == "LP_INTRODUCE_GOODS" else "cis"
        if (
            not isinstance(products, list)
            or not 1 <= len(products) <= 500
            or any(
                not isinstance(v, dict)
                or not v.get(field)
                or v.get("uitu_code")
                or v.get("children")
                for v in products
            )
        ):
            raise InvalidInput(f"Нужно от 1 до 500 единиц товара с {field}")
        codes = [v[field] for v in products]
        if any(cis_from_code(v) != v for v in codes):
            raise InvalidInput("True API требует КИ без криптохвоста")
        statuses = self.check_codes(config, codes, group)
        expected = "APPLIED" if kind == "LP_INTRODUCE_GOODS" else "INTRODUCED"
        for row in statuses:
            info = row.get("cisInfo") or {}
            if (
                row.get("errorCode")
                or info.get("ownerInn") != config.inn
                or info.get("status") != expected
                or info.get("productGroup") != group
                or info.get("packageType") != "UNIT"
                or info.get("statusEx") not in {None, "", "EMPTY"}
            ):
                raise InvalidInput(
                    "Не подтверждены владелец, товарная группа или допустимый статус всех КИ"
                )
        return {
            "kind": "true_document",
            "title": f"{kind} · {len(codes)} КИ",
            "body": {
                "type": kind,
                "product_group": group,
                "document": body,
                "preflight": statuses,
                "targets": [f"cis:{v}" for v in codes],
            },
        }

    def prepare_wire(self, context, document):
        config = self.config(context)
        kind, body = document["kind"], document["body"]
        if kind == "nk_feed":
            cards = self._require_cards(config, list(map(int, body["versions"])))
            if any(digest(v) != body["versions"][str(v["good_id"])] for v in cards):
                raise Conflict("Карточки изменились после предпросмотра; подготовьте пакет заново")
            value = body["entries"]
        elif kind == "nk_sign":
            if not config.certificate:
                raise InvalidInput("Для публикации выберите сертификат УКЭП")
            fresh = self.prepare_signature(
                config,
                {
                    "good_ids": [v["goodId"] for v in body["xmls"]],
                    "publication_agreement": body["publication_agreement"],
                },
            )
            if {v["goodId"]: v["xml"] for v in fresh["body"]["xmls"]} != {
                v["goodId"]: v["xml"] for v in body["xmls"]
            }:
                raise Conflict("XML карточек изменился; подготовьте публикацию заново")
            value = [
                {
                    "goodId": v["goodId"],
                    "base64Xml": base64.b64encode(v["xml"].encode("utf-8")).decode(),
                    "signature": self.signer.sign(
                        v["xml"].encode("utf-8"), config.certificate, detached=True
                    ),
                }
                for v in body["xmls"]
            ]
        elif kind == "true_document":
            if not config.certificate:
                raise InvalidInput("Для документа оборота выберите сертификат УКЭП")
            self.prepare_true(config, body)
            raw = encode_json(body["document"])
            value = {
                "document_format": "MANUAL",
                "type": body["type"],
                "product_document": base64.b64encode(raw).decode(),
                "signature": self.signer.sign(raw, config.certificate, detached=True),
            }
        else:
            return self.prepare_suz_wire(config, document)
        # Authentication finishes before the journal crosses the uncertain I/O boundary.
        self._token(config)
        raw = encode_json(value)
        return {"data": base64.b64encode(raw).decode(), "sha256": digest(value)}

    def send(self, context, document, wire):
        config, kind = self.config(context), document["kind"]
        raw = base64.b64decode(wire["data"], validate=True) if wire.get("data") else None
        if kind == "nk_feed":
            response = unwrap(self._call(config, "nk", "POST", "/v3/feed", body=raw))
            external_id = response.get("feed_id") if isinstance(response, dict) else None
            if (
                isinstance(external_id, bool)
                or not isinstance(external_id, int)
                or external_id <= 0
            ):
                raise RemoteError(None, "acknowledgement_missing")
            return {"state": "accepted", "external_id": str(external_id), "response": response}
        if kind == "nk_sign":
            response = unwrap(
                self._call(config, "nk", "POST", "/v3/feed-product-sign-pkcs", body=raw)
            )
            requested = {v["goodId"] for v in document["body"]["xmls"]}
            signed, errors = response.get("signed", []), response.get("errors", [])
            if (
                not isinstance(signed, list)
                or not isinstance(errors, list)
                or any(not isinstance(v, dict) for v in errors)
            ):
                raise RemoteError(None, "signature_response_invalid")
            failed = [v.get("goodId") for v in errors]
            if (
                len(set(signed + failed)) != len(signed + failed)
                or set(signed + failed) != requested
            ):
                raise RemoteError(None, "signature_response_mismatch")
            return {
                "state": "partial" if signed and errors else "rejected" if errors else "succeeded",
                "response": response,
            }
        if kind == "true_document":
            response = self._call(
                config,
                "true",
                "POST",
                "/lk/documents/create",
                params={"pg": document["body"]["product_group"]},
                body=raw,
            )
            try:
                external_id = uuid_text(response)
            except InvalidInput as exc:
                raise RemoteError(None, "acknowledgement_missing") from exc
            return {
                "state": "accepted",
                "external_id": external_id,
                "response": {"id": external_id},
            }
        return self.send_suz(config, document, wire)

    def poll(self, context, document):
        config, kind = self.config(context), document["kind"]
        if not document.get("external_id"):
            return self.verify_nk(config, document)
        if kind == "nk_feed":
            response = unwrap(
                self._call(
                    config,
                    "nk",
                    "GET",
                    "/v3/feed-status",
                    params={"feed_id": document["external_id"], "verbose": "true"},
                )
            )
            if (
                not isinstance(response, dict)
                or str(response.get("feed_id")) != document["external_id"]
            ):
                raise RemoteError(None, "feed_response_mismatch")
            status = response.get("status")
            errors = response.get("item") or response.get("error_details") or response.get("result")
            state = (
                "rejected"
                if status == "Rejected"
                else ("partial" if errors else "succeeded")
                if status in {"Moderated", "Signed"}
                else "processing"
            )
        elif kind == "true_document":
            external_id = uuid_text(document["external_id"])
            response = self._call(
                config,
                "true4",
                "GET",
                f"/doc/{external_id}/info",
                params={"pg": document["body"]["product_group"], "body": "true", "content": "true"},
            )
            if (
                not isinstance(response, list)
                or len(response) != 1
                or not isinstance(response[0], dict)
            ):
                raise RemoteError(None, "document_response_invalid")
            response = response[0]
            if (
                response.get("senderInn") != config.inn
                or response.get("type") != document["body"]["type"]
                or response.get("number") != external_id
                or not isinstance(response.get("productGroup"), list)
                or document["body"]["product_group"] not in response["productGroup"]
            ):
                raise InvalidInput("Внешний документ не соответствует аккаунту и типу")
            status = response.get("status")
            state = {
                "CHECKED_OK": "succeeded",
                "CHECKED_NOT_OK": "rejected",
                "PARSE_ERROR": "rejected",
            }.get(status, "processing")
        else:
            return self.poll_suz(config, document)
        return {"state": state, "external_status": status, "response": response}

    def verify_nk(self, config, document):
        body, kind = document["body"], document["kind"]
        if kind not in {"nk_feed", "nk_sign"}:
            raise InvalidInput("Нет внешнего ID; повторная отправка запрещена")
        ids = (
            list(map(int, body["versions"]))
            if kind == "nk_feed"
            else [v["goodId"] for v in body["xmls"]]
        )
        cards = self._require_cards(config, ids)
        if kind == "nk_sign":
            verified = all(
                v.get("good_signed")
                and v.get("good_status") == "published"
                and digest(card_content(v)) == body["contents"][str(v["good_id"])]
                for v in cards
            )
        else:
            by_id = {v["good_id"]: v for v in cards}
            verified = True
            for entry in body["entries"]:
                for change in entry["good_attrs"]:
                    values = [
                        v.get("attr_value")
                        for v in by_id[entry["good_id"]].get("good_attrs", [])
                        if str(v.get("attr_id")) == str(change["attr_id"])
                        and v.get("attr_value_type") == change.get("attr_value_type")
                        and v.get("gtin") == change.get("gtin")
                    ]
                    verified &= (
                        (change["attr_value"] not in values)
                        if change.get("delete")
                        else (change["attr_value"] in values)
                    )
            # A pending moderation request cannot be established from current values alone.
            if any(v.get("moderation") for v in body["entries"]):
                verified = False
        if not verified:
            raise Conflict("Текущие данные НК не подтверждают завершение; повтор запрещён")
        return {
            "state": "succeeded",
            "external_status": "verified_current_state",
            "response": {"verified_current_state": True, "cards": cards},
        }

    def reconcile(self, context, document, external_id):
        config = self.config(context)
        if document["kind"] in {"nk_feed", "nk_sign"}:
            return self.verify_nk(config, document)
        if document["kind"] == "suz_codes":
            return self.reconcile_block(config, document, external_id)
        if document["kind"] in {"suz_order", "suz_utilisation", "suz_close"}:
            return self.reconcile_suz_document(config, document, external_id)
        if document["kind"] != "true_document":
            raise InvalidInput("Неподдержанный вид документа для сверки")
        outcome = self.poll(context, {**document, "external_id": uuid_text(external_id)})
        content = outcome["response"].get("content")
        try:
            value = json.loads(content)
        except (ValueError, TypeError):
            try:
                value = json.loads(base64.b64decode(content, validate=True))
            except (ValueError, TypeError, UnicodeError) as exc:
                raise InvalidInput("ГИС МТ не вернул содержимое для точной сверки") from exc
        if value != document["body"]["document"]:
            raise InvalidInput("Содержимое внешнего документа отличается")
        return {**outcome, "external_id": uuid_text(external_id)}
