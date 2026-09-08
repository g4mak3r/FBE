from __future__ import annotations

import base64
import hashlib
import json
from datetime import datetime, timezone
from typing import Any

# Official WB API JWT category bit positions (field "s").
CATEGORY_BITS: dict[int, str] = {
    1: "Контент",
    2: "Аналитика",
    3: "Цены и скидки",
    4: "Маркетплейс",
    5: "Статистика",
    6: "Продвижение",
    7: "Вопросы и отзывы",
    9: "Чат с покупателями",
    10: "Поставки",
    11: "Возвраты покупателями",
    12: "Документы",
    13: "Финансы",
    16: "Пользователи",
}

TOKEN_TYPES = {
    1: "Базовый",
    2: "Тестовый",
    3: "Персональный",
    4: "Сервисный",
}

SOURCE_SPECS: dict[str, dict[str, Any]] = {
    "statistics": {
        "label": "Статистика",
        "required_category": "Статистика",
        "token_genitive": "статистики",
        "required_bit": 5,
        "settings_key": "statistics_token",
        "sync_sources": ("orders", "sales"),
    },
    "finance": {
        "label": "Финансы",
        "required_category": "Финансы",
        "token_genitive": "финансов",
        "required_bit": 13,
        "settings_key": "finance_token",
        "sync_sources": ("finance",),
    },
    "promotion": {
        "label": "Продвижение",
        "required_category": "Продвижение",
        "token_genitive": "продвижения",
        "required_bit": 6,
        "settings_key": "promotion_token",
        "sync_sources": ("ads",),
    },
}


def normalize_token(token: str) -> str:
    value = str(token or "").strip().strip('"').strip("'")
    if value.lower().startswith("bearer "):
        value = value[7:].strip()
    return value


def token_fingerprint(token: str) -> str:
    value = normalize_token(token)
    if not value:
        return ""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:20]


