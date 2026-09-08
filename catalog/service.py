from __future__ import annotations

import json
from pathlib import Path
from typing import Any, ClassVar

from .repository import get_catalog_revision, list_all_products, list_name_formats, list_names, list_samples


def _clean(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


class ProductCatalog:
    """SQLite-backed compatibility facade used by the existing FBE services.

    Existing printing, WB and marking code can keep calling find_by_article().
    The data itself is read only from fbe.db. A revision-aware in-process snapshot
    avoids reloading tens of thousands of products for every lookup.
    """

    _snapshot_cache: ClassVar[
        dict[str, tuple[int, dict[str, dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[str]]]
    ] = {}

    def __init__(self, db_path: str | Path, columns: dict[str, str] | None = None, **_: Any):
        self.path = Path(db_path)
        self.columns = columns or {}
        self.by_article: dict[str, dict[str, Any]] = {}
        self.error: str | None = None
        self.warnings: list[str] = []
        self.headers: list[str] = list(self.columns.values())
        self._samples: list[dict[str, Any]] = []
        self._names: list[dict[str, Any]] = []
        self._name_formats: list[dict[str, Any]] = []
        self.load()

    def _cache_key(self) -> str:
        mapping = "|".join(f"{key}={value}" for key, value in sorted(self.columns.items()))
        return f"{self.path.resolve()}::{mapping}"

    def load(self) -> None:
        self.by_article.clear()
        self.error = None
        self.warnings = []
        try:
            revision = get_catalog_revision(self.path)
            cache_key = self._cache_key()
            cached = self._snapshot_cache.get(cache_key)
            if cached and cached[0] == revision:
                self.by_article = cached[1]
                self._samples = cached[2]
                self._names = cached[3]
                self._name_formats = cached[4]
                self.warnings = list(cached[5])
                return

            by_article: dict[str, dict[str, Any]] = {}
            warnings: list[str] = []
            for row in list_all_products(self.path, active_only=True):
                item = dict(row)
                item["excel_kiz"] = "Да" if int(item.get("kiz_required") or 0) else "Нет"
                item["kiz_required"] = bool(item.get("kiz_required"))
                try:
                    raw = json.loads(str(item.get("raw_json") or "{}"))
                    if not isinstance(raw, dict):
                        raw = {}
                except Exception:
                    raw = {}
                # BarTender-compatible names are generated from the current DB fields.
                raw_excel = dict(raw)
                for internal_name, header in self.columns.items():
                    value = item.get(internal_name)
                    if internal_name == "excel_kiz":
                        value = item["excel_kiz"]
                    raw_excel[header] = _clean(value)
                item["raw_excel"] = raw_excel
                item["display_name"] = self._build_display_name(item)
                by_article[str(item["seller_article"])] = item

            samples = list_samples(self.path, active_only=True)
            names = list_names(self.path, active_only=True)
            name_formats = list_name_formats(self.path, active_only=True)
            if not by_article:
                warnings.append("Каталог SQLite пуст. Импортируйте products.xlsx во вкладке «Каталог товаров».")

            self.by_article = by_article
            self._samples = samples
            self._names = names
            self._name_formats = name_formats
            self.warnings = warnings
            self._snapshot_cache[cache_key] = (revision, by_article, samples, names, name_formats, list(warnings))
        except Exception as exc:
            self.error = f"Не удалось открыть каталог SQLite: {exc}"
            self.by_article.clear()
            self._samples = []
            self._names = []
            self._name_formats = []

    def _build_display_name(self, item: dict[str, Any]) -> str:
        parts = [_clean(item.get(key)) for key in ("subcategory", "product", "volume")]
        name = " ".join(part for part in parts if part).strip()
        sample = _clean(item.get("sample"))
        sample_volume = _clean(item.get("sample_volume"))
        if sample:
            tail = " ".join(part for part in (sample, sample_volume) if part).strip()
            name = f"{name} + {tail}" if name and tail else (tail or name)
        return name or _clean(item.get("seller_article"))

    def find_by_article(self, seller_article: str | None) -> dict[str, Any] | None:
        return self.by_article.get(_clean(seller_article)) if seller_article is not None else None

    def list_name_aromas(self) -> list[str]:
        return [_clean(row.get("name")) for row in self._names if _clean(row.get("name"))]

    def list_sample_aromas(self) -> list[str]:
        return [_clean(row.get("name")) for row in self._samples if _clean(row.get("name"))]

    def list_name_formats(self) -> list[str]:
        return [_clean(row.get("name")) for row in self._name_formats if _clean(row.get("name"))]

    # Backwards-compatible aliases used by older templates/plugins.
    def list_oil_perfume_aromas(self) -> list[str]:
        return self.list_name_aromas()

    def list_oil_perfume_volumes(self) -> list[str]:
        return self.list_name_formats()

    def find_name_label(self, aroma: str, volume: str | None = None) -> dict[str, Any] | None:
        aroma = _clean(aroma)
        volume = _clean(volume)
        record = next((row for row in self._names if _clean(row.get("name")) == aroma), None)
        if not record:
            return None
        if volume and volume not in self.list_name_formats():
            return None
        return {
            "seller_article": "",
            "subcategory": "Духи",
            "product": _clean(record.get("name")),
            "volume": volume,
            "sample": "",
            "sample_volume": "",
            "category": "Парфюмерия",
            "excel_kiz": "Нет",
            "inspiration_name": _clean(record.get("inspiration_name")),
            "name_no": _clean(record.get("name_no")),
            "raw_excel": {
                "№": _clean(record.get("name_no")),
                "Название": _clean(record.get("name")),
                "Название мотива": _clean(record.get("inspiration_name")),
                "Продукт": _clean(record.get("name")),
                "Объем (парфюмерия)": volume,
            },
        }

    def find_oil_perfume_product(self, aroma: str, volume: str | None = None) -> dict[str, Any] | None:
        return self.find_name_label(aroma, volume)

    def find_sample_by_aroma(self, aroma: str) -> dict[str, str] | None:
        aroma = _clean(aroma)
        for row in self._samples:
            if _clean(row.get("name")) == aroma:
                return {
                    "№": _clean(row.get("sample_no")),
                    "Name": _clean(row.get("name")),
                    "WB code": _clean(row.get("wb_code")),
                    "QR link": _clean(row.get("qr_link")),
                }
        return None
