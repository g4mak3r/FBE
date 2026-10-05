"""Canonical trade items, declared documents and dated classification rules."""

import json
import re
from datetime import date
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import Field, JsonValue, field_validator, model_validator

from fbe_flow.core.models import Contract, Text

Short = Annotated[Text, Field(max_length=240)]
Measure = Annotated[Decimal, Field(gt=0, le=100000000, max_digits=15, decimal_places=6)]


def normalize_gtin(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{8}|[0-9]{12,14}", value):
        raise ValueError("GTIN: 8, 12, 13 или 14 цифр; хранится текстом")
    digits = value.zfill(14)
    total = sum(int(v) * (3 if i % 2 == 0 else 1) for i, v in enumerate(digits[:-1]))
    if (-total) % 10 != int(digits[-1]):
        raise ValueError("Неверная контрольная цифра GTIN")
    return digits


class CatalogContract(Contract):
    @field_validator("*", check_fields=False)
    @classmethod
    def safe_text(cls, value):
        if isinstance(value, str) and re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", value):
            raise ValueError("Недопустимые управляющие символы")
        return value


class CatalogProduct(CatalogContract):
    title: Short
    sku: Short
    family: Short | None = None
    brand: Short | None = None
    manufacturer: Short | None = None
    country: Short | None = None
    description: Annotated[str, Field(max_length=10000)] | None = None
    composition: Annotated[str, Field(max_length=10000)] | None = None
    volume_ml: Measure | None = None
    net_weight_g: Measure | None = None
    gross_weight_g: Measure | None = None
    length_mm: Measure | None = None
    width_mm: Measure | None = None
    height_mm: Measure | None = None
    package_quantity: Annotated[int, Field(strict=True, ge=1, le=1000000)] = 1
    shelf_life_days: Annotated[int, Field(strict=True, ge=1, le=100000)] | None = None
    tnved: Annotated[str, Field(pattern=r"^[0-9]{10}$")] | None = None
    okpd2: Annotated[str, Field(pattern=r"^[0-9]{2}(\.[0-9]{1,3}){0,4}$")] | None = None
    gtins: Annotated[list[str], Field(max_length=100)] = Field(default_factory=list)
    barcodes: Annotated[list[Short], Field(max_length=100)] = Field(default_factory=list)
    product_group: Short | None = None
    marking_attestation: bool | None = None
    archived: bool = False
    attributes: dict[str, JsonValue] = Field(default_factory=dict)

    @field_validator("gtins")
    @classmethod
    def gtin_values(cls, values):
        return list(dict.fromkeys(normalize_gtin(v) for v in values))

    @field_validator("attributes")
    @classmethod
    def attribute_limits(cls, value):
        if len(value) > 300 or len(json.dumps(value, ensure_ascii=False, allow_nan=False)) > 100000:
            raise ValueError("Слишком много характеристик")
        if any(not k.strip() or len(k) > 240 for k in value):
            raise ValueError("Имя характеристики: от 1 до 240 символов")
        if any(re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", k) for k in value):
            raise ValueError("Недопустимые символы в имени характеристики")
        if any(len(json.dumps(v, ensure_ascii=False)) > 32767 for v in value.values()):
            raise ValueError("Значение характеристики слишком длинное для обмена XLSX")
        return value

    @model_validator(mode="after")
    def weights(self):
        if self.net_weight_g and self.gross_weight_g and self.net_weight_g > self.gross_weight_g:
            raise ValueError("Масса нетто превышает массу брутто")
        return self


class CatalogDocument(CatalogContract):
    kind: Literal["declaration", "certificate", "sgr", "other"]
    number: Short
    issued_on: date | None = None
    expires_on: date | None = None
    issuer: Short | None = None
    scope: Annotated[str, Field(max_length=10000)] | None = None
    status: Literal["declared", "verified", "revoked"] = "declared"
    verification_note: Annotated[str, Field(max_length=2000)] | None = None
    product_ids: Annotated[list[str], Field(max_length=100000)] = Field(default_factory=list)

    @model_validator(mode="after")
    def dates(self):
        if self.issued_on and self.expires_on and self.issued_on > self.expires_on:
            raise ValueError("Дата выдачи позже окончания действия документа")
        if self.status == "verified" and not self.verification_note:
            raise ValueError("Укажите основание ручной проверки документа")
        if len(self.product_ids) != len(set(self.product_ids)):
            raise ValueError("Товары документа повторяются")
        return self


class CatalogBatch(CatalogContract):
    product_id: Short
    name: Short
    quantity: Annotated[int, Field(strict=True, ge=1, le=10000000)]
    manufactured_on: date | None = None
    expires_on: date | None = None
    note: Annotated[str, Field(max_length=2000)] | None = None

    @model_validator(mode="after")
    def dates(self):
        if self.manufactured_on and self.expires_on and self.manufactured_on > self.expires_on:
            raise ValueError("Дата производства позже срока годности")
        return self


class RuleCondition(CatalogContract):
    field: Annotated[Text, Field(max_length=251)]
    operator: Literal["eq", "ne", "gt", "ge", "lt", "le"]
    value: JsonValue

    @field_validator("field")
    @classmethod
    def known_field(cls, value):
        if value not in CatalogProduct.model_fields and not value.startswith("attributes."):
            raise ValueError("Неизвестное поле условия")
        if value == "attributes.":
            raise ValueError("Укажите имя характеристики")
        return value


class ClassificationRule(CatalogContract):
    title: Short
    product_group: Short
    marking_required: bool = True
    tnved_prefixes: Annotated[list[str], Field(min_length=1, max_length=100)]
    okpd2_prefixes: Annotated[list[str], Field(max_length=100)] = Field(default_factory=list)
    conditions: Annotated[list[RuleCondition], Field(max_length=30)] = Field(default_factory=list)
    valid_from: date
    valid_until: date | None = None
    source_url: Annotated[str, Field(pattern=r"^https?://", max_length=2000)]
    source_note: Annotated[Text, Field(max_length=2000)]
    priority: Annotated[int, Field(strict=True, ge=0, le=10000)] = 0
    enabled: bool = True

    @field_validator("tnved_prefixes")
    @classmethod
    def prefixes(cls, values):
        if any(not re.fullmatch(r"[0-9]{2,10}", v) for v in values):
            raise ValueError("Префикс ТН ВЭД: от 2 до 10 цифр")
        return list(dict.fromkeys(values))

    @field_validator("okpd2_prefixes")
    @classmethod
    def okpd_prefixes(cls, values):
        if any(not re.fullmatch(r"[0-9]{2}(\.[0-9]{1,3}){0,4}", v) for v in values):
            raise ValueError("Неверный префикс ОКПД2")
        return list(dict.fromkeys(values))

    @model_validator(mode="after")
    def dates(self):
        if self.valid_until and self.valid_until < self.valid_from:
            raise ValueError("Дата окончания правила раньше начала")
        return self