def _decode_segment(segment: str) -> dict[str, Any]:
    padding = "=" * ((4 - len(segment) % 4) % 4)
    raw = base64.urlsafe_b64decode((segment + padding).encode("ascii"))
    data = json.loads(raw.decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("JWT payload должен быть JSON-объектом")
    return data


def decode_wb_token(token: str) -> dict[str, Any]:
    value = normalize_token(token)
    result: dict[str, Any] = {
        "present": bool(value),
        "valid_jwt": False,
        "fingerprint": token_fingerprint(value),
        "categories": [],
        "category_bits": [],
        "read_only": False,
        "expired": False,
        "expires_at": "",
        "token_type": "Не определен",
        "seller_id": "",
        "error": "",
        "payload": {},
    }
    if not value:
        result["error"] = "Токен не задан"
        return result
    parts = value.split(".")
    if len(parts) != 3:
        result["error"] = "Строка не похожа на JWT-токен WB"
        return result
    try:
        payload = _decode_segment(parts[1])
        result["payload"] = payload
        mask = int(payload.get("s") or 0)
        category_bits = [bit for bit in sorted(CATEGORY_BITS) if mask & (1 << bit)]
        result["category_bits"] = category_bits
        result["categories"] = [CATEGORY_BITS[bit] for bit in category_bits]
        result["read_only"] = bool(mask & (1 << 30))
        result["token_type"] = TOKEN_TYPES.get(int(payload.get("acc") or 0), "Не определен")
        result["seller_id"] = str(payload.get("sid") or "")
        exp = int(payload.get("exp") or 0)
        if exp:
            expires = datetime.fromtimestamp(exp, tz=timezone.utc).astimezone()
            result["expires_at"] = expires.strftime("%d.%m.%Y %H:%M")
            result["expired"] = expires <= datetime.now().astimezone()
        result["valid_jwt"] = True
        return result
    except Exception as exc:
        result["error"] = f"Не удалось декодировать JWT: {exc}"
        return result


def _mask_token(token: str) -> str:
    value = normalize_token(token)
    if not value:
        return "не задан"
    if len(value) <= 12:
        return "задан"
    return "задан"


def inspect_tokens(config: dict[str, Any], settings: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Inspect one shared FBE token against every Economy API contour.

    Credentials are resolved by :class:`settings.Settings`; this module only
    consumes the resulting shared token. ``settings`` contains non-secret
    Economy UI state such as cached connection checks.
    """
    default_token = normalize_token(str(config.get("wb_token") or ""))

    checks = settings.get("token_checks") if isinstance(settings.get("token_checks"), dict) else {}
    decoded = decode_wb_token(default_token)
    result: dict[str, dict[str, Any]] = {}

    for source, spec in SOURCE_SPECS.items():
        token = default_token
        required_bit = int(spec["required_bit"])
        has_scope = required_bit in decoded.get("category_bits", [])
        cached = checks.get(source) if isinstance(checks.get(source), dict) else {}
        cache_matches = bool(
            decoded.get("fingerprint")
            and cached.get("fingerprint") == decoded.get("fingerprint")
        )

        status = "ready"
        status_label = "Готов к проверке"
        status_class = "ready"
        message = f"Единый JWT содержит категорию «{spec['required_category']}». Выполните проверку подключения."
        can_sync = True
        can_check = True

        if not decoded["present"]:
            status = "missing"
            status_label = "Токен не задан"
            status_class = "missing"
            message = "Задайте единый Персональный токен FBE."
            can_sync = False
            can_check = False
        elif not decoded["valid_jwt"]:
            status = "invalid"
            status_label = "Некорректный токен"
            status_class = "danger"
            message = decoded["error"]
            can_sync = False
            can_check = False
        elif decoded["expired"]:
            status = "expired"
            status_label = "Срок истек"
            status_class = "danger"
            message = f"Создайте новый токен. Срок действия закончился {decoded['expires_at']}."
            can_sync = False
            can_check = False
        elif not has_scope:
            status = "no_access"
            status_label = "Нет категории"
            status_class = "danger"
            message = (
                f"Единый токен FBE не имеет категории «{spec['required_category']}». "
                "Создайте Персональный токен WB с нужными категориями."
            )
            can_sync = False
            can_check = False
        elif source == "finance" and decoded.get("token_type") not in {"Персональный", "Сервисный"}:
            status = "wrong_type"
            status_label = "Нужен Персональный"
            status_class = "danger"
            message = "Новый Finance API требует Персональный или Сервисный токен. Для локальной FBE выберите Персональный."
            can_sync = False
            can_check = False
        elif cache_matches:
            cached_status = str(cached.get("status") or "")
            if cached_status == "connected":
                status = "connected"
                status_label = "Подключено"
                status_class = "connected"
                message = str(cached.get("message") or "Подключение к WB API подтверждено.")
            elif cached_status == "no_access":
                status = "no_access"
                status_label = "Нет доступа"
                status_class = "danger"
                message = str(cached.get("message") or "WB API отклонил токен.")
                can_sync = False
            elif cached_status == "error":
                status = "error"
                status_label = "Ошибка проверки"
                status_class = "warning"
                message = str(cached.get("message") or "Не удалось проверить подключение.")

        result[source] = {
            "source": source,
            "label": spec["label"],
            "required_category": spec["required_category"],
            "required_bit": required_bit,
            "sync_sources": list(spec["sync_sources"]),
            "token_mask": _mask_token(token),
            "origin": "Единый токен FBE",
            "using_default": True,
            "fingerprint": decoded["fingerprint"],
            "categories": decoded["categories"],
            "read_only": decoded["read_only"],
            "token_type": decoded["token_type"],
            "expires_at": decoded["expires_at"],
            "expired": decoded["expired"],
            "status": status,
            "status_label": status_label,
            "status_class": status_class,
            "message": message,
            "can_sync": can_sync,
            "can_check": can_check,
            "checked_at": str(cached.get("checked_at") or "") if cache_matches else "",
            "request_id": str(cached.get("request_id") or "") if cache_matches else "",
            "http_status": cached.get("http_status") if cache_matches else None,
        }
    return result


def source_for_sync(sync_source: str) -> str:
    if sync_source in {"orders", "sales"}:
        return "statistics"
    if sync_source == "finance":
        return "finance"
    if sync_source == "ads":
        return "promotion"
    raise ValueError(f"Неизвестный источник синхронизации: {sync_source}")
