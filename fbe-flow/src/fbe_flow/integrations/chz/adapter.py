import json
import re
import time
from datetime import UTC, datetime
from threading import Lock
from typing import Literal

from pydantic import Field

from fbe_flow.core.credentials import CredentialVault, credential_owner
from fbe_flow.core.errors import InvalidInput
from fbe_flow.core.models import (
    AccountInfo,
    Contract,
    NormalizedBatch,
    OperationResult,
    OperationSpec,
    Product,
)
from fbe_flow.integrations.chz.http import HttpTransport, RemoteError, redact
from fbe_flow.integrations.chz.workflows import ChzWorkflows

# These are protocol addresses/types, not seller categories, products or warehouse IDs.
ENDPOINTS = {
    "production": {
        "nk": "https://xn--80aqu.xn----7sbabas4ajkhfocclk9d3cvfsa.xn--p1ai",
        "true": "https://markirovka.crpt.ru/api/v3/true-api",
        "true4": "https://markirovka.crpt.ru/api/v4/true-api",
        "suz": "https://suzgrid.crpt.ru",
    },
    "sandbox": {
        "nk": "https://api.nk.sandbox.crptech.ru",
        "true": "https://markirovka.sandbox.crptech.ru/api/v3/true-api",
        "true4": "https://markirovka.sandbox.crptech.ru/api/v4/true-api",
        "suz": "https://suz.sandbox.crptech.ru",
    },
}
SPECS = [
    ("account.refresh", "Обновить данные аккаунта"),
    ("nk.sync", "Синхронизировать каталог"),
    ("nk.references", "Обновить справочники НК"),
    ("nk.lookup", "Найти карточку по GTIN"),
    ("codes.check", "Проверить статусы кодов"),
    ("suz.status", "Проверить заказ СУЗ"),
    ("suz.blocks", "Получить список выданных блоков"),
    ("suz.receipt", "Прочитать квитанцию СУЗ"),
    ("document.submit", "Отправить подготовленный документ"),
    ("document.poll", "Проверить обработку документа"),
]


class ChzConfig(Contract):
    environment: Literal["production", "sandbox"] = "sandbox"
    inn: str = Field(pattern=r"^\d{10}(\d{2})?$")
    credential_ref: str
    certificate: str = Field(default="", pattern=r"^([a-fA-F0-9]{40})?$")
    oms_id: str = Field(default="", pattern=r"^([a-fA-F0-9-]{36})?$")
    oms_connection: str = Field(default="", pattern=r"^([a-fA-F0-9-]{36})?$")


def unwrap(data):
    if isinstance(data, dict) and "result" in data:
        return data["result"]
    return data


