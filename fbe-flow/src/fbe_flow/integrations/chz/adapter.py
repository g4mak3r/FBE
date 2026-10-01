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

# These are protocol addresses/types, not seller categories, products or warehouse IDs.
ENDPOINTS = {
    "production": {
        "nk": "https://xn--80aqu.xn----7sbabas4ajkhfocclk9d3cvfsa.xn--p1ai",
        "true": "https://markirovka.crpt.ru/api/v3/true-api",
    },
    "sandbox": {
        "nk": "https://api.nk.sandbox.crptech.ru",
        "true": "https://markirovka.sandbox.crptech.ru/api/v3/true-api",
    },
}
SPECS = [
    ("account.refresh", "Обновить данные аккаунта"),
    ("nk.sync", "Синхронизировать каталог"),
    ("nk.references", "Обновить справочники НК"),
    ("nk.lookup", "Найти карточку по GTIN"),
]


class ChzConfig(Contract):
    environment: Literal["production", "sandbox"] = "sandbox"
    inn: str = Field(pattern=r"^\d{10}(\d{2})?$")
    credential_ref: str
    certificate: str = Field(default="", pattern=r"^([a-fA-F0-9]{40})?$")


def unwrap(data):
    if isinstance(data, dict) and "result" in data:
        return data["result"]
    return data


class ChzAdapter:
    key = "chz"
    label = "Честный Знак"

    def __init__(self, vault: CredentialVault, signer, transport=None):
        self.vault = vault
        self.signer = signer
        self.http = transport or HttpTransport()
        self._tokens = {}
        self._lock = Lock()

    def validate_binding(self, seller_id, config):
        parsed = ChzConfig.model_validate(config)
        self.vault.get(seller_id, parsed.credential_ref)

    def config(self, context):
        self.validate_binding(context.seller_id, context.config)
        return ChzConfig.model_validate(context.config)

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
        token = self._token(config)
        try:
            reply = self.http.request(
                method,
                ENDPOINTS[config.environment][system] + path,
                params=params,
                headers={"Authorization": f"Bearer {token}", **(headers or {})},
                body=body,
            )
        except RemoteError as exc:
            exc.details = redact(exc.details, [token])
            if exc.status == 401:
                with self._lock:
                    self._tokens.clear()
            raise
        value = redact(reply.data, [token])
        if isinstance(value, dict) and (value.get("error") or value.get("error_code")):
            raise RemoteError(reply.status, "nk_api_error", value)
        return value

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
        return AccountInfo(
            external_account_id=f"{parsed.environment}:{account['inn']}",
            operations=tuple(OperationSpec(key=key, label=label) for key, label in SPECS),
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
        config = ChzConfig.model_validate(context.config)
        self.validate_binding(context.seller_id, context.config)
        if context.external_account_id != f"{config.environment}:{config.inn}":
            raise InvalidInput("Параметры не соответствуют подключенному аккаунту")
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
        raise InvalidInput("Операция недоступна в контуре чтения НК")
