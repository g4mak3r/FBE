"""Wire encoding and identifiers for the CHZ adapter; never seller-specific values."""

import hashlib
import json
import re
from uuid import UUID

from fbe_flow.core.errors import InvalidInput


def encode_json(value):
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def digest(value):
    return hashlib.sha256(encode_json(value)).hexdigest()


def positive_ids(values, maximum=500):
    if (
        not isinstance(values, list)
        or not 1 <= len(values) <= maximum
        or any(isinstance(v, bool) or not isinstance(v, int) or v <= 0 for v in values)
        or len(set(values)) != len(values)
    ):
        raise InvalidInput(f"Выберите от 1 до {maximum} разных карточек")
    return values


def uuid_text(value):
    try:
        return str(UUID(value))
    except (ValueError, TypeError, AttributeError) as exc:
        raise InvalidInput("Ожидается идентификатор UUID") from exc


def gtin_text(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{14}", value):
        raise InvalidInput("СУЗ требует GTIN строкой из 14 цифр")
    return value


def cis_from_code(value):
    if not isinstance(value, str) or not value or len(value) > 1000:
        raise InvalidInput("Некорректный код маркировки")
    # Do not strip or rewrite the full KM: its exact bytes are needed by SUZ.
    cis = value.removeprefix("]d2").split("\x1d", 1)[0]
    if not re.fullmatch(r"01\d{14}21[!-~]{1,100}", cis):
        raise InvalidInput("Ожидается КИ GS1: 01 + GTIN + 21 + серийный номер")
    return cis