class ChzAdapter(ChzWorkflows):
    key = "chz"
    label = "Честный Знак"

    @staticmethod
    def urls(config):
        return ENDPOINTS[config.environment]

    @staticmethod
    def supported_operations():
        return [OperationSpec(key=key, label=label).model_dump() for key, label in SPECS]

    def __init__(self, vault: CredentialVault, signer, transport=None):
        self.vault = vault
        self.signer = signer
        self.http = transport or HttpTransport()
        self._tokens = {}
        self._suz_tokens = {}
        self._lock = Lock()

    def validate_binding(self, seller_id, config):
        parsed = ChzConfig.model_validate(config)
        self.vault.get(seller_id, parsed.credential_ref)

    def config(self, context):
        self.validate_binding(context.seller_id, context.config)
        config = ChzConfig.model_validate(context.config)
        if context.external_account_id != f"{config.environment}:{config.inn}":
            raise InvalidInput("Параметры не соответствуют подключенному аккаунту")
        return config

    def _token(self, config):
        secrets = self.vault.get(credential_owner(config.credential_ref), config.credential_ref)
        key = (config.credential_ref, config.environment, config.inn, config.certificate)
        with self._lock:
            cached = self._tokens.get(key)
            if cached and cached[1] > time.monotonic():
                return cached[0]
            if config.certificate:
                base = ENDPOINTS[config.environment]["true"]
                challenge = self.http.request("GET", base + "/auth/key").data
                if not isinstance(challenge, dict) or not all(
                    isinstance(challenge.get(k), str) for k in ("uuid", "data")
                ):
                    raise RemoteError(None, "auth_response_invalid")
                signature = self.signer.sign(
                    challenge["data"].encode("utf-8"), config.certificate, detached=False
                )
                try:
                    response = self.http.request(
                        "POST",
                        base + "/auth/simpleSignIn",
                        body={
                            "uuid": challenge["uuid"],
                            "data": signature,
                            "inn": config.inn,
                            "unitedToken": True,
                        },
                    ).data
                except RemoteError as exc:
                    exc.details = redact(exc.details, [signature, challenge["data"]])
                    raise
                if not isinstance(response, dict):
                    raise RemoteError(None, "auth_response_invalid")
                token = response.get("uuidToken") or response.get("token")
                if not isinstance(token, str) or not token:
                    raise RemoteError(None, "auth_response_invalid")
                # Short cache bound. The server remains authoritative about token validity.
                lifetime = 25 * 60
                if response.get("expireDate"):
                    try:
                        expiry = datetime.fromisoformat(
                            response["expireDate"].replace("Z", "+00:00")
                        )
                        lifetime = min(lifetime, (expiry - datetime.now(UTC)).total_seconds() - 30)
                    except (ValueError, TypeError, AttributeError) as exc:
                        raise RemoteError(None, "auth_response_invalid") from exc
                    if lifetime <= 0:
                        raise RemoteError(None, "auth_token_expired")
                self._tokens[key] = (token, time.monotonic() + lifetime)
                return token
            if token := secrets.get("true_token"):
                return token
            raise InvalidInput("Выберите УКЭП или сохраните действующий токен True API")

    def _call(self, config, system, method, path, *, params=None, body=None, headers=None):
        token = self._client_token(config) if system == "suz" else self._token(config)
        if system == "suz":
            from fbe_flow.integrations.chz.formats import uuid_text

            params = {**(params or {}), "omsId": uuid_text(config.oms_id)}
        auth = {"clientToken": token} if system == "suz" else {"Authorization": f"Bearer {token}"}
        hidden = [token, (headers or {}).get("X-Signature", "")]
        request_value = json.loads(body) if isinstance(body, bytes) else body
        entries = request_value if isinstance(request_value, list) else [request_value]
        hidden.extend(
            v["signature"]
            for v in entries
            if isinstance(v, dict) and isinstance(v.get("signature"), str)
        )
        try:
            reply = self.http.request(
                method,
                ENDPOINTS[config.environment][system] + path,
                params=params,
                headers={**auth, **(headers or {})},
                body=body,
            )
        except RemoteError as exc:
            exc.details = redact(exc.details, hidden)
            if exc.status == 401:
                with self._lock:
                    self._tokens.clear()
                    self._suz_tokens.clear()
            raise
        value = redact(reply.data, hidden)
        if isinstance(value, dict) and (value.get("error") or value.get("error_code")):
            raise RemoteError(400, "upstream_api_error", value)
        return value

    def refresh_document(self, context, document):
        body = document["body"]
        ids = (
            list(map(int, body["versions"]))
            if document["kind"] == "nk_feed"
            else [v["goodId"] for v in body["xmls"]]
        )
        cards = self._own_cards(self.config(context), ids)
        return NormalizedBatch(products=tuple(self._product(v) for v in cards))

    def account(self, config):
        value = self._call(config, "true", "GET", "/participants", params={"inns": config.inn})
        items = value if isinstance(value, list) else [value]
        account = next(
            (v for v in items if isinstance(v, dict) and v.get("inn") == config.inn), None
        )
        # Public participant information alone is insufficient to verify a token's organisation.
        if not account or not account.get("role") or not account.get("is_registered"):
            raise InvalidInput("Токен не подтвердил доступ к организации с этим ИНН")
        return account

    def describe(self, config):
        parsed = ChzConfig.model_validate(config)
        account = self.account(parsed)
        if not account.get("productGroups"):
            raise InvalidInput("К аккаунту не подключены товарные группы")
        # Probe the authenticated NK interface. Permissions are rechecked during execution.
        self._call(parsed, "nk", "GET", "/v3/categories")
        page = unwrap(
            self._call(
                parsed, "nk", "GET", "/v3/etagslist", params={"offset": 0, "owner_inn": parsed.inn}
            )
        )
        if not isinstance(page, dict) or not isinstance(page.get("goods"), list):
            raise RemoteError(None, "nk_response_invalid")
        if parsed.oms_id or parsed.oms_connection:
            self.suz_ping(parsed)
        return AccountInfo(
            external_account_id=f"{parsed.environment}:{account['inn']}",
            operations=tuple(
                OperationSpec.model_validate(item) for item in self.supported_operations()
            ),
        )

    def _own_cards(self, config, ids):
        result = []
        for start in range(0, len(ids), 25):
            value = unwrap(
                self._call(
                    config,
                    "nk",
                    "GET",
                    "/v3/feed-product",
                    params={"good_ids": ";".join(map(str, ids[start : start + 25]))},
                )
            )
            requested = set(map(str, ids[start : start + 25]))
            if not isinstance(value, list) or any(
                not isinstance(v, dict) or str(v.get("good_id")) not in requested for v in value
            ):
                raise RemoteError(None, "nk_response_invalid")
            result.extend(value)
        if len({str(v["good_id"]) for v in result}) != len(result):
            raise RemoteError(None, "nk_duplicate_cards")
        return result

    @staticmethod
    def _product(card, etag=None, own=True):
        identifiers = {}
        for item in card.get("identified_by", []):
            if item.get("type") and item.get("value"):
                identifiers.setdefault(str(item["type"]), []).append(str(item["value"]))
        if card.get("gtin"):
            values = identifiers.setdefault("gtin", [])
            if str(card["gtin"]) not in values:
                values.append(str(card["gtin"]))
        return Product(
            external_id=str(card["good_id"]),
            title=card.get("good_name") or str(card["good_id"]),
            identifiers=identifiers,
            category={"categories": card.get("categories", [])},
            attributes={"source": card, "etag": etag, "own_card": own},
        )

    @staticmethod
    def catalog_variants(record):
        from fbe_flow.integrations.catalog import variants

        return variants(record, "chz")

    def catalog_check(self, context, product):
        from fbe_flow.integrations.catalog import gtins

        config = self.config(context)
        account = self.account(config)
        groups = account.get("productGroups", [])
        if not isinstance(groups, list) or any(not isinstance(v, str) for v in groups):
            raise RemoteError(None, "chz_account_groups_invalid")
        issues, cards, registry = [], [], []
        if product.get("tnved"):
            page = self._call(
                config,
                "true4",
                "POST",
                "/tn-ved/search",
                body={
                    "tnveds": [product["tnved"]],
                    "page": 0,
                    "limit": 1000,
                },
            )
            if not isinstance(page, dict) or not isinstance(page.get("tnveds"), list):
                raise RemoteError(None, "chz_tnved_response_invalid")
            if any(not isinstance(v, dict) for v in page["tnveds"]):
                raise RemoteError(None, "chz_tnved_response_invalid")
            registry = [v for v in page["tnveds"] if v.get("tnved") == product["tnved"]]
            if page.get("total", 0) > len(page["tnveds"]) and not page.get("last"):
                raise RemoteError(None, "chz_tnved_response_incomplete")
            if not registry:
                issues.append(
                    {
                        "code": "tnved_not_listed",
                        "message": "ТН ВЭД не найден в справочнике ЧЗ. Это не подтверждает "
                        "освобождение от маркировки.",
                    }
                )
        requested, ready = product["gtins"], {}
        if requested:
            result = self._call(
                config, "true4", "POST", "/product/info", body={"gtins": requested, "rdInfo": True}
            )
            if not isinstance(result, dict) or not isinstance(result.get("results"), list):
                raise RemoteError(None, "chz_product_info_invalid")
            for item in result["results"]:
                if (
                    not isinstance(item, dict)
                    or item.get("gtin") not in requested
                    or item["gtin"] in ready
                ):
                    raise RemoteError(None, "chz_product_info_invalid")
                ready[item["gtin"]] = item
        else:
            issues.append({"code": "gtin_missing", "message": "В товаре не указан GTIN"})
        for gtin in requested:
            value = unwrap(self._call(config, "nk", "GET", "/v3/product", params={"gtin": gtin}))
            if not isinstance(value, list) or any(not isinstance(v, dict) for v in value):
                raise RemoteError(None, "nk_response_invalid")
            found = []
            for candidate in value:
                identifiers = candidate.get("identified_by", [])
                if not isinstance(identifiers, list) or any(
                    not isinstance(v, dict) for v in identifiers
                ):
                    raise RemoteError(None, "nk_response_invalid")
                codes = [str(candidate.get("gtin", ""))] + [
                    str(v.get("value", "")) for v in identifiers if v.get("type") == "gtin"
                ]
                if gtin in gtins(codes):
                    found.append(candidate)
            if len(found) != 1:
                issues.append(
                    {
                        "code": "nk_card_missing" if not found else "nk_card_ambiguous",
                        "gtin": gtin,
                        "message": "Карточка НК не найдена однозначно по GTIN",
                    }
                )
                continue
            card = found[0]
            parsed = {
                "gtin": gtin,
                "good_id": str(card.get("good_id", "")),
                "name": card.get("good_name"),
                "categories": card.get("categories", []),
                "product_groups": [],
                "tnved": [],
                "okpd2": [],
                "ready_for_circulation": gtin in ready,
                "permits": ready.get(gtin, {}).get("permits", {}),
            }
            if ready.get(gtin, {}).get("productGroup"):
                parsed["product_groups"] = [ready[gtin]["productGroup"]]
            attributes = card.get("good_attrs", [])
            if not isinstance(attributes, list) or any(not isinstance(a, dict) for a in attributes):
                raise RemoteError(None, "nk_response_invalid")
            for attr in attributes:
                name = re.sub(r"[^а-яa-z0-9]", "", str(attr.get("attr_name", "")).lower())
                if name in {"кодтнвэд", "тнвэд", "кодокпд2", "окпд2"}:
                    field = "tnved" if "тнвэд" in name else "okpd2"
                    parsed[field].append(str(attr.get("attr_value", "")))
            if not parsed["product_groups"]:
                issues.append(
                    {
                        "code": "nk_group_unconfirmed",
                        "gtin": gtin,
                        "message": "True API не подтвердил группу и готовность карточки к обороту",
                    }
                )
            for field in ("tnved", "okpd2"):
                if product.get(field) and not parsed[field]:
                    issues.append(
                        {
                            "code": "nk_" + field + "_unconfirmed",
                            "gtin": gtin,
                            "message": "В ответе НК не подтверждено поле " + field,
                        }
                    )
            cards.append(parsed)
        return {
            "account_groups": groups,
            "tnved_registry": registry,
            "tnved_groups": sorted({v["pg"] for v in registry if isinstance(v.get("pg"), str)}),
            "cards": cards,
            "issues": issues,
            "environment": config.environment,
            "source_url": "https://markirovka.crpt.ru/api/v4/true-api",
        }

    def _sync(self, config, payload):
        offset = payload.get("offset", 0)
        if isinstance(offset, bool) or not isinstance(offset, int) or not 0 <= offset <= 10000000:
            raise InvalidInput("Неверная страница синхронизации")
        page = unwrap(
            self._call(
                config,
                "nk",
                "GET",
                "/v3/etagslist",
                params={"offset": offset, "owner_inn": config.inn},
            )
        )
        if not isinstance(page, dict) or not isinstance(page.get("goods"), list):
            raise RemoteError(None, "nk_response_invalid")
        goods = page["goods"]
        if (
            len(goods) > 100
            or any(
                not isinstance(g, dict)
                or isinstance(g.get("good_id"), bool)
                or not isinstance(g.get("good_id"), int)
                or g["good_id"] <= 0
                or not isinstance(g.get("etag"), (str, type(None)))
                for g in goods
            )
            or len({g["good_id"] for g in goods}) != len(goods)
        ):
            raise RemoteError(None, "nk_response_invalid")
        known = payload.get("known_etags", {})
        changed = [
            g
            for g in goods
            if payload.get("force")
            or not g.get("etag")
            or known.get(str(g["good_id"])) != g.get("etag")
        ]
        hashes = {str(g["good_id"]): g.get("etag") for g in goods}
        cards = self._own_cards(config, [g["good_id"] for g in changed]) if changed else []
        next_offset = offset + len(goods)
        total = page.get("total")
        if (
            isinstance(total, bool)
            or not isinstance(total, int)
            or total < next_offset
            or page.get("offset") != offset
            or page.get("goods_count") != len(goods)
            or page.get("last_product_number") != next_offset
            or (not goods and next_offset < total)
        ):
            raise RemoteError(None, "nk_pagination_invalid")
        if payload.get("expected_total", total) != total:
            raise RemoteError(None, "nk_catalog_changed")
        returned = {str(c["good_id"]) for c in cards}
        changed_ids = {str(g["good_id"]) for g in changed}
        return OperationResult(
            batch=NormalizedBatch(
                products=tuple(self._product(c, hashes[str(c["good_id"])]) for c in cards)
            ),
            data={
                "sync": {
                    "offset": next_offset,
                    "total": total,
                    "complete": next_offset >= total,
                    "force": bool(payload.get("force")),
                    "run_id": payload.get("run_id"),
                    "cards": [
                        {
                            "external_id": str(g["good_id"]),
                            "etag": g.get("etag"),
                            "detail_available": str(g["good_id"]) in returned
                            or str(g["good_id"]) not in changed_ids,
                        }
                        for g in goods
                    ],
                }
            },
        )

    def _references(self, config):
        return {
            "categories": unwrap(self._call(config, "nk", "GET", "/v3/categories")),
            "account": self.account(config),
        }

    def execute(self, context, operation, payload):
        config = self.config(context)
        if operation == "account.refresh":
            return OperationResult(data={"snapshots": {"account": self.account(config)}})
        if operation == "nk.sync":
            return self._sync(config, payload)
        if operation == "nk.references":
            return OperationResult(data={"snapshots": self._references(config)})
        if operation == "nk.lookup":
            gtin = payload.get("gtin")
            if not isinstance(gtin, str) or not re.fullmatch(r"\d{8}|\d{12,14}", gtin):
                raise InvalidInput("Введите GTIN строкой из 8, 12, 13 или 14 цифр")
            cards = unwrap(self._call(config, "nk", "GET", "/v3/product", params={"gtin": gtin}))
            if not isinstance(cards, list) or any(not isinstance(c, dict) for c in cards):
                raise RemoteError(None, "nk_response_invalid")
            return OperationResult(data={"lookup": cards})
        if operation == "codes.check":
            return OperationResult(
                data={
                    "codes": self.check_codes(
                        config, payload.get("codes"), payload.get("product_group")
                    )
                }
            )
        if operation == "suz.status":
            return OperationResult(
                data={"snapshots": {"suz_status": self.suz_status(config, payload)}}
            )
        if operation == "suz.blocks":
            return OperationResult(
                data={"snapshots": {"suz_blocks": self.suz_blocks(config, payload)}}
            )
        if operation == "suz.receipt":
            return OperationResult(
                data={
                    "snapshots": {
                        "suz_receipt": self.suz_receipt(config, payload.get("receipt_id"))
                    }
                }
            )
        raise InvalidInput("Операция требует подготовленного документа или не поддерживается")
