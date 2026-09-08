from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable


CATALOG_SCHEMA_VERSION = 6


DDL = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA busy_timeout=30000;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS products (
    seller_article TEXT PRIMARY KEY,
    wb_article TEXT NOT NULL DEFAULT '',
    subcategory TEXT NOT NULL DEFAULT '',
    product TEXT NOT NULL DEFAULT '',
    volume TEXT NOT NULL DEFAULT '',
    sample TEXT NOT NULL DEFAULT '',
    sample_volume TEXT NOT NULL DEFAULT '',
    category TEXT NOT NULL DEFAULT '',
    marking_profile TEXT NOT NULL DEFAULT 'AUTO',
    kiz_required INTEGER NOT NULL DEFAULT 0,
    gtin TEXT NOT NULL DEFAULT '',
    product_group TEXT NOT NULL DEFAULT '',
    template_id INTEGER NOT NULL DEFAULT 0,
    cis_type TEXT NOT NULL DEFAULT 'UNIT',
    tnved_code TEXT NOT NULL DEFAULT '',
    certificate_type TEXT NOT NULL DEFAULT '',
    certificate_number TEXT NOT NULL DEFAULT '',
    certificate_date TEXT NOT NULL DEFAULT '',
    certificate_valid_until TEXT NOT NULL DEFAULT '',
    shelf_life_months INTEGER NOT NULL DEFAULT 0,
    alcohol_volume_pct TEXT NOT NULL DEFAULT '',
    notes TEXT NOT NULL DEFAULT '',
    marketing_title TEXT NOT NULL DEFAULT '',
    fragrance_family TEXT NOT NULL DEFAULT '',
    target_audience TEXT NOT NULL DEFAULT '',
    marketing_description TEXT NOT NULL DEFAULT '',
    marketing_keywords TEXT NOT NULL DEFAULT '',
    package_length_cm REAL NOT NULL DEFAULT 0,
    package_width_cm REAL NOT NULL DEFAULT 0,
    package_height_cm REAL NOT NULL DEFAULT 0,
    package_weight_g REAL NOT NULL DEFAULT 0,
    units_per_box INTEGER NOT NULL DEFAULT 0,
    materials_cost_rub REAL NOT NULL DEFAULT 0,
    packaging_cost_rub REAL NOT NULL DEFAULT 0,
    labor_cost_rub REAL NOT NULL DEFAULT 0,
    marking_cost_rub REAL NOT NULL DEFAULT 0,
    other_unit_cost_rub REAL NOT NULL DEFAULT 0,
    cost_price_rub REAL NOT NULL DEFAULT 0,
    tax_rate_pct REAL NOT NULL DEFAULT 0,
    seller_price_rub REAL NOT NULL DEFAULT 0,
    minimum_price_rub REAL NOT NULL DEFAULT 0,
    marketplace_commission_pct REAL NOT NULL DEFAULT 0,
    marketplace_logistics_rub REAL NOT NULL DEFAULT 0,
    marketplace_storage_rub REAL NOT NULL DEFAULT 0,
    marketplace_advertising_pct REAL NOT NULL DEFAULT 0,
    marketplace_other_cost_rub REAL NOT NULL DEFAULT 0,
    marketplace_buyout_pct REAL NOT NULL DEFAULT 100,
    marketplace_return_cost_rub REAL NOT NULL DEFAULT 0,
    site_price_rub REAL NOT NULL DEFAULT 0,
    acquiring_pct REAL NOT NULL DEFAULT 0,
    site_delivery_cost_rub REAL NOT NULL DEFAULT 0,
    site_advertising_pct REAL NOT NULL DEFAULT 0,
    site_other_cost_rub REAL NOT NULL DEFAULT 0,
    raw_json TEXT NOT NULL DEFAULT '{}',
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_products_gtin ON products(gtin);
CREATE INDEX IF NOT EXISTS idx_products_category ON products(category, subcategory);
CREATE INDEX IF NOT EXISTS idx_products_kiz ON products(kiz_required, active);
CREATE INDEX IF NOT EXISTS idx_products_name ON products(product);

CREATE TABLE IF NOT EXISTS catalog_samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sample_no TEXT NOT NULL DEFAULT '',
    name TEXT NOT NULL UNIQUE,
    wb_code TEXT NOT NULL DEFAULT '',
    qr_link TEXT NOT NULL DEFAULT '',
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_catalog_samples_active_name
ON catalog_samples(active, name);

CREATE TABLE IF NOT EXISTS catalog_names (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name_no TEXT NOT NULL DEFAULT '',
    name TEXT NOT NULL UNIQUE,
    inspiration_name TEXT NOT NULL DEFAULT '',
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_catalog_names_active_name
ON catalog_names(active, name);

CREATE TABLE IF NOT EXISTS catalog_name_formats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    format_no TEXT NOT NULL DEFAULT '',
    name TEXT NOT NULL UNIQUE,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_catalog_name_formats_active_name
ON catalog_name_formats(active, name);

CREATE TABLE IF NOT EXISTS catalog_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    seller_article TEXT NOT NULL DEFAULT '',
    details_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS catalog_imports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    filename TEXT NOT NULL DEFAULT '',
    mode TEXT NOT NULL DEFAULT 'merge',
    products_read INTEGER NOT NULL DEFAULT 0,
    products_written INTEGER NOT NULL DEFAULT 0,
    samples_read INTEGER NOT NULL DEFAULT 0,
    samples_written INTEGER NOT NULL DEFAULT 0,
    names_read INTEGER NOT NULL DEFAULT 0,
    names_written INTEGER NOT NULL DEFAULT 0,
    formats_read INTEGER NOT NULL DEFAULT 0,
    formats_written INTEGER NOT NULL DEFAULT 0,
    warnings_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""


class CatalogDbError(RuntimeError):
    pass


PRODUCT_DATA_COLUMNS: tuple[str, ...] = (
    "seller_article", "wb_article", "subcategory", "product", "volume", "sample",
    "sample_volume", "category", "marking_profile", "kiz_required", "gtin",
    "product_group", "template_id", "cis_type", "tnved_code", "certificate_type",
    "certificate_number", "certificate_date", "certificate_valid_until", "shelf_life_months",
    "alcohol_volume_pct", "notes",
    "marketing_title", "fragrance_family", "target_audience", "marketing_description",
    "marketing_keywords", "package_length_cm", "package_width_cm", "package_height_cm",
    "package_weight_g", "units_per_box", "materials_cost_rub", "packaging_cost_rub",
    "labor_cost_rub", "marking_cost_rub", "other_unit_cost_rub", "cost_price_rub",
    "tax_rate_pct", "seller_price_rub", "minimum_price_rub",
    "marketplace_commission_pct", "marketplace_logistics_rub",
    "marketplace_storage_rub", "marketplace_advertising_pct",
    "marketplace_other_cost_rub", "marketplace_buyout_pct",
    "marketplace_return_cost_rub", "site_price_rub", "acquiring_pct",
    "site_delivery_cost_rub", "site_advertising_pct", "site_other_cost_rub",
    "raw_json", "active",
)


def _upsert_product(conn: sqlite3.Connection, item: dict[str, Any]) -> None:
    columns = ", ".join(PRODUCT_DATA_COLUMNS)
    values = ", ".join(f":{column}" for column in PRODUCT_DATA_COLUMNS)
    updates = ", ".join(
        f"{column}=excluded.{column}" for column in PRODUCT_DATA_COLUMNS if column != "seller_article"
    )
    conn.execute(
        f"INSERT INTO products({columns}, updated_at) VALUES({values}, CURRENT_TIMESTAMP) "
        f"ON CONFLICT(seller_article) DO UPDATE SET {updates}, updated_at=CURRENT_TIMESTAMP",
        item,
    )


_COST_COLUMNS: tuple[str, ...] = (
    "cost_price_rub",
    "materials_cost_rub",
    "packaging_cost_rub",
    "labor_cost_rub",
    "marking_cost_rub",
    "other_unit_cost_rub",
)


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone() is not None


def _record_economy_cost(conn: sqlite3.Connection, item: dict[str, Any]) -> None:
    """Keep catalog cost and its economy history in the same transaction."""
    if not int(item.get("active") or 0) or not _table_exists(conn, "economy_cost_history"):
        return
    article = _clean(item.get("seller_article"))
    current = tuple(round(float(item.get(column) or 0), 6) for column in _COST_COLUMNS)
    latest = conn.execute(
        f"SELECT {', '.join(_COST_COLUMNS)} FROM economy_cost_history "
        "WHERE seller_article=? ORDER BY effective_from DESC, id DESC LIMIT 1",
        (article,),
    ).fetchone()
    if latest is not None:
        previous = tuple(round(float(latest[column] or 0), 6) for column in _COST_COLUMNS)
        if previous == current:
            return
    conn.execute(
        """
        INSERT INTO economy_cost_history(
            seller_article, effective_from, cost_price_rub,
            materials_cost_rub, packaging_cost_rub, labor_cost_rub,
            marking_cost_rub, other_unit_cost_rub, source, notes
        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, 'catalog', 'Снимок при изменении каталога')
        """,
        (article, datetime.now().date().isoformat(), *current),
    )


PRODUCT_COLUMN_MIGRATIONS: dict[str, str] = {
    "marking_profile": "TEXT NOT NULL DEFAULT 'AUTO'",
    "shelf_life_months": "INTEGER NOT NULL DEFAULT 0",
    "alcohol_volume_pct": "TEXT NOT NULL DEFAULT ''",
    "marketing_title": "TEXT NOT NULL DEFAULT ''",
    "fragrance_family": "TEXT NOT NULL DEFAULT ''",
    "target_audience": "TEXT NOT NULL DEFAULT ''",
    "marketing_description": "TEXT NOT NULL DEFAULT ''",
    "marketing_keywords": "TEXT NOT NULL DEFAULT ''",
    "package_length_cm": "REAL NOT NULL DEFAULT 0",
    "package_width_cm": "REAL NOT NULL DEFAULT 0",
    "package_height_cm": "REAL NOT NULL DEFAULT 0",
    "package_weight_g": "REAL NOT NULL DEFAULT 0",
    "units_per_box": "INTEGER NOT NULL DEFAULT 0",
    "materials_cost_rub": "REAL NOT NULL DEFAULT 0",
    "packaging_cost_rub": "REAL NOT NULL DEFAULT 0",
    "labor_cost_rub": "REAL NOT NULL DEFAULT 0",
    "marking_cost_rub": "REAL NOT NULL DEFAULT 0",
    "other_unit_cost_rub": "REAL NOT NULL DEFAULT 0",
    "cost_price_rub": "REAL NOT NULL DEFAULT 0",
    "tax_rate_pct": "REAL NOT NULL DEFAULT 0",
    "seller_price_rub": "REAL NOT NULL DEFAULT 0",
    "minimum_price_rub": "REAL NOT NULL DEFAULT 0",
    "marketplace_commission_pct": "REAL NOT NULL DEFAULT 0",
    "marketplace_logistics_rub": "REAL NOT NULL DEFAULT 0",
    "marketplace_storage_rub": "REAL NOT NULL DEFAULT 0",
    "marketplace_advertising_pct": "REAL NOT NULL DEFAULT 0",
    "marketplace_other_cost_rub": "REAL NOT NULL DEFAULT 0",
    "marketplace_buyout_pct": "REAL NOT NULL DEFAULT 100",
    "marketplace_return_cost_rub": "REAL NOT NULL DEFAULT 0",
    "site_price_rub": "REAL NOT NULL DEFAULT 0",
    "acquiring_pct": "REAL NOT NULL DEFAULT 0",
    "site_delivery_cost_rub": "REAL NOT NULL DEFAULT 0",
    "site_advertising_pct": "REAL NOT NULL DEFAULT 0",
    "site_other_cost_rub": "REAL NOT NULL DEFAULT 0",
}


def _ensure_product_columns(conn: sqlite3.Connection) -> None:
    existing = {str(row[1]) for row in conn.execute("PRAGMA table_info(products)").fetchall()}
    for column, definition in PRODUCT_COLUMN_MIGRATIONS.items():
        if column not in existing:
            conn.execute(f"ALTER TABLE products ADD COLUMN {column} {definition}")


def _connect(path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(Path(path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _bump_revision(conn: sqlite3.Connection) -> None:
    conn.execute(
        "UPDATE schema_meta SET value = CAST(COALESCE(value, '0') AS INTEGER) + 1 WHERE key = 'catalog_revision'"
    )


def get_catalog_revision(path: str | Path) -> int:
    with _connect(path) as conn:
        row = conn.execute("SELECT value FROM schema_meta WHERE key = 'catalog_revision'").fetchone()
    try:
        return int(row[0]) if row else 0
    except (TypeError, ValueError):
        return 0


DEFAULT_NAME_ROWS: tuple[tuple[str, str, str], ...] = (
    ("1", "Сливочная Ваниль", ""),
    ("2", "Черный Опиум", "Black Opium"),
    ("3", "Интрига Дьявола", "Devil’s Intrigue"),
    ("4", "Кирке", "Kirke"),
    ("5", "Табак&Ваниль", "Tobacco Vanille"),
    ("6", "Девственная Вишня", "Lost Cherry"),
    ("7", "Молекула 02", "Molecule 02"),
    ("8", "Роковая Женщина", "Fleur Narcotique"),
    ("9", "Лавандовый Мед", ""),
    ("10", "Терпкий Плод", "Bitter Peach"),
    ("11", "Хаяти", "Hayati"),
    ("12", "Баккара Руж", "Baccarat Rouge 540"),
    ("13", "Императрица", "L’Imperatrice"),
    ("14", "Черный Афгано", "Black Afgano"),
    ("15", "Сладкое Манго", "Mango Skin"),
    ("16", "Африканский Бал", "Bal d’Afrique"),
    ("17", "Бланш", "Blanche"),
    ("18", "Тадж Сансет", "Taj Sunset"),
    ("19", "Ежевика&Лес", "Blackberry&Bay"),
    ("20", "Шалфей&Море", "Wood Sage&Sea Salt"),
    ("21", "Черный Перец", "Black Pepper"),
    ("22", "Ванильный Бленд", "Vanilla Blend"),
    ("23", "Белый Шоколад", ""),
    ("24", "Молочный Шелк", "White Chocola"),
    ("25", "Розовая Молекула", "Pink Molecule"),
    ("26", "Любовь Моя!", "Love, don’t be shy"),
    ("27", "Ганимед", "Ganymede"),
    ("28", "Ангел", "Angels’ Share"),
    ("29", "Дурман", "Side Effect"),
    ("30", "Габа", "Gaba"),
    ("31", "Плохая Девочка", "Good Girl Gone Bad"),
    ("32", "Тайгар", "Tygar"),
    ("33", "Симфония", "Symphony"),
    ("34", "Чистая Любовь", "Crystal Love"),
    ("35", "Психоделика", "Psychedelic Love"),
    ("36", "Имперский Уд", "Oud for Greatness"),
    ("37", "Мегамар", "Megamare"),
    ("38", "Амбра&Кожа", "Ombre Leather"),
    ("39", "Яра", "Yara"),
    ("40", "Любовник №4", "APRES L’AMOUR"),
    ("41", "Соблазн", "Lucky Wish"),
)

DEFAULT_NAME_FORMATS: tuple[str, ...] = (
    "3мл роллер",
    "5мл атомайзер",
    "10мл роллер",
    "10мл атомайзер",
    "20мл атомайзер",
    "33мл атомайзер",
    "100мл рефил для роллеров",
    "100мл рефил для атомайзеров",
)


def _ensure_catalog_import_columns(conn: sqlite3.Connection) -> None:
    existing = {str(row[1]) for row in conn.execute("PRAGMA table_info(catalog_imports)").fetchall()}
    for column in ("names_read", "names_written", "formats_read", "formats_written"):
        if column not in existing:
            conn.execute(f"ALTER TABLE catalog_imports ADD COLUMN {column} INTEGER NOT NULL DEFAULT 0")


def _seed_manual_name_catalog(conn: sqlite3.Connection) -> None:
    names_count = int(conn.execute("SELECT COUNT(*) FROM catalog_names").fetchone()[0])
    if names_count == 0:
        conn.executemany(
            "INSERT INTO catalog_names(name_no, name, inspiration_name, active) VALUES(?, ?, ?, 1)",
            DEFAULT_NAME_ROWS,
        )
    formats_count = int(conn.execute("SELECT COUNT(*) FROM catalog_name_formats").fetchone()[0])
    if formats_count == 0:
        conn.executemany(
            "INSERT INTO catalog_name_formats(format_no, name, active) VALUES(?, ?, 1)",
            [(str(i), name) for i, name in enumerate(DEFAULT_NAME_FORMATS, start=1)],
        )


def _migrate_marking_profiles_v5(conn: sqlite3.Connection) -> int:
    """Freeze legacy AUTO rows into explicit profiles.

    0.75 inferred KIZ from category on every save/import. In 0.76 the profile
    itself is authoritative, so the currently resolved technical state is
    converted once and no longer changes when a legacy KIZ cell is removed.
    """
    rows = conn.execute(
        "SELECT seller_article, marking_profile, kiz_required, product_group, template_id FROM products"
    ).fetchall()
    changed = 0
    for row in rows:
        profile = _normalize_marking_profile(row["marking_profile"])
        if profile != "AUTO":
            continue
        group = _clean(row["product_group"]).lower()
        required = int(row["kiz_required"] or 0)
        template_id = int(row["template_id"] or 0)
        if not required:
            explicit = "NONE"
        elif group == "perfumery" or template_id == 9:
            explicit = "PERFUMERY"
        elif group == "chemistry" or template_id == 46:
            explicit = "CHEMISTRY"
        else:
            explicit = "CUSTOM"
        conn.execute(
            "UPDATE products SET marking_profile=?, updated_at=CURRENT_TIMESTAMP WHERE seller_article=?",
            (explicit, row["seller_article"]),
        )
        changed += 1
    if changed:
        conn.execute(
            "INSERT INTO catalog_events(event_type, details_json) VALUES('marking_profiles_v5_migrated', ?)",
            (json.dumps({"changed": changed}, ensure_ascii=False),),
        )
        _bump_revision(conn)
    return changed


def ensure_catalog_schema(path: str | Path) -> Path:
    db_path = Path(path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with _connect(db_path) as conn:
        conn.executescript(DDL)
        _ensure_product_columns(conn)
        _ensure_catalog_import_columns(conn)
        _seed_manual_name_catalog(conn)
        previous_row = conn.execute(
            "SELECT value FROM schema_meta WHERE key='catalog_schema_version'"
        ).fetchone()
        try:
            previous_version = int(previous_row[0]) if previous_row else 0
        except (TypeError, ValueError):
            previous_version = 0
        if previous_version < 5:
            _migrate_marking_profiles_v5(conn)
        conn.execute(
            "INSERT INTO schema_meta(key, value) VALUES('catalog_schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(CATALOG_SCHEMA_VERSION),),
        )
        conn.execute(
            "INSERT OR IGNORE INTO schema_meta(key, value) VALUES('catalog_revision', '0')"
        )
        conn.commit()
    refresh_automatic_marking_profiles(db_path)
    return db_path


def _clean(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def _normalize_date(value: Any) -> str:
    text = _clean(value)
    if not text:
        return ""
    text = text.split(" ", 1)[0]
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(text, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return text


def _normalize_certificate_type(value: Any) -> str:
    text = _clean(value)
    low = text.lower()
    if "деклар" in low:
        return "CONFORMITY_DECLARATION"
    if "сертифик" in low:
        return "CONFORMITY_CERTIFICATE"
    return text


def _normalize_gtin(value: Any) -> str:
    text = _clean(value).replace(" ", "")
    if text.endswith(".0") and text[:-2].isdigit():
        text = text[:-2]
    if text.isdigit() and 8 <= len(text) <= 14:
        return text.zfill(14)
    return text


def _normalize_tnved(value: Any) -> str:
    text = _clean(value).replace(" ", "").replace("\u00a0", "")
    if text.endswith(".0") and text[:-2].isdigit():
        text = text[:-2]
    return "".join(char for char in text if char.isdigit()) if text else ""


def _bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return _clean(value).lower() in {"1", "true", "yes", "да", "y", "on", "+"}


def _float(value: Any, *, field_name: str = "Значение") -> float:
    text = _clean(value).replace(" ", "").replace(",", ".")
    if not text:
        return 0.0
    try:
        number = float(text)
    except (TypeError, ValueError) as exc:
        raise CatalogDbError(f"{field_name} должно быть числом") from exc
    if number < 0:
        raise CatalogDbError(f"{field_name} не может быть отрицательным")
    return number


def _int(value: Any, *, field_name: str = "Значение") -> int:
    number = _float(value, field_name=field_name)
    if not number.is_integer():
        raise CatalogDbError(f"{field_name} должно быть целым числом")
    return int(number)


def _payload_or_previous(payload: dict[str, Any], previous: dict[str, Any], key: str, default: Any = "") -> Any:
    return payload[key] if key in payload else previous.get(key, default)


def _infer_certificate_type(number: Any, explicit: Any = "") -> str:
    normalized = _normalize_certificate_type(explicit)
    if normalized:
        return normalized
    text = _clean(number).upper().replace("Ё", "Е")
    if not text:
        return ""
    if " Д-" in text or "Д-" in text or "DECLAR" in text:
        return "CONFORMITY_DECLARATION"
    if " С-" in text or "С-" in text or "CERT" in text:
        return "CONFORMITY_CERTIFICATE"
    return ""


MARKING_PROFILE_LABELS: dict[str, str] = {
    "AUTO": "Автоматически",
    "PERFUMERY": "Парфюмерия",
    "CHEMISTRY": "Дезодоранты и косметика",
    "NONE": "Не требует маркировки",
    "CUSTOM": "Пользовательский профиль",
}


RAW_EXCEL_HEADERS: dict[str, str] = {
    "seller_article": "Артикул продавца",
    "wb_article": "Артикул WB",
    "subcategory": "Подкатегория",
    "product": "Продукт",
    "volume": "Объем (парфюмерия)",
    "sample": "Пробник",
    "sample_volume": "Объем пробника",
    "category": "Категория",
    "marking_profile": "Профиль маркировки",
    "gtin": "GTIN",
    "product_group": "Товарная группа ЧЗ",
    "template_id": "templateId",
    "cis_type": "Тип КИЗ",
    "tnved_code": "ТН ВЭД",
    "certificate_type": "Тип документа",
    "certificate_number": "Номер документа",
    "certificate_date": "Дата начала действия документа",
    "certificate_valid_until": "Дата окончания действия документа",
    "shelf_life_months": "Срок годности, мес.",
    "alcohol_volume_pct": "Этиловый спирт, %",
    "notes": "Комментарий",
    "active": "Активен",
    "marketing_title": "Маркетинговое название",
    "fragrance_family": "Семейство аромата",
    "target_audience": "Аудитория",
    "marketing_description": "Маркетинговое описание",
    "marketing_keywords": "Ключевые слова",
    "package_length_cm": "Длина упаковки, см",
    "package_width_cm": "Ширина упаковки, см",
    "package_height_cm": "Высота упаковки, см",
    "package_weight_g": "Вес с упаковкой, г",
    "units_per_box": "Штук в коробе",
    "cost_price_rub": "Себестоимость, руб",
}
RAW_META_KEY = "__fbe_cell_meta__"
RAW_HEADERS_KEY = "__fbe_header_order__"
CERTIFICATE_TYPE_LABELS = {
    "CONFORMITY_DECLARATION": "Декларация о соответствии",
    "CONFORMITY_CERTIFICATE": "Сертификат соответствия",
}


def _load_raw_excel(previous: dict[str, Any]) -> dict[str, Any]:
    raw = previous.get("raw_json") or {}
    if isinstance(raw, dict):
        return dict(raw)
    try:
        parsed = json.loads(str(raw))
    except Exception:
        return {}
    return dict(parsed) if isinstance(parsed, dict) else {}


def _raw_excel_value(field: str, value: Any) -> Any:
    if field == "marking_profile":
        code = _normalize_marking_profile(value)
        return MARKING_PROFILE_LABELS.get(code, _clean(value))
    if field == "certificate_type":
        code = _normalize_certificate_type(value)
        return CERTIFICATE_TYPE_LABELS.get(code, _clean(value))
    if field == "active":
        return "Да" if _bool(value, True) else "Нет"
    return value if value is not None else ""


def _merge_raw_excel(payload: dict[str, Any], previous: dict[str, Any]) -> dict[str, Any]:
    supplied = payload.get("raw_excel")
    if isinstance(supplied, dict):
        return dict(supplied)
    raw = _load_raw_excel(previous)
    meta = raw.get(RAW_META_KEY)
    if not isinstance(meta, dict):
        meta = {}
    for field, header in RAW_EXCEL_HEADERS.items():
        if field not in payload:
            continue
        raw[header] = _raw_excel_value(field, payload.get(field))
        # An edit in the Catalog UI intentionally replaces the old imported
        # cell type/format for this one field, while untouched cells stay exact.
        meta.pop(header, None)
    if meta:
        raw[RAW_META_KEY] = meta
    else:
        raw.pop(RAW_META_KEY, None)
    if raw:
        raw.setdefault(RAW_HEADERS_KEY, list(RAW_EXCEL_HEADERS.values()))
    return raw


def _normalize_marking_profile(value: Any) -> str:
    text = _clean(value).upper().replace("Ё", "Е")
    aliases = {
        "": "AUTO",
        "AUTO": "AUTO",
        "АВТО": "AUTO",
        "АВТОМАТИЧЕСКИ": "AUTO",
        "PERFUMERY": "PERFUMERY",
        "ПАРФЮМЕРИЯ": "PERFUMERY",
        "ДУХИ": "PERFUMERY",
        "CHEMISTRY": "CHEMISTRY",
        "ДЕЗОДОРАНТЫ": "CHEMISTRY",
        "ДЕЗОДОРАНТЫ И КОСМЕТИКА": "CHEMISTRY",
        "КОСМЕТИКА": "CHEMISTRY",
        "NONE": "NONE",
        "БЕЗ МАРКИРОВКИ": "NONE",
        "НЕ ТРЕБУЕТ МАРКИРОВКИ": "NONE",
        "НЕ ТРЕБУЕТСЯ МАРКИРОВКА": "NONE",
        "НЕ ТРЕБУЕТСЯ": "NONE",
        "CUSTOM": "CUSTOM",
        "ПОЛЬЗОВАТЕЛЬСКИЙ": "CUSTOM",
        "ПОЛЬЗОВАТЕЛЬСКИЙ ПРОФИЛЬ": "CUSTOM",
    }
    return aliases.get(text, "AUTO")


def _percentage(value: Any, *, field_name: str, default: float = 0.0) -> float:
    text = _clean(value)
    if not text:
        return float(default)
    number = _float(value, field_name=field_name)
    if number > 100:
        raise CatalogDbError(f"{field_name} не может быть больше 100%")
    return number


def infer_marking_profile(payload: dict[str, Any], previous: dict[str, Any] | None = None) -> dict[str, Any]:
    previous = previous or {}
    category = _clean(payload.get("category") if "category" in payload else previous.get("category"))
    subcategory = _clean(payload.get("subcategory") if "subcategory" in payload else previous.get("subcategory"))
    product = _clean(payload.get("product") if "product" in payload else previous.get("product"))
    requested_profile = _normalize_marking_profile(
        payload.get("marking_profile") if "marking_profile" in payload else previous.get("marking_profile")
    )
    category_text = category.lower().replace("ё", "е")
    subcategory_text = subcategory.lower().replace("ё", "е")
    product_text = product.lower().replace("ё", "е")
    text = " ".join((category_text, subcategory_text, product_text))

    profile = {
        "marking_profile": requested_profile,
        "kiz_required": int(previous.get("kiz_required") or 0),
        "product_group": _clean(previous.get("product_group")),
        "template_id": int(previous.get("template_id") or 0),
        "cis_type": _clean(previous.get("cis_type")) or "UNIT",
        "tnved_code": _normalize_tnved(payload.get("tnved_code") if "tnved_code" in payload else previous.get("tnved_code")),
        "profile_label": MARKING_PROFILE_LABELS.get(requested_profile, "Не определен"),
    }

    def apply(profile_code: str) -> dict[str, Any]:
        profile["marking_profile"] = requested_profile
        profile["profile_label"] = MARKING_PROFILE_LABELS[profile_code]
        if profile_code == "PERFUMERY":
            profile.update({"kiz_required": 1, "product_group": "perfumery", "template_id": 9, "cis_type": "UNIT"})
            if not profile["tnved_code"]:
                if "духи масляные" in subcategory_text or subcategory_text == "духи" or product_text.startswith("духи масляные"):
                    profile["tnved_code"] = "3303001000"
                elif "туалетная вода" in subcategory_text or product_text.startswith("туалетная вода"):
                    profile["tnved_code"] = "3303009000"
        elif profile_code == "CHEMISTRY":
            profile.update({"kiz_required": 1, "product_group": "chemistry", "template_id": 46, "cis_type": "UNIT"})
        elif profile_code == "NONE":
            profile.update({"kiz_required": 0, "product_group": "", "template_id": 0, "cis_type": "UNIT"})
        return profile

    if requested_profile in {"PERFUMERY", "CHEMISTRY", "NONE"}:
        return apply(requested_profile)
    if requested_profile == "CUSTOM":
        profile["profile_label"] = MARKING_PROFILE_LABELS["CUSTOM"]
        return profile

    # AUTO: first use category/subcategory, then preserve legacy technical data.
    if "дезодорант" in text or category_text == "дезодоранты":
        return apply("CHEMISTRY")

    perfume_subcategory = (
        "духи" in subcategory_text
        or "туалетная вода" in subcategory_text
        or "парфюмерная вода" in subcategory_text
    )
    perfume_product_fallback = (
        not subcategory_text
        and (
            product_text.startswith("духи ")
            or product_text.startswith("духи масляные")
            or product_text.startswith("туалетная вода")
            or product_text.startswith("парфюмерная вода")
        )
    )
    if category_text == "парфюмерия" or perfume_subcategory or perfume_product_fallback:
        return apply("PERFUMERY")

    if profile["product_group"] or profile["template_id"] or profile["kiz_required"]:
        profile["profile_label"] = "Пользовательский профиль"
    else:
        profile["profile_label"] = "Не определен"
    return profile


def normalize_product(payload: dict[str, Any], previous: dict[str, Any] | None = None) -> dict[str, Any]:
    previous = previous or {}
    article = _clean(payload.get("seller_article") if "seller_article" in payload else previous.get("seller_article"))
    if not article:
        raise CatalogDbError("Артикул продавца обязателен")

    if "marking_profile" in payload and not _clean(payload.get("marking_profile")):
        raise CatalogDbError("Выберите профиль маркировки")

    # From 0.72.0 the manufacturer controls one financial input only: the
    # complete production cost of one unit. Legacy component fields remain in
    # SQLite for compatibility but no longer override the entered total.
    materials = _float(_payload_or_previous(payload, previous, "materials_cost_rub", 0), field_name="Сырье и состав")
    packaging = _float(_payload_or_previous(payload, previous, "packaging_cost_rub", 0), field_name="Тара и упаковка")
    labor = _float(_payload_or_previous(payload, previous, "labor_cost_rub", 0), field_name="Производство и работа")
    marking = _float(_payload_or_previous(payload, previous, "marking_cost_rub", 0), field_name="Маркировка и этикетки")
    other_unit = _float(_payload_or_previous(payload, previous, "other_unit_cost_rub", 0), field_name="Прочие прямые расходы")
    if "cost_price_rub" in payload:
        total_cost = _float(payload.get("cost_price_rub"), field_name="Себестоимость")
    else:
        previous_total = _float(previous.get("cost_price_rub", 0), field_name="Себестоимость")
        component_cost = round(materials + packaging + labor + marking + other_unit, 4)
        total_cost = previous_total if previous_total > 0 else component_cost

    base = {
        "seller_article": article,
        "wb_article": _clean(_payload_or_previous(payload, previous, "wb_article")),
        "subcategory": _clean(_payload_or_previous(payload, previous, "subcategory")),
        "product": _clean(_payload_or_previous(payload, previous, "product")),
        "volume": _clean(_payload_or_previous(payload, previous, "volume")),
        "sample": _clean(_payload_or_previous(payload, previous, "sample")),
        "sample_volume": _clean(_payload_or_previous(payload, previous, "sample_volume")),
        "category": _clean(_payload_or_previous(payload, previous, "category")),
        "marking_profile": _normalize_marking_profile(_payload_or_previous(payload, previous, "marking_profile", "AUTO")),
        "gtin": _normalize_gtin(_payload_or_previous(payload, previous, "gtin")),
        "tnved_code": _normalize_tnved(_payload_or_previous(payload, previous, "tnved_code")),
        "certificate_number": _clean(_payload_or_previous(payload, previous, "certificate_number")),
        "certificate_date": _normalize_date(_payload_or_previous(payload, previous, "certificate_date")),
        "certificate_valid_until": _normalize_date(_payload_or_previous(payload, previous, "certificate_valid_until")),
        "shelf_life_months": _int(_payload_or_previous(payload, previous, "shelf_life_months", 0), field_name="Срок годности, мес."),
        "alcohol_volume_pct": _clean(_payload_or_previous(payload, previous, "alcohol_volume_pct")),
        "notes": _clean(_payload_or_previous(payload, previous, "notes")),
        "marketing_title": _clean(_payload_or_previous(payload, previous, "marketing_title")),
        "fragrance_family": _clean(_payload_or_previous(payload, previous, "fragrance_family")),
        "target_audience": _clean(_payload_or_previous(payload, previous, "target_audience")),
        "marketing_description": _clean(_payload_or_previous(payload, previous, "marketing_description")),
        "marketing_keywords": _clean(_payload_or_previous(payload, previous, "marketing_keywords")),
        "package_length_cm": _float(_payload_or_previous(payload, previous, "package_length_cm", 0), field_name="Длина упаковки"),
        "package_width_cm": _float(_payload_or_previous(payload, previous, "package_width_cm", 0), field_name="Ширина упаковки"),
        "package_height_cm": _float(_payload_or_previous(payload, previous, "package_height_cm", 0), field_name="Высота упаковки"),
        "package_weight_g": _float(_payload_or_previous(payload, previous, "package_weight_g", 0), field_name="Вес упаковки"),
        "units_per_box": _int(_payload_or_previous(payload, previous, "units_per_box", 0), field_name="Штук в коробе"),
        "materials_cost_rub": materials,
        "packaging_cost_rub": packaging,
        "labor_cost_rub": labor,
        "marking_cost_rub": marking,
        "other_unit_cost_rub": other_unit,
        "cost_price_rub": total_cost,
        "tax_rate_pct": float(previous.get("tax_rate_pct") or 0),
        "seller_price_rub": _float(_payload_or_previous(payload, previous, "seller_price_rub", 0), field_name="Цена продавца"),
        "minimum_price_rub": _float(_payload_or_previous(payload, previous, "minimum_price_rub", 0), field_name="Минимальная цена"),
        "marketplace_commission_pct": _percentage(_payload_or_previous(payload, previous, "marketplace_commission_pct", 0), field_name="Комиссия маркетплейса"),
        "marketplace_logistics_rub": _float(_payload_or_previous(payload, previous, "marketplace_logistics_rub", 0), field_name="Логистика маркетплейса"),
        "marketplace_storage_rub": _float(_payload_or_previous(payload, previous, "marketplace_storage_rub", 0), field_name="Хранение маркетплейса"),
        "marketplace_advertising_pct": _percentage(_payload_or_previous(payload, previous, "marketplace_advertising_pct", 0), field_name="Реклама маркетплейса"),
        "marketplace_other_cost_rub": _float(_payload_or_previous(payload, previous, "marketplace_other_cost_rub", 0), field_name="Прочие расходы маркетплейса"),
        "marketplace_buyout_pct": _percentage(_payload_or_previous(payload, previous, "marketplace_buyout_pct", 100), field_name="Процент выкупа", default=100),
        "marketplace_return_cost_rub": _float(_payload_or_previous(payload, previous, "marketplace_return_cost_rub", 0), field_name="Стоимость обратной логистики"),
        "site_price_rub": _float(_payload_or_previous(payload, previous, "site_price_rub", 0), field_name="Цена на сайте"),
        "acquiring_pct": _percentage(_payload_or_previous(payload, previous, "acquiring_pct", 0), field_name="Эквайринг"),
        "site_delivery_cost_rub": _float(_payload_or_previous(payload, previous, "site_delivery_cost_rub", 0), field_name="Доставка сайта"),
        "site_advertising_pct": _percentage(_payload_or_previous(payload, previous, "site_advertising_pct", 0), field_name="Реклама сайта"),
        "site_other_cost_rub": _float(_payload_or_previous(payload, previous, "site_other_cost_rub", 0), field_name="Прочие расходы сайта"),
        "raw_json": json.dumps(_merge_raw_excel(payload, previous), ensure_ascii=False),
        "active": 1 if _bool(_payload_or_previous(payload, previous, "active", True), True) else 0,
    }

    if base["certificate_date"] and base["certificate_valid_until"]:
        try:
            date_from = datetime.strptime(base["certificate_date"], "%Y-%m-%d").date()
            date_to = datetime.strptime(base["certificate_valid_until"], "%Y-%m-%d").date()
        except ValueError:
            date_from = date_to = None
        if date_from and date_to and date_to < date_from:
            raise CatalogDbError("Дата окончания действия документа не может быть раньше даты начала")

    if int(base.get("shelf_life_months") or 0) < 0 or int(base.get("shelf_life_months") or 0) > 240:
        raise CatalogDbError("Срок годности должен быть от 1 до 240 месяцев или оставлен пустым")
    alcohol_text = _clean(base.get("alcohol_volume_pct")).replace(",", ".")
    if alcohol_text:
        try:
            alcohol_value = float(alcohol_text)
        except ValueError as exc:
            raise CatalogDbError("Этиловый спирт, %: укажите число от 0 до 99,9") from exc
        if alcohol_value < 0 or alcohol_value > 99.9:
            raise CatalogDbError("Этиловый спирт, %: допустимо значение от 0 до 99,9")
        base["alcohol_volume_pct"] = (f"{alcohol_value:.1f}".rstrip("0").rstrip("."))

    technical_source = dict(previous)
    for key in ("marking_profile", "kiz_required", "product_group", "template_id", "cis_type", "tnved_code"):
        if key in payload:
            technical_source[key] = payload[key]
    if "excel_kiz" in payload and "kiz_required" not in payload:
        technical_source["kiz_required"] = payload["excel_kiz"]

    profile = infer_marking_profile(base, technical_source)
    if base["marking_profile"] == "CUSTOM":
        profile["kiz_required"] = 1 if _bool(technical_source.get("kiz_required")) else 0
        profile["product_group"] = _clean(technical_source.get("product_group"))
        try:
            profile["template_id"] = int(technical_source.get("template_id") or 0)
        except (TypeError, ValueError) as exc:
            raise CatalogDbError("templateId должен быть целым числом") from exc
        profile["cis_type"] = _clean(technical_source.get("cis_type")) or "UNIT"

    certificate_explicit = _payload_or_previous(payload, previous, "certificate_type")
    base.update({
        "marking_profile": profile["marking_profile"],
        "kiz_required": int(profile["kiz_required"]),
        "product_group": profile["product_group"],
        "template_id": int(profile["template_id"]),
        "cis_type": profile["cis_type"],
        "tnved_code": profile["tnved_code"],
        "certificate_type": _infer_certificate_type(base["certificate_number"], certificate_explicit),
    })
    return base


def refresh_automatic_marking_profiles(path: str | Path) -> int:
    changed = 0
    with _connect(path) as conn:
        rows = conn.execute("SELECT * FROM products").fetchall()
        for raw in rows:
            row = dict(raw)
            profile = infer_marking_profile(row, row)
            cert_type = _infer_certificate_type(row.get("certificate_number"), row.get("certificate_type"))
            values = (
                profile["marking_profile"], int(profile["kiz_required"]), profile["product_group"],
                int(profile["template_id"]), profile["cis_type"], profile["tnved_code"],
                cert_type, row["seller_article"],
            )
            current = (
                _normalize_marking_profile(row.get("marking_profile")), int(row.get("kiz_required") or 0),
                _clean(row.get("product_group")), int(row.get("template_id") or 0),
                _clean(row.get("cis_type")) or "UNIT", _clean(row.get("tnved_code")),
                _clean(row.get("certificate_type")),
            )
            if values[:-1] != current:
                conn.execute(
                    "UPDATE products SET marking_profile=?, kiz_required=?, product_group=?, template_id=?, cis_type=?, tnved_code=?, certificate_type=?, updated_at=CURRENT_TIMESTAMP WHERE seller_article=?",
                    values,
                )
                changed += 1
        if changed:
            conn.execute(
                "INSERT INTO catalog_events(event_type, details_json) VALUES('automatic_marking_profiles_refreshed', ?)",
                (json.dumps({"changed": changed}, ensure_ascii=False),),
            )
            _bump_revision(conn)
        conn.commit()
    return changed

def count_products(path: str | Path, *, include_inactive: bool = True) -> int:
    sql = "SELECT COUNT(*) FROM products" + ("" if include_inactive else " WHERE active = 1")
    with _connect(path) as conn:
        return int(conn.execute(sql).fetchone()[0])


def get_product(path: str | Path, seller_article: str) -> dict[str, Any] | None:
    with _connect(path) as conn:
        row = conn.execute("SELECT * FROM products WHERE seller_article = ?", (_clean(seller_article),)).fetchone()
    return dict(row) if row else None


def list_all_products(path: str | Path, *, active_only: bool = True) -> list[dict[str, Any]]:
    where = "WHERE active = 1" if active_only else ""
    with _connect(path) as conn:
        rows = conn.execute(f"SELECT * FROM products {where} ORDER BY seller_article COLLATE NOCASE").fetchall()
    return [dict(row) for row in rows]


def list_products(
    path: str | Path,
    *,
    query: str = "",
    category: str = "",
    marking: str = "",
    active: str = "active",
    page: int = 1,
    page_size: int = 100,
) -> dict[str, Any]:
    filters: list[str] = []
    params: list[Any] = []

    query = _clean(query)
    if query:
        like = f"%{query}%"
        filters.append(
            "(seller_article LIKE ? OR wb_article LIKE ? OR product LIKE ? OR subcategory LIKE ? OR gtin LIKE ? OR certificate_number LIKE ?)"
        )
        params.extend([like] * 6)
    category = _clean(category)
    if category:
        filters.append("category = ?")
        params.append(category)
    if marking == "yes":
        filters.append("kiz_required = 1")
    elif marking == "no":
        filters.append("kiz_required = 0")
    if active == "active":
        filters.append("active = 1")
    elif active == "inactive":
        filters.append("active = 0")

    where = f"WHERE {' AND '.join(filters)}" if filters else ""
    page_size = max(10, min(500, int(page_size or 100)))
    page = max(1, int(page or 1))

    with _connect(path) as conn:
        total = int(conn.execute(f"SELECT COUNT(*) FROM products {where}", params).fetchone()[0])
        pages = max(1, (total + page_size - 1) // page_size)
        page = min(page, pages)
        offset = (page - 1) * page_size
        rows = conn.execute(
            f"SELECT * FROM products {where} ORDER BY active DESC, seller_article COLLATE NOCASE LIMIT ? OFFSET ?",
            [*params, page_size, offset],
        ).fetchall()
        categories = [
            str(row[0])
            for row in conn.execute(
                "SELECT DISTINCT category FROM products WHERE category <> '' ORDER BY category COLLATE NOCASE"
            ).fetchall()
        ]
    return {
        "items": [dict(row) for row in rows],
        "total": total,
        "page": page,
        "pages": pages,
        "page_size": page_size,
        "categories": categories,
    }


def catalog_stats(path: str | Path) -> dict[str, int]:
    with _connect(path) as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) AS total,
                   SUM(CASE WHEN active = 1 THEN 1 ELSE 0 END) AS active,
                   SUM(CASE WHEN active = 1 AND kiz_required = 1 THEN 1 ELSE 0 END) AS marked,
                   SUM(CASE WHEN active = 1 AND kiz_required = 1 AND (gtin = '' OR LENGTH(gtin) <> 14) THEN 1 ELSE 0 END) AS missing_gtin,
                   SUM(CASE WHEN active = 1 AND kiz_required = 1 AND (LENGTH(tnved_code) <> 10 OR certificate_type = '' OR certificate_number = '' OR certificate_date = '' OR certificate_valid_until = '') THEN 1 ELSE 0 END) AS incomplete_compliance
            FROM products
            """
        ).fetchone()
        sample_count = int(conn.execute("SELECT COUNT(*) FROM catalog_samples WHERE active = 1").fetchone()[0])
        name_count = int(conn.execute("SELECT COUNT(*) FROM catalog_names WHERE active = 1").fetchone()[0])
        format_count = int(conn.execute("SELECT COUNT(*) FROM catalog_name_formats WHERE active = 1").fetchone()[0])
    return {
        "total": int(row["total"] or 0),
        "active": int(row["active"] or 0),
        "marked": int(row["marked"] or 0),
        "missing_gtin": int(row["missing_gtin"] or 0),
        "incomplete_compliance": int(row["incomplete_compliance"] or 0),
        "samples": sample_count,
        "names": name_count,
        "name_formats": format_count,
    }


def save_product(
    path: str | Path,
    payload: dict[str, Any],
    *,
    original_article: str | None = None,
    event_type: str = "product_saved",
) -> dict[str, Any]:
    requested_article = _clean(payload.get("seller_article"))
    old_article = _clean(original_article) or requested_article
    with _connect(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        previous_row = conn.execute(
            "SELECT * FROM products WHERE seller_article = ?", (old_article,)
        ).fetchone()
        previous = dict(previous_row) if previous_row else {}
        item = normalize_product(payload, previous)
        new_article = item["seller_article"]
        existing = conn.execute("SELECT seller_article FROM products WHERE seller_article = ?", (old_article,)).fetchone()
        if old_article != new_article:
            conflict = conn.execute("SELECT 1 FROM products WHERE seller_article = ?", (new_article,)).fetchone()
            if conflict:
                raise CatalogDbError(f"Артикул уже существует: {new_article}")
            if existing:
                conn.execute("UPDATE products SET seller_article = ? WHERE seller_article = ?", (new_article, old_article))
                if _table_exists(conn, "economy_cost_history"):
                    conn.execute(
                        "UPDATE economy_cost_history SET seller_article=? WHERE seller_article=?",
                        (new_article, old_article),
                    )

        _upsert_product(conn, item)
        _record_economy_cost(conn, item)
        conn.execute(
            "INSERT INTO catalog_events(event_type, seller_article, details_json) VALUES(?, ?, ?)",
            (event_type, new_article, json.dumps(item, ensure_ascii=False)),
        )
        _bump_revision(conn)
        conn.commit()
    saved = get_product(path, new_article)
    if not saved:
        raise CatalogDbError("Не удалось сохранить товар")
    return saved


def save_products_batch(
    path: str | Path,
    products: Iterable[dict[str, Any]],
    *,
    mode: str = "merge",
    filename: str = "",
) -> dict[str, Any]:
    source_items = list(products)
    mode = mode if mode in {"merge", "replace"} else "merge"
    with _connect(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        normalized: list[dict[str, Any]] = []
        for payload in source_items:
            article = _clean(payload.get("seller_article"))
            previous_row = conn.execute(
                "SELECT * FROM products WHERE seller_article = ?", (article,)
            ).fetchone()
            normalized.append(normalize_product(payload, dict(previous_row) if previous_row else {}))
        if mode == "replace":
            for raw_row in conn.execute("SELECT seller_article, raw_json FROM products").fetchall():
                raw_excel = _merge_raw_excel({"active": False}, {"raw_json": raw_row["raw_json"]})
                conn.execute(
                    "UPDATE products SET active = 0, raw_json = ?, updated_at = CURRENT_TIMESTAMP WHERE seller_article = ?",
                    (json.dumps(raw_excel, ensure_ascii=False), raw_row["seller_article"]),
                )
        written = 0
        for item in normalized:
            _upsert_product(conn, item)
            _record_economy_cost(conn, item)
            written += 1
        conn.execute(
            "INSERT INTO catalog_events(event_type, details_json) VALUES('products_imported', ?)",
            (json.dumps({"filename": filename, "mode": mode, "count": written}, ensure_ascii=False),),
        )
        _bump_revision(conn)
        conn.commit()
    return {"read": len(normalized), "written": written, "mode": mode}


def set_product_active(path: str | Path, seller_article: str, active: bool) -> None:
    article = _clean(seller_article)
    with _connect(path) as conn:
        existing = conn.execute("SELECT raw_json FROM products WHERE seller_article = ?", (article,)).fetchone()
        if not existing:
            raise CatalogDbError(f"Товар не найден: {article}")
        raw_excel = _merge_raw_excel({"active": active}, {"raw_json": existing["raw_json"]})
        cur = conn.execute(
            "UPDATE products SET active = ?, raw_json = ?, updated_at = CURRENT_TIMESTAMP WHERE seller_article = ?",
            (1 if active else 0, json.dumps(raw_excel, ensure_ascii=False), article),
        )
        if not cur.rowcount:
            raise CatalogDbError(f"Товар не найден: {article}")
        if active:
            current = conn.execute(
                "SELECT * FROM products WHERE seller_article=?", (article,)
            ).fetchone()
            if current:
                _record_economy_cost(conn, dict(current))
        conn.execute(
            "INSERT INTO catalog_events(event_type, seller_article, details_json) VALUES('product_active_changed', ?, ?)",
            (article, json.dumps({"active": bool(active)}, ensure_ascii=False)),
        )
        _bump_revision(conn)
        conn.commit()


def bulk_apply_compliance(
    path: str | Path,
    articles: Iterable[str],
    values: dict[str, Any],
) -> int:
    ids = sorted({_clean(article) for article in articles if _clean(article)})
    if not ids:
        raise CatalogDbError("Не выбраны товары")

    allowed = {
        "tnved_code",
        "certificate_type",
        "certificate_number",
        "certificate_date",
        "certificate_valid_until",
    }
    updates: dict[str, Any] = {}
    for key in allowed:
        if key not in values:
            continue
        if key in {"certificate_date", "certificate_valid_until"}:
            updates[key] = _normalize_date(values[key])
        elif key == "tnved_code":
            updates[key] = _normalize_tnved(values[key])
        elif key == "certificate_type":
            updates[key] = _normalize_certificate_type(values[key])
        else:
            updates[key] = _clean(values[key])
    if "certificate_number" in updates and "certificate_type" not in updates:
        updates["certificate_type"] = _infer_certificate_type(updates["certificate_number"])
    if updates.get("certificate_date") and updates.get("certificate_valid_until"):
        if updates["certificate_valid_until"] < updates["certificate_date"]:
            raise CatalogDbError("Дата окончания действия документа не может быть раньше даты начала")
    if not updates:
        raise CatalogDbError("Нет полей для массового изменения")

    clauses = [f"{key} = ?" for key in updates]
    placeholders = ",".join("?" for _ in ids)
    params = [*updates.values(), *ids]
    with _connect(path) as conn:
        cur = conn.execute(
            f"UPDATE products SET {', '.join(clauses)}, updated_at = CURRENT_TIMESTAMP WHERE seller_article IN ({placeholders})",
            params,
        )
        for raw_row in conn.execute(
            f"SELECT seller_article, raw_json FROM products WHERE seller_article IN ({placeholders})", ids
        ).fetchall():
            raw_excel = _merge_raw_excel(updates, {"raw_json": raw_row["raw_json"]})
            conn.execute(
                "UPDATE products SET raw_json = ? WHERE seller_article = ?",
                (json.dumps(raw_excel, ensure_ascii=False), raw_row["seller_article"]),
            )
        conn.execute(
            "INSERT INTO catalog_events(event_type, details_json) VALUES('bulk_compliance', ?)",
            (json.dumps({"articles": ids, "values": updates}, ensure_ascii=False),),
        )
        _bump_revision(conn)
        conn.commit()
        return int(cur.rowcount or 0)


def list_samples(path: str | Path, *, active_only: bool = True) -> list[dict[str, Any]]:
    where = "WHERE active = 1" if active_only else ""
    with _connect(path) as conn:
        rows = conn.execute(
            f"SELECT * FROM catalog_samples {where} ORDER BY CAST(sample_no AS INTEGER), name COLLATE NOCASE"
        ).fetchall()
    return [dict(row) for row in rows]


def save_sample(path: str | Path, payload: dict[str, Any], *, original_name: str | None = None) -> dict[str, Any]:
    name = _clean(payload.get("name") or payload.get("Name"))
    if not name:
        raise CatalogDbError("Название аромата обязательно")
    old_name = _clean(original_name) or name
    data = {
        "sample_no": _clean(payload.get("sample_no") or payload.get("№")),
        "name": name,
        "wb_code": _clean(payload.get("wb_code") or payload.get("WB code")),
        "qr_link": _clean(payload.get("qr_link") or payload.get("QR link")),
        "active": 1 if _bool(payload.get("active"), True) else 0,
    }
    with _connect(path) as conn:
        if old_name != name:
            conflict = conn.execute("SELECT 1 FROM catalog_samples WHERE name = ?", (name,)).fetchone()
            if conflict:
                raise CatalogDbError(f"Аромат уже существует: {name}")
            conn.execute("UPDATE catalog_samples SET name = ? WHERE name = ?", (name, old_name))
        conn.execute(
            """
            INSERT INTO catalog_samples(sample_no, name, wb_code, qr_link, active, updated_at)
            VALUES(:sample_no, :name, :wb_code, :qr_link, :active, CURRENT_TIMESTAMP)
            ON CONFLICT(name) DO UPDATE SET
                sample_no=excluded.sample_no, wb_code=excluded.wb_code,
                qr_link=excluded.qr_link, active=excluded.active,
                updated_at=CURRENT_TIMESTAMP
            """,
            data,
        )
        _bump_revision(conn)
        conn.commit()
        row = conn.execute("SELECT * FROM catalog_samples WHERE name = ?", (name,)).fetchone()
    return dict(row)


def save_samples_batch(path: str | Path, samples: Iterable[dict[str, Any]], *, mode: str = "merge") -> dict[str, int]:
    items = list(samples)
    with _connect(path) as conn:
        if mode == "replace":
            conn.execute("UPDATE catalog_samples SET active = 0, updated_at = CURRENT_TIMESTAMP")
        written = 0
        for payload in items:
            name = _clean(payload.get("name") or payload.get("Name"))
            if not name:
                continue
            data = {
                "sample_no": _clean(payload.get("sample_no") or payload.get("№")),
                "name": name,
                "wb_code": _clean(payload.get("wb_code") or payload.get("WB code")),
                "qr_link": _clean(payload.get("qr_link") or payload.get("QR link")),
                "active": 1 if _bool(payload.get("active"), True) else 0,
            }
            conn.execute(
                """
                INSERT INTO catalog_samples(sample_no, name, wb_code, qr_link, active, updated_at)
                VALUES(:sample_no, :name, :wb_code, :qr_link, :active, CURRENT_TIMESTAMP)
                ON CONFLICT(name) DO UPDATE SET sample_no=excluded.sample_no,
                    wb_code=excluded.wb_code, qr_link=excluded.qr_link,
                    active=excluded.active, updated_at=CURRENT_TIMESTAMP
                """,
                data,
            )
            written += 1
        _bump_revision(conn)
        conn.commit()
    return {"read": len(items), "written": written}


def list_names(path: str | Path, *, active_only: bool = True) -> list[dict[str, Any]]:
    where = "WHERE active = 1" if active_only else ""
    with _connect(path) as conn:
        rows = conn.execute(
            f"SELECT * FROM catalog_names {where} ORDER BY CAST(name_no AS INTEGER), name COLLATE NOCASE"
        ).fetchall()
    return [dict(row) for row in rows]


def save_names_batch(path: str | Path, names: Iterable[dict[str, Any]], *, mode: str = "merge") -> dict[str, int]:
    items = list(names)
    with _connect(path) as conn:
        if mode == "replace":
            conn.execute("UPDATE catalog_names SET active = 0, updated_at = CURRENT_TIMESTAMP")
        written = 0
        for payload in items:
            name = _clean(payload.get("name") or payload.get("Название"))
            if not name:
                continue
            data = {
                "name_no": _clean(payload.get("name_no") or payload.get("№")),
                "name": name,
                "inspiration_name": _clean(payload.get("inspiration_name") or payload.get("Название мотива")),
                "active": 1 if _bool(payload.get("active"), True) else 0,
            }
            conn.execute(
                """
                INSERT INTO catalog_names(name_no, name, inspiration_name, active, updated_at)
                VALUES(:name_no, :name, :inspiration_name, :active, CURRENT_TIMESTAMP)
                ON CONFLICT(name) DO UPDATE SET name_no=excluded.name_no,
                    inspiration_name=excluded.inspiration_name, active=excluded.active,
                    updated_at=CURRENT_TIMESTAMP
                """,
                data,
            )
            written += 1
        _bump_revision(conn)
        conn.commit()
    return {"read": len(items), "written": written}


def list_name_formats(path: str | Path, *, active_only: bool = True) -> list[dict[str, Any]]:
    where = "WHERE active = 1" if active_only else ""
    with _connect(path) as conn:
        rows = conn.execute(
            f"SELECT * FROM catalog_name_formats {where} ORDER BY CAST(format_no AS INTEGER), name COLLATE NOCASE"
        ).fetchall()
    return [dict(row) for row in rows]


def save_name_formats_batch(path: str | Path, formats: Iterable[dict[str, Any]], *, mode: str = "merge") -> dict[str, int]:
    items = list(formats)
    with _connect(path) as conn:
        if mode == "replace":
            conn.execute("UPDATE catalog_name_formats SET active = 0, updated_at = CURRENT_TIMESTAMP")
        written = 0
        for payload in items:
            name = _clean(payload.get("name") or payload.get("Формат"))
            if not name:
                continue
            data = {
                "format_no": _clean(payload.get("format_no") or payload.get("№")),
                "name": name,
                "active": 1 if _bool(payload.get("active"), True) else 0,
            }
            conn.execute(
                """
                INSERT INTO catalog_name_formats(format_no, name, active, updated_at)
                VALUES(:format_no, :name, :active, CURRENT_TIMESTAMP)
                ON CONFLICT(name) DO UPDATE SET format_no=excluded.format_no,
                    active=excluded.active, updated_at=CURRENT_TIMESTAMP
                """,
                data,
            )
            written += 1
        _bump_revision(conn)
        conn.commit()
    return {"read": len(items), "written": written}


def record_import(
    path: str | Path,
    *,
    filename: str,
    mode: str,
    products_read: int,
    products_written: int,
    samples_read: int,
    samples_written: int,
    names_read: int = 0,
    names_written: int = 0,
    formats_read: int = 0,
    formats_written: int = 0,
    warnings: list[str] | None = None,
) -> None:
    with _connect(path) as conn:
        conn.execute(
            """
            INSERT INTO catalog_imports(
                filename, mode, products_read, products_written,
                samples_read, samples_written, names_read, names_written,
                formats_read, formats_written, warnings_json
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                filename,
                mode,
                int(products_read),
                int(products_written),
                int(samples_read),
                int(samples_written),
                int(names_read),
                int(names_written),
                int(formats_read),
                int(formats_written),
                json.dumps(warnings or [], ensure_ascii=False),
            ),
        )
        conn.commit()


def database_backup(path: str | Path, backup_dir: str | Path, *, reason: str = "manual") -> Path:
    source = Path(path)
    if not source.exists():
        raise CatalogDbError("Файл базы данных еще не создан")
    target_dir = Path(backup_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    safe_reason = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in reason)[:40] or "backup"
    target = target_dir / f"fbe_{datetime.now().strftime('%Y-%m-%d_%H%M%S_%f')}_{safe_reason}.db"
    src = _connect(source)
    dst = sqlite3.connect(target)
    try:
        src.backup(dst)
        dst.commit()
    finally:
        dst.close()
        src.close()
    return target


def prune_backups(backup_dir: str | Path, keep: int = 30) -> None:
    folder = Path(backup_dir)
    if not folder.exists():
        return
    files = sorted(folder.glob("fbe_*.db"), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in files[max(1, int(keep)):]:
        try:
            old.unlink()
        except OSError:
            pass
