from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

ECONOMY_SCHEMA_VERSION = 11

DDL = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA busy_timeout=30000;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS economy_sync_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    date_from TEXT NOT NULL DEFAULT '',
    date_to TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    rows_read INTEGER NOT NULL DEFAULT 0,
    rows_written INTEGER NOT NULL DEFAULT 0,
    message TEXT NOT NULL DEFAULT '',
    details_json TEXT NOT NULL DEFAULT '{}',
    started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    finished_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_economy_sync_source_date
ON economy_sync_runs(source, started_at DESC);

CREATE TABLE IF NOT EXISTS economy_orders (
    event_key TEXT PRIMARY KEY,
    order_date TEXT NOT NULL DEFAULT '',
    last_change_date TEXT NOT NULL DEFAULT '',
    supplier_article TEXT NOT NULL DEFAULT '',
    nm_id TEXT NOT NULL DEFAULT '',
    srid TEXT NOT NULL DEFAULT '',
    warehouse_name TEXT NOT NULL DEFAULT '',
    region_name TEXT NOT NULL DEFAULT '',
    quantity REAL NOT NULL DEFAULT 1,
    gross_amount REAL NOT NULL DEFAULT 0,
    seller_amount REAL NOT NULL DEFAULT 0,
    discounted_amount REAL NOT NULL DEFAULT 0,
    is_cancel INTEGER NOT NULL DEFAULT 0,
    cancel_date TEXT NOT NULL DEFAULT '',
    raw_json TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_economy_orders_date ON economy_orders(order_date);
CREATE INDEX IF NOT EXISTS idx_economy_orders_article ON economy_orders(supplier_article, nm_id);

CREATE TABLE IF NOT EXISTS economy_sales (
    sale_key TEXT PRIMARY KEY,
    sale_date TEXT NOT NULL DEFAULT '',
    last_change_date TEXT NOT NULL DEFAULT '',
    supplier_article TEXT NOT NULL DEFAULT '',
    nm_id TEXT NOT NULL DEFAULT '',
    srid TEXT NOT NULL DEFAULT '',
    sale_id TEXT NOT NULL DEFAULT '',
    doc_type TEXT NOT NULL DEFAULT '',
    quantity REAL NOT NULL DEFAULT 1,
    amount REAL NOT NULL DEFAULT 0,
    seller_amount REAL NOT NULL DEFAULT 0,
    for_pay_amount REAL NOT NULL DEFAULT 0,
    is_return INTEGER NOT NULL DEFAULT 0,
    raw_json TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_economy_sales_date ON economy_sales(sale_date);
CREATE INDEX IF NOT EXISTS idx_economy_sales_article ON economy_sales(supplier_article, nm_id);

CREATE TABLE IF NOT EXISTS economy_finance_reports (
    report_id TEXT PRIMARY KEY,
    date_from TEXT NOT NULL DEFAULT '',
    date_to TEXT NOT NULL DEFAULT '',
    create_date TEXT NOT NULL DEFAULT '',
    currency TEXT NOT NULL DEFAULT 'RUB',
    report_type TEXT NOT NULL DEFAULT '',
    raw_json TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS economy_finance_rows (
    rrd_id TEXT PRIMARY KEY,
    report_id TEXT NOT NULL DEFAULT '',
    operation_date TEXT NOT NULL DEFAULT '',
    sale_date TEXT NOT NULL DEFAULT '',
    supplier_article TEXT NOT NULL DEFAULT '',
    nm_id TEXT NOT NULL DEFAULT '',
    srid TEXT NOT NULL DEFAULT '',
    doc_type TEXT NOT NULL DEFAULT '',
    operation_name TEXT NOT NULL DEFAULT '',
    bonus_type_name TEXT NOT NULL DEFAULT '',
    payment_processing TEXT NOT NULL DEFAULT '',
    delivery_method TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    quantity REAL NOT NULL DEFAULT 1,
    retail_price REAL NOT NULL DEFAULT 0,
    retail_amount REAL NOT NULL DEFAULT 0,
    retail_price_with_disc REAL NOT NULL DEFAULT 0,
    sale_percent REAL NOT NULL DEFAULT 0,
    product_discount_for_report REAL NOT NULL DEFAULT 0,
    seller_promo REAL NOT NULL DEFAULT 0,
    spp REAL NOT NULL DEFAULT 0,
    kvw_base REAL NOT NULL DEFAULT 0,
    kvw REAL NOT NULL DEFAULT 0,
    vw REAL NOT NULL DEFAULT 0,
    vw_nds REAL NOT NULL DEFAULT 0,
    ppvz_for_pay REAL NOT NULL DEFAULT 0,
    ppvz_sales_commission REAL NOT NULL DEFAULT 0,
    commission_percent REAL NOT NULL DEFAULT 0,
    acquiring_fee REAL NOT NULL DEFAULT 0,
    delivery_rub REAL NOT NULL DEFAULT 0,
    storage_fee REAL NOT NULL DEFAULT 0,
    acceptance_rub REAL NOT NULL DEFAULT 0,
    deduction REAL NOT NULL DEFAULT 0,
    penalty REAL NOT NULL DEFAULT 0,
    additional_payment REAL NOT NULL DEFAULT 0,
    rebill_logistic_cost REAL NOT NULL DEFAULT 0,
    raw_json TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_economy_finance_date ON economy_finance_rows(operation_date, sale_date);
CREATE INDEX IF NOT EXISTS idx_economy_finance_article ON economy_finance_rows(supplier_article, nm_id);
CREATE INDEX IF NOT EXISTS idx_economy_finance_report ON economy_finance_rows(report_id);

CREATE TABLE IF NOT EXISTS economy_ad_campaigns (
    advert_id TEXT PRIMARY KEY,
    name TEXT NOT NULL DEFAULT '',
    status INTEGER NOT NULL DEFAULT 0,
    campaign_type INTEGER NOT NULL DEFAULT 0,
    payment_type TEXT NOT NULL DEFAULT '',
    create_time TEXT NOT NULL DEFAULT '',
    change_time TEXT NOT NULL DEFAULT '',
    start_time TEXT NOT NULL DEFAULT '',
    end_time TEXT NOT NULL DEFAULT '',
    daily_budget REAL NOT NULL DEFAULT 0,
    raw_json TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_economy_ad_campaigns_status
ON economy_ad_campaigns(status, advert_id);

CREATE TABLE IF NOT EXISTS economy_ad_stats (
    stat_date TEXT NOT NULL,
    advert_id TEXT NOT NULL,
    nm_id TEXT NOT NULL DEFAULT '',
    views REAL NOT NULL DEFAULT 0,
    clicks REAL NOT NULL DEFAULT 0,
    spend REAL NOT NULL DEFAULT 0,
    atbs REAL NOT NULL DEFAULT 0,
    orders REAL NOT NULL DEFAULT 0,
    shks REAL NOT NULL DEFAULT 0,
    revenue REAL NOT NULL DEFAULT 0,
    ctr REAL NOT NULL DEFAULT 0,
    cpc REAL NOT NULL DEFAULT 0,
    cpm REAL NOT NULL DEFAULT 0,
    cr REAL NOT NULL DEFAULT 0,
    raw_json TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY(stat_date, advert_id, nm_id)
);
CREATE INDEX IF NOT EXISTS idx_economy_ads_date ON economy_ad_stats(stat_date);
CREATE INDEX IF NOT EXISTS idx_economy_ads_nm ON economy_ad_stats(nm_id);


CREATE TABLE IF NOT EXISTS economy_ad_expenses (
    expense_key TEXT PRIMARY KEY,
    expense_date TEXT NOT NULL DEFAULT '',
    expense_time TEXT NOT NULL DEFAULT '',
    advert_id TEXT NOT NULL DEFAULT '',
    campaign_name TEXT NOT NULL DEFAULT '',
    advert_type INTEGER NOT NULL DEFAULT 0,
    payment_type TEXT NOT NULL DEFAULT '',
    expense_sum REAL NOT NULL DEFAULT 0,
    upd_num TEXT NOT NULL DEFAULT '',
    raw_json TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_economy_ad_expenses_date
ON economy_ad_expenses(expense_date, advert_id);

CREATE TABLE IF NOT EXISTS economy_cost_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    seller_article TEXT NOT NULL,
    effective_from TEXT NOT NULL,
    cost_price_rub REAL NOT NULL DEFAULT 0,
    materials_cost_rub REAL NOT NULL DEFAULT 0,
    packaging_cost_rub REAL NOT NULL DEFAULT 0,
    labor_cost_rub REAL NOT NULL DEFAULT 0,
    marking_cost_rub REAL NOT NULL DEFAULT 0,
    other_unit_cost_rub REAL NOT NULL DEFAULT 0,
    source TEXT NOT NULL DEFAULT 'manual',
    notes TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_economy_cost_article_date
ON economy_cost_history(seller_article, effective_from DESC, id DESC);
"""


def _connect(path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    columns = {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def _backfill_finance_v5(conn: sqlite3.Connection) -> int:
    """Reparse stored raw Finance API JSON using current field semantics."""
    updated = 0
    rows = conn.execute("SELECT rrd_id, raw_json FROM economy_finance_rows").fetchall()
    for row in rows:
        try:
            payload = json.loads(str(row["raw_json"] or "{}"))
        except Exception:
            continue
        if not isinstance(payload, dict):
            continue

        doc_type = _clean(_first(payload, "docTypeName", "doc_type_name"))
        operation_name = _clean(_first(
            payload,
            "sellerOperName", "supplierOperName", "supplier_oper_name", "operationName",
        ))

        transaction = lambda *names: _transaction_money(
            _first(payload, *names, default=0), doc_type, operation_name
        )
        commission = lambda *names: _commission_money(
            _first(payload, *names, default=0), doc_type, operation_name
        )

        conn.execute(
            """
            UPDATE economy_finance_rows SET
                supplier_article=?, doc_type=?, operation_name=?, bonus_type_name=?,
                payment_processing=?, delivery_method=?,
                retail_price=?, retail_amount=?, retail_price_with_disc=?,
                sale_percent=?, product_discount_for_report=?, seller_promo=?,
                spp=?, kvw_base=?, kvw=?, vw=?, vw_nds=?,
                ppvz_for_pay=?, ppvz_sales_commission=?, commission_percent=?,
                acquiring_fee=?, delivery_rub=?, storage_fee=?, acceptance_rub=?,
                deduction=?, penalty=?, additional_payment=?, rebill_logistic_cost=?
            WHERE rrd_id=?
            """,
            (
                _clean(_first(payload, "vendorCode", "supplierArticle", "supplier_article", "saName")),
                doc_type,
                operation_name,
                _clean(_first(payload, "bonusTypeName", "bonus_type_name")),
                _clean(_first(payload, "paymentProcessing", "payment_processing")),
                _clean(_first(payload, "deliveryMethod", "delivery_method")),
                abs(_money(_first(payload, "retailPrice", "retail_price", default=0))),
                transaction("retailAmount", "retail_amount"),
                abs(_money(_first(payload, "retailPriceWithDisc", "retail_price_withdisc_rub", default=0))),
                _money(_first(payload, "salePercent", "sale_percent", default=0)),
                _money(_first(payload, "productDiscountForReport", "product_discount_for_report", default=0)),
                _money(_first(payload, "sellerPromo", "supplierPromo", "supplier_promo", default=0)),
                _money(_first(payload, "spp", "ppvzSppPrc", "ppvz_spp_prc", default=0)),
                _money(_first(payload, "kvwBase", "ppvzKvwPrcBase", "ppvz_kvw_prc_base", default=0)),
                _money(_first(payload, "kvw", "ppvzKvwPrc", "ppvz_kvw_prc", default=0)),
                commission("vw", "ppvzVw", "ppvz_vw"),
                commission("vwNds", "ppvzVwNds", "ppvz_vw_nds"),
                transaction("forPay", "ppvzForPay", "ppvz_for_pay"),
                commission("ppvzSalesCommission", "ppvz_sales_commission"),
                _money(_first(payload, "commissionPercent", "commission_percent", default=0)),
                _money(_first(payload, "acquiringFee", "acquiring_fee", default=0)),
                _money(_first(payload, "deliveryService", "deliveryRub", "delivery_rub", default=0)),
                _money(_first(payload, "storageFee", "paidStorage", "storage_fee", "paid_storage", default=0)),
                _money(_first(payload, "acceptance", "acceptanceRub", "paidAcceptance", "acceptance_rub", "paid_acceptance", default=0)),
                _money(_first(payload, "deduction", default=0)),
                _money(_first(payload, "penalty", default=0)),
                _money(_first(payload, "additionalPayment", "additional_payment", default=0)),
                _money(_first(payload, "rebillLogisticCost", "rebill_logistic_cost", default=0)),
                str(row["rrd_id"]),
            ),
        )
        updated += 1
    return updated

def _ensure_economy_schema_base_impl(path: str | Path) -> Path:
    db_path = Path(path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with _connect(db_path) as conn:
        previous_row = conn.execute(
            "SELECT value FROM schema_meta WHERE key='economy_schema_version'"
        ).fetchone() if conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='schema_meta'").fetchone() else None
        try:
            previous_version = int(previous_row[0]) if previous_row else 0
        except Exception:
            previous_version = 0
        conn.executescript(DDL)
        finance_columns = {
            "operation_name": "TEXT NOT NULL DEFAULT ''",
            "bonus_type_name": "TEXT NOT NULL DEFAULT ''",
            "payment_processing": "TEXT NOT NULL DEFAULT ''",
            "delivery_method": "TEXT NOT NULL DEFAULT ''",
            "retail_price": "REAL NOT NULL DEFAULT 0",
            "retail_price_with_disc": "REAL NOT NULL DEFAULT 0",
            "sale_percent": "REAL NOT NULL DEFAULT 0",
            "product_discount_for_report": "REAL NOT NULL DEFAULT 0",
            "seller_promo": "REAL NOT NULL DEFAULT 0",
            "spp": "REAL NOT NULL DEFAULT 0",
            "kvw_base": "REAL NOT NULL DEFAULT 0",
            "kvw": "REAL NOT NULL DEFAULT 0",
            "vw": "REAL NOT NULL DEFAULT 0",
            "vw_nds": "REAL NOT NULL DEFAULT 0",
            "ppvz_sales_commission": "REAL NOT NULL DEFAULT 0",
            "commission_percent": "REAL NOT NULL DEFAULT 0",
        }
        for column, definition in finance_columns.items():
            _ensure_column(conn, "economy_finance_rows", column, definition)
        _ensure_column(conn, "economy_sales", "for_pay_amount", "REAL NOT NULL DEFAULT 0")
        if previous_version < 6:
            _backfill_finance_v5(conn)
        conn.execute(
            "INSERT INTO schema_meta(key, value) VALUES('economy_schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(ECONOMY_SCHEMA_VERSION),),
        )
        conn.commit()
    return db_path


def _clean(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def _money(value: Any) -> float:
    if value is None or value == "":
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip().replace(" ", "").replace("\u00a0", "").replace(",", "."))
    except Exception:
        return 0.0


def _first(row: dict[str, Any], *names: str, default: Any = "") -> Any:
    for name in names:
        if name in row and row.get(name) not in (None, ""):
            return row.get(name)
    return default


def _date_only(value: Any) -> str:
    text = _clean(value)
    if not text:
        return ""
    return text[:10]


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return _clean(value).lower() in {"1", "true", "yes", "да"}


def _document_sign(doc_type: str, operation_name: str = "") -> int:
    """Economic sign for sale/return documents.

    Finance API usually sends signed monetary values, but some report versions
    keep return amounts positive.  Storno-return restores a sale, while
    storno-sale reverses one.  The document sign is therefore used only for
    transaction fields (quantity, sale amount, forPay and sales commission),
    never for service charges such as logistics or deductions.
    """
    lowered = f"{_clean(doc_type)} {_clean(operation_name)}".lower().replace("ё", "е")
    if "сторно возврат" in lowered or "reversal of return" in lowered:
        return 1
    if "сторно продаж" in lowered or "reversal of sale" in lowered:
        return -1
    if "возврат" in lowered or "return" in lowered:
        return -1
    return 1


def _transaction_money(value: Any, doc_type: str, operation_name: str = "") -> float:
    """Normalize a transaction amount while preserving explicit API signs."""
    raw = _money(value)
    if raw == 0:
        return 0.0
    if raw < 0:
        return raw
    return abs(raw) * _document_sign(doc_type, operation_name)


def _commission_money(value: Any, doc_type: str, operation_name: str = "") -> float:
    """Normalize WB sales commission as an expense on sales and reversal on returns.

    Legacy report versions could expose the commission with the opposite sign,
    whereas the current Finance API sample uses a positive amount.  The
    document type is more stable than that historical sign convention.
    """
    raw = _money(value)
    if raw == 0:
        return 0.0
    return abs(raw) * _document_sign(doc_type, operation_name)


def record_sync_run(
    path: str | Path,
    *,
    source: str,
    date_from: str,
    date_to: str,
    status: str,
    rows_read: int,
    rows_written: int,
    message: str = "",
    details: dict[str, Any] | None = None,
    started_at: str | None = None,
) -> int:
    with _connect(path) as conn:
        cur = conn.execute(
            """
            INSERT INTO economy_sync_runs(
                source, date_from, date_to, status, rows_read, rows_written,
                message, details_json, started_at, finished_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            """,
            (
                source,
                date_from,
                date_to,
                status,
                int(rows_read),
                int(rows_written),
                message[:4000],
                json.dumps(details or {}, ensure_ascii=False),
                started_at or datetime.now().isoformat(timespec="seconds"),
            ),
        )
        conn.commit()
        return int(cur.lastrowid)


def get_incremental_cursor(path: str | Path, source: str, date_from: str = "", date_to: str = "") -> str:
    """Return the latest lastChangeDate already stored for an operational source.

    The WB statistics endpoints are incremental by lastChangeDate.  A short
    overlap is applied by the caller so corrections at the boundary are not
    missed.
    """
    table = {"orders": "economy_orders", "sales": "economy_sales"}.get(source)
    date_column = {"orders": "order_date", "sales": "sale_date"}.get(source)
    if not table or not date_column:
        return ""
    where = ["last_change_date <> ''"]
    params: list[Any] = []
    if date_from:
        where.append(f"{date_column} >= ?")
        params.append(_date_only(date_from))
    if date_to:
        where.append(f"{date_column} <= ?")
        params.append(_date_only(date_to))
    sql = f"SELECT MAX(last_change_date) FROM {table} WHERE " + " AND ".join(where)
    with _connect(path) as conn:
        row = conn.execute(sql, params).fetchone()
        return _clean(row[0] if row else "")


def save_orders(path: str | Path, rows: Iterable[dict[str, Any]]) -> int:
    written = 0
    with _connect(path) as conn:
        for index, row in enumerate(rows):
            srid = _clean(_first(row, "srid", "odid"))
            key = srid or _clean(_first(row, "gNumber", "orderId", "id"))
            if not key:
                key = f"order:{_clean(_first(row, 'supplierArticle'))}:{_clean(_first(row, 'date'))}:{index}"
            total = _money(_first(row, "totalPrice", "price", default=0))
            discount_pct = _money(_first(row, "discountPercent", default=0))
            seller_amount = _money(_first(row, "priceWithDisc", default=0))
            if not seller_amount and total:
                seller_amount = total * (1 - discount_pct / 100)
            finished = _money(_first(row, "finishedPrice", "priceWithDisc", default=0))
            if not finished and total:
                finished = total * (1 - discount_pct / 100)
            conn.execute(
                """
                INSERT INTO economy_orders(
                    event_key, order_date, last_change_date, supplier_article, nm_id,
                    srid, warehouse_name, region_name, quantity, gross_amount,
                    seller_amount, discounted_amount, is_cancel, cancel_date, raw_json, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(event_key) DO UPDATE SET
                    order_date=excluded.order_date,
                    last_change_date=excluded.last_change_date,
                    supplier_article=excluded.supplier_article,
                    nm_id=excluded.nm_id,
                    srid=excluded.srid,
                    warehouse_name=excluded.warehouse_name,
                    region_name=excluded.region_name,
                    quantity=excluded.quantity,
                    gross_amount=excluded.gross_amount,
                    seller_amount=excluded.seller_amount,
                    discounted_amount=excluded.discounted_amount,
                    is_cancel=excluded.is_cancel,
                    cancel_date=excluded.cancel_date,
                    raw_json=excluded.raw_json,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (
                    key,
                    _date_only(_first(row, "date", "orderDate")),
                    _clean(_first(row, "lastChangeDate")),
                    _clean(_first(row, "supplierArticle", "vendorCode")),
                    _clean(_first(row, "nmId", "nmID")),
                    srid,
                    _clean(_first(row, "warehouseName", "warehouse")),
                    _clean(_first(row, "regionName", "region")),
                    _money(_first(row, "quantity", default=1)) or 1,
                    total,
                    seller_amount,
                    finished,
                    1 if _bool(_first(row, "isCancel", default=False)) else 0,
                    _date_only(_first(row, "cancelDate")),
                    json.dumps(row, ensure_ascii=False),
                ),
            )
            written += 1
        conn.commit()
    return written


def save_sales(path: str | Path, rows: Iterable[dict[str, Any]]) -> int:
    written = 0
    with _connect(path) as conn:
        for index, row in enumerate(rows):
            sale_id = _clean(_first(row, "saleID", "saleId"))
            srid = _clean(_first(row, "srid", "odid"))
            key = sale_id or srid or f"sale:{_clean(_first(row, 'supplierArticle'))}:{_clean(_first(row, 'date'))}:{index}"
            doc_type = _clean(_first(row, "docTypeName", "orderType"))
            is_return = sale_id.upper().startswith("R") or "возврат" in doc_type.lower() or "return" in doc_type.lower()
            amount = _money(_first(row, "finishedPrice", "priceWithDisc", "forPay", "totalPrice", default=0))
            seller_amount = abs(_money(_first(row, "priceWithDisc", "finishedPrice", "forPay", "totalPrice", default=0)))
            for_pay_amount = abs(_money(_first(row, "forPay", default=0)))
            conn.execute(
                """
                INSERT INTO economy_sales(
                    sale_key, sale_date, last_change_date, supplier_article, nm_id,
                    srid, sale_id, doc_type, quantity, amount, seller_amount,
                    for_pay_amount, is_return, raw_json, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(sale_key) DO UPDATE SET
                    sale_date=excluded.sale_date,
                    last_change_date=excluded.last_change_date,
                    supplier_article=excluded.supplier_article,
                    nm_id=excluded.nm_id,
                    srid=excluded.srid,
                    sale_id=excluded.sale_id,
                    doc_type=excluded.doc_type,
                    quantity=excluded.quantity,
                    amount=excluded.amount,
                    seller_amount=excluded.seller_amount,
                    for_pay_amount=excluded.for_pay_amount,
                    is_return=excluded.is_return,
                    raw_json=excluded.raw_json,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (
                    key,
                    _date_only(_first(row, "date", "saleDate")),
                    _clean(_first(row, "lastChangeDate")),
                    _clean(_first(row, "supplierArticle", "vendorCode")),
                    _clean(_first(row, "nmId", "nmID")),
                    srid,
                    sale_id,
                    doc_type,
                    _money(_first(row, "quantity", default=1)) or 1,
                    amount,
                    seller_amount,
                    for_pay_amount,
                    1 if is_return else 0,
                    json.dumps(row, ensure_ascii=False),
                ),
            )
            written += 1
        conn.commit()
    return written


AD_STATUS_LABELS = {
    -1: "Удалена", 4: "Готова", 7: "Завершена", 8: "Отклонена",
    9: "Активна", 11: "Приостановлена",
}
AD_TYPE_LABELS = {4: "Каталог", 5: "Карточка", 6: "Поиск", 7: "Рекомендации", 8: "Автоматическая", 9: "Единая ставка"}


def save_ad_campaigns(path: str | Path, rows: Iterable[dict[str, Any]]) -> int:
    written = 0
    with _connect(path) as conn:
        for row in rows:
            advert_id = _clean(_first(row, "advertId", "advert_id", "id"))
            if not advert_id:
                continue
            conn.execute(
                """
                INSERT INTO economy_ad_campaigns(
                    advert_id, name, status, campaign_type, payment_type,
                    create_time, change_time, start_time, end_time, daily_budget,
                    raw_json, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(advert_id) DO UPDATE SET
                    name=CASE WHEN excluded.name<>'' THEN excluded.name ELSE economy_ad_campaigns.name END,
                    status=excluded.status, campaign_type=excluded.campaign_type,
                    payment_type=CASE WHEN excluded.payment_type<>'' THEN excluded.payment_type ELSE economy_ad_campaigns.payment_type END,
                    create_time=excluded.create_time, change_time=excluded.change_time,
                    start_time=excluded.start_time, end_time=excluded.end_time,
                    daily_budget=excluded.daily_budget, raw_json=excluded.raw_json,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (
                    advert_id,
                    _clean(_first(row, "name", "advertName", "campaignName", "campName")),
                    int(_money(_first(row, "status", default=0))),
                    int(_money(_first(row, "type", "advertType", "campaignType", default=0))),
                    _clean(_first(row, "paymentType", "payment_type")),
                    _clean(_first(row, "createTime", "create_time")),
                    _clean(_first(row, "changeTime", "change_time")),
                    _clean(_first(row, "startTime", "start_time")),
                    _clean(_first(row, "endTime", "end_time")),
                    _money(_first(row, "dailyBudget", "daily_budget", default=0)),
                    json.dumps(row, ensure_ascii=False),
                ),
            )
            written += 1
        conn.commit()
    return written


def save_ad_expenses(path: str | Path, rows: Iterable[dict[str, Any]]) -> int:
    written = 0
    with _connect(path) as conn:
        for index, row in enumerate(rows):
            advert_id = _clean(_first(row, "advertId", "advert_id"))
            upd_time = _clean(_first(row, "updTime", "date", "time"))
            upd_num = _clean(_first(row, "updNum", "id", default=index))
            expense_sum = abs(_money(_first(row, "updSum", "sum", "expense", default=0)))
            expense_key = f"{advert_id}:{upd_time}:{upd_num}:{expense_sum:.4f}"
            conn.execute(
                """
                INSERT INTO economy_ad_expenses(
                    expense_key, expense_date, expense_time, advert_id, campaign_name,
                    advert_type, payment_type, expense_sum, upd_num, raw_json, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(expense_key) DO UPDATE SET
                    expense_date=excluded.expense_date, expense_time=excluded.expense_time,
                    advert_id=excluded.advert_id, campaign_name=excluded.campaign_name,
                    advert_type=excluded.advert_type, payment_type=excluded.payment_type,
                    expense_sum=excluded.expense_sum, upd_num=excluded.upd_num,
                    raw_json=excluded.raw_json, updated_at=CURRENT_TIMESTAMP
                """,
                (
                    expense_key, _date_only(upd_time), upd_time, advert_id,
                    _clean(_first(row, "campName", "campaignName", "name")),
                    int(_money(_first(row, "advertType", "type", default=0))),
                    _clean(_first(row, "paymentType", "payment_type")),
                    expense_sum, upd_num, json.dumps(row, ensure_ascii=False),
                ),
            )
            written += 1
        conn.commit()
    return written


def delete_ad_stats_for_period(path: str | Path, date_from: str, date_to: str, advert_ids: Iterable[int | str] | None = None) -> int:
    ids = [_clean(value) for value in (advert_ids or []) if _clean(value)]
    with _connect(path) as conn:
        if ids:
            placeholders = ",".join("?" for _ in ids)
            cursor = conn.execute(
                f"DELETE FROM economy_ad_stats WHERE stat_date BETWEEN ? AND ? AND advert_id IN ({placeholders})",
                (_date_only(date_from), _date_only(date_to), *ids),
            )
        else:
            cursor = conn.execute(
                "DELETE FROM economy_ad_stats WHERE stat_date BETWEEN ? AND ?",
                (_date_only(date_from), _date_only(date_to)),
            )
        conn.commit()
        return int(cursor.rowcount or 0)


def delete_ad_expenses_for_period(path: str | Path, date_from: str, date_to: str) -> int:
    with _connect(path) as conn:
        cursor = conn.execute(
            "DELETE FROM economy_ad_expenses WHERE expense_date BETWEEN ? AND ?",
            (_date_only(date_from), _date_only(date_to)),
        )
        conn.commit()
        return int(cursor.rowcount or 0)


def get_ad_coverage(path: str | Path) -> dict[str, Any]:
    with _connect(path) as conn:
        row = conn.execute(
            "SELECT MIN(stat_date) date_from, MAX(stat_date) date_to, COUNT(*) rows_count, MAX(updated_at) updated_at FROM economy_ad_stats"
        ).fetchone()
        return dict(row) if row else {}

def flatten_ad_stats(payload: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    flat: list[dict[str, Any]] = []
    for campaign in payload:
        advert_id = _clean(_first(campaign, "advertId", "advert_id", "id"))
        days = campaign.get("days") or campaign.get("dailyStats") or campaign.get("daily_stats") or []
        if not days:
            days = [campaign]
        for day in days:
            stat_date = _date_only(_first(day, "date", "day", "dateFrom", "date_from"))
            nm_rows: list[dict[str, Any]] = []
            for app_item in day.get("apps", []) or day.get("appTypeStats", []) or []:
                nm_rows.extend(app_item.get("nm", []) or app_item.get("stats", []) or [])
            nm_rows.extend(day.get("nm", []) or day.get("nms", []) or [])
            if nm_rows:
                for nm in nm_rows:
                    flat.append({
                        "stat_date": stat_date,
                        "advert_id": advert_id,
                        "nm_id": _clean(_first(nm, "nmId", "nm", "nm_id")),
                        "views": _money(_first(nm, "views", default=0)),
                        "clicks": _money(_first(nm, "clicks", default=0)),
                        "spend": _money(_first(nm, "sum", "spend", "expenses", default=0)),
                        "atbs": _money(_first(nm, "atbs", default=0)),
                        "orders": _money(_first(nm, "orders", default=0)),
                        "shks": _money(_first(nm, "shks", default=0)),
                        "revenue": _money(_first(nm, "sum_price", "sumPrice", "revenue", default=0)),
                        "ctr": _money(_first(nm, "ctr", default=0)),
                        "cpc": _money(_first(nm, "cpc", default=0)),
                        "cpm": _money(_first(nm, "cpm", default=0)),
                        "cr": _money(_first(nm, "cr", default=0)),
                        "raw_json": json.dumps({"campaign": campaign, "day": day, "nm": nm}, ensure_ascii=False),
                    })
            else:
                flat.append({
                    "stat_date": stat_date,
                    "advert_id": advert_id,
                    "nm_id": "",
                    "views": _money(_first(day, "views", default=0)),
                    "clicks": _money(_first(day, "clicks", default=0)),
                    "spend": _money(_first(day, "sum", "spend", "expenses", default=0)),
                    "atbs": _money(_first(day, "atbs", default=0)),
                    "orders": _money(_first(day, "orders", default=0)),
                    "shks": _money(_first(day, "shks", default=0)),
                    "revenue": _money(_first(day, "sum_price", "sumPrice", "revenue", default=0)),
                    "ctr": _money(_first(day, "ctr", default=0)),
                    "cpc": _money(_first(day, "cpc", default=0)),
                    "cpm": _money(_first(day, "cpm", default=0)),
                    "cr": _money(_first(day, "cr", default=0)),
                    "raw_json": json.dumps({"campaign": campaign, "day": day}, ensure_ascii=False),
                })
    return [row for row in flat if row.get("stat_date") and row.get("advert_id")]


def save_ad_stats(path: str | Path, rows: Iterable[dict[str, Any]]) -> int:
    written = 0
    with _connect(path) as conn:
        for row in rows:
            conn.execute(
                """
                INSERT INTO economy_ad_stats(
                    stat_date, advert_id, nm_id, views, clicks, spend, atbs,
                    orders, shks, revenue, ctr, cpc, cpm, cr, raw_json, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(stat_date, advert_id, nm_id) DO UPDATE SET
                    views=excluded.views,
                    clicks=excluded.clicks,
                    spend=excluded.spend,
                    atbs=excluded.atbs,
                    orders=excluded.orders,
                    shks=excluded.shks,
                    revenue=excluded.revenue,
                    ctr=excluded.ctr, cpc=excluded.cpc, cpm=excluded.cpm, cr=excluded.cr,
                    raw_json=excluded.raw_json,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (
                    row["stat_date"], row["advert_id"], row.get("nm_id", ""),
                    _money(row.get("views")), _money(row.get("clicks")),
                    _money(row.get("spend")), _money(row.get("atbs")),
                    _money(row.get("orders")), _money(row.get("shks")),
                    _money(row.get("revenue")), _money(row.get("ctr")),
                    _money(row.get("cpc")), _money(row.get("cpm")),
                    _money(row.get("cr")), row.get("raw_json", "{}"),
                ),
            )
            written += 1
        conn.commit()
    return written


def capture_catalog_costs(path: str | Path, effective_from: str | None = None) -> int:
    effective_from = effective_from or date.today().isoformat()
    inserted = 0
    with _connect(path) as conn:
        if not conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='products'"
        ).fetchone():
            return 0
        products = conn.execute(
            """
            SELECT seller_article, cost_price_rub, materials_cost_rub, packaging_cost_rub,
                   labor_cost_rub, marking_cost_rub, other_unit_cost_rub
            FROM products WHERE active=1
            """
        ).fetchall()
        latest_by_article: dict[str, sqlite3.Row] = {}
        for row in conn.execute(
            """
            SELECT seller_article, cost_price_rub, materials_cost_rub,
                   packaging_cost_rub, labor_cost_rub, marking_cost_rub,
                   other_unit_cost_rub
            FROM economy_cost_history
            ORDER BY seller_article, effective_from DESC, id DESC
            """
        ).fetchall():
            latest_by_article.setdefault(str(row["seller_article"]), row)
        for product in products:
            latest = latest_by_article.get(str(product["seller_article"]))
            current_tuple = tuple(round(float(product[key] or 0), 6) for key in (
                "cost_price_rub", "materials_cost_rub", "packaging_cost_rub",
                "labor_cost_rub", "marking_cost_rub", "other_unit_cost_rub",
            ))
            latest_tuple = tuple(round(float(latest[key] or 0), 6) for key in (
                "cost_price_rub", "materials_cost_rub", "packaging_cost_rub",
                "labor_cost_rub", "marking_cost_rub", "other_unit_cost_rub",
            )) if latest else None
            if latest_tuple == current_tuple:
                continue
            conn.execute(
                """
                INSERT INTO economy_cost_history(
                    seller_article, effective_from, cost_price_rub,
                    materials_cost_rub, packaging_cost_rub, labor_cost_rub,
                    marking_cost_rub, other_unit_cost_rub, source, notes
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, 'catalog', 'Автоматический снимок каталога')
                """,
                (product["seller_article"], effective_from, *current_tuple),
            )
            inserted += 1
        conn.commit()
    return inserted


def add_cost_history(
    path: str | Path,
    *,
    seller_article: str,
    effective_from: str,
    cost_price_rub: float,
    notes: str = "",
) -> int:
    with _connect(path) as conn:
        product = conn.execute(
            "SELECT materials_cost_rub, packaging_cost_rub, labor_cost_rub, marking_cost_rub, other_unit_cost_rub FROM products WHERE seller_article=?",
            (seller_article,),
        ).fetchone()
        if not product:
            raise ValueError("Товар не найден в каталоге")
        cur = conn.execute(
            """
            INSERT INTO economy_cost_history(
                seller_article, effective_from, cost_price_rub,
                materials_cost_rub, packaging_cost_rub, labor_cost_rub,
                marking_cost_rub, other_unit_cost_rub, source, notes
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, 'manual', ?)
            """,
            (
                seller_article, effective_from, float(cost_price_rub),
                float(product["materials_cost_rub"] or 0),
                float(product["packaging_cost_rub"] or 0),
                float(product["labor_cost_rub"] or 0),
                float(product["marking_cost_rub"] or 0),
                float(product["other_unit_cost_rub"] or 0),
                notes,
            ),
        )
        conn.commit()
        return int(cur.lastrowid)


def _history_map(conn: sqlite3.Connection) -> dict[str, list[tuple[str, float]]]:
    result: dict[str, list[tuple[str, float]]] = defaultdict(list)
    for row in conn.execute(
        "SELECT seller_article, effective_from, cost_price_rub FROM economy_cost_history ORDER BY seller_article, effective_from, id"
    ).fetchall():
        result[str(row["seller_article"])].append((str(row["effective_from"]), float(row["cost_price_rub"] or 0)))
    return result


def _cost_at(history: dict[str, list[tuple[str, float]]], article: str, event_date: str, fallback: float) -> float:
    chosen = None
    for effective_from, cost in history.get(article, []):
        if not event_date or effective_from <= event_date:
            chosen = cost
        elif event_date and effective_from > event_date:
            break
    return float(fallback if chosen is None else chosen)


def _date_range(date_from: str, date_to: str) -> tuple[str, str]:
    return _date_only(date_from), _date_only(date_to)


def _article_key(value: Any) -> str:
    """Normalize an external seller article only for catalog matching.

    The article displayed in Unit economics always comes from products.seller_article.
    External WB values are never used as canonical product identifiers.
    """
    return _clean(value).casefold()


def _nm_key(value: Any) -> str:
    raw = _clean(value)
    if not raw:
        return ""
    # SQLite/JSON can surface nmID as 12345, "12345" or occasionally 12345.0.
    if raw.endswith(".0") and raw[:-2].isdigit():
        raw = raw[:-2]
    return raw


# ---------------------------------------------------------------------------
# FBE 0.77.0: local Economy cache with daily financial reports
# ---------------------------------------------------------------------------
# Operational orders and sales are kept as event rows. Official daily WB
# report summaries are the financial basis for arbitrary calendar periods.
# Weekly summaries are stored separately and used only for reconciliation.

_REPORT_SUMMARY_COLUMNS = {
    "period_kind": "TEXT NOT NULL DEFAULT 'weekly'",
    "retail_amount_sum": "REAL NOT NULL DEFAULT 0",
    "for_pay_sum": "REAL NOT NULL DEFAULT 0",
    "delivery_service_sum": "REAL NOT NULL DEFAULT 0",
    "paid_storage_sum": "REAL NOT NULL DEFAULT 0",
    "paid_acceptance_sum": "REAL NOT NULL DEFAULT 0",
    "deduction_sum": "REAL NOT NULL DEFAULT 0",
    "penalty_sum": "REAL NOT NULL DEFAULT 0",
    "additional_payment_sum": "REAL NOT NULL DEFAULT 0",
    "bank_payment_sum": "REAL NOT NULL DEFAULT 0",
}

def ensure_economy_schema(path: str | Path) -> Path:
    db_path = _ensure_economy_schema_base_impl(path)
    with _connect(db_path) as conn:
        _ensure_column(conn, "economy_sales", "for_pay_amount", "REAL NOT NULL DEFAULT 0")
        _ensure_column(conn, "economy_orders", "seller_amount", "REAL NOT NULL DEFAULT 0")
        _ensure_column(conn, "economy_sales", "seller_amount", "REAL NOT NULL DEFAULT 0")
        for column in ("ctr", "cpc", "cpm", "cr"):
            _ensure_column(conn, "economy_ad_stats", column, "REAL NOT NULL DEFAULT 0")
        # priceWithDisc is the seller price after the seller's discount and
        # before WB's customer discount (SPP).  Earlier versions stored only
        # finishedPrice, so restore this amount from the cached API payloads.
        for row in conn.execute(
            "SELECT event_key, raw_json, gross_amount, discounted_amount "
            "FROM economy_orders WHERE seller_amount=0"
        ).fetchall():
            try:
                payload = json.loads(str(row["raw_json"] or "{}"))
            except Exception:
                payload = {}
            seller_amount = _money(_first(payload, "priceWithDisc", default=0)) if isinstance(payload, dict) else 0.0
            if not seller_amount:
                total = float(row["gross_amount"] or 0)
                discount_pct = _money(_first(payload, "discountPercent", default=0)) if isinstance(payload, dict) else 0.0
                seller_amount = total * (1 - discount_pct / 100) if total else float(row["discounted_amount"] or 0)
            conn.execute(
                "UPDATE economy_orders SET seller_amount=? WHERE event_key=?",
                (seller_amount, str(row["event_key"])),
            )
        for row in conn.execute(
            "SELECT sale_key, raw_json, amount FROM economy_sales WHERE seller_amount=0"
        ).fetchall():
            try:
                payload = json.loads(str(row["raw_json"] or "{}"))
            except Exception:
                payload = {}
            seller_amount = _money(_first(payload, "priceWithDisc", default=0)) if isinstance(payload, dict) else 0.0
            if not seller_amount:
                seller_amount = float(row["amount"] or 0)
            conn.execute(
                "UPDATE economy_sales SET seller_amount=? WHERE sale_key=?",
                (abs(seller_amount), str(row["sale_key"])),
            )
        # Backfill the exact operational amount after sales commission from
        # payloads already cached by earlier FBE versions.
        for row in conn.execute(
            "SELECT sale_key, raw_json FROM economy_sales WHERE for_pay_amount=0"
        ).fetchall():
            try:
                payload = json.loads(str(row["raw_json"] or "{}"))
            except Exception:
                continue
            if isinstance(payload, dict):
                for_pay = abs(_money(_first(payload, "forPay", default=0)))
                if for_pay:
                    conn.execute(
                        "UPDATE economy_sales SET for_pay_amount=? WHERE sale_key=?",
                        (for_pay, str(row["sale_key"])),
                    )
        for column, definition in _REPORT_SUMMARY_COLUMNS.items():
            _ensure_column(conn, "economy_finance_reports", column, definition)
        # All summaries written by 0.69.7 were explicitly weekly.
        conn.execute(
            "UPDATE economy_finance_reports SET period_kind='weekly' "
            "WHERE COALESCE(period_kind, '')=''"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_economy_finance_reports_period "
            "ON economy_finance_reports(period_kind, date_from, date_to)"
        )
        conn.execute(
            "INSERT INTO schema_meta(key, value) VALUES('economy_schema_version', '11') "
            "ON CONFLICT(key) DO UPDATE SET value='11'"
        )
        conn.commit()
    # Seed products that predate Economy once. Runtime catalog edits append
    # their snapshots atomically in catalog.repository.
    capture_catalog_costs(db_path)
    return db_path


def save_finance_reports(
    path: str | Path,
    rows: Iterable[dict[str, Any]],
    *,
    period_kind: str = "daily",
) -> int:
    """Store official report-level totals with an explicit grouping kind."""
    period_kind = str(period_kind or "daily").strip().lower()
    if period_kind not in {"daily", "weekly"}:
        raise ValueError("period_kind must be 'daily' or 'weekly'")
    written = 0
    with _connect(path) as conn:
        for row in rows:
            report_id = _clean(_first(row, "reportId", "realizationreport_id", "id"))
            if not report_id:
                continue
            conn.execute(
                """
                INSERT INTO economy_finance_reports(
                    report_id, period_kind, date_from, date_to, create_date, currency,
                    report_type, retail_amount_sum, for_pay_sum,
                    delivery_service_sum, paid_storage_sum, paid_acceptance_sum,
                    deduction_sum, penalty_sum, additional_payment_sum,
                    bank_payment_sum, raw_json, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(report_id) DO UPDATE SET
                    period_kind=excluded.period_kind,
                    date_from=excluded.date_from,
                    date_to=excluded.date_to,
                    create_date=excluded.create_date,
                    currency=excluded.currency,
                    report_type=excluded.report_type,
                    retail_amount_sum=excluded.retail_amount_sum,
                    for_pay_sum=excluded.for_pay_sum,
                    delivery_service_sum=excluded.delivery_service_sum,
                    paid_storage_sum=excluded.paid_storage_sum,
                    paid_acceptance_sum=excluded.paid_acceptance_sum,
                    deduction_sum=excluded.deduction_sum,
                    penalty_sum=excluded.penalty_sum,
                    additional_payment_sum=excluded.additional_payment_sum,
                    bank_payment_sum=excluded.bank_payment_sum,
                    raw_json=excluded.raw_json,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (
                    report_id,
                    period_kind,
                    _date_only(_first(row, "dateFrom", "date_from")),
                    _date_only(_first(row, "dateTo", "date_to")),
                    _date_only(_first(row, "createDate", "create_date")),
                    _clean(_first(row, "currency", default="RUB")) or "RUB",
                    _clean(_first(row, "reportType", "report_type")),
                    _money(_first(row, "retailAmountSum", "retail_amount_sum", default=0)),
                    _money(_first(row, "forPaySum", "for_pay_sum", default=0)),
                    _money(_first(row, "deliveryServiceSum", "delivery_service_sum", default=0)),
                    _money(_first(row, "paidStorageSum", "paid_storage_sum", default=0)),
                    _money(_first(row, "paidAcceptanceSum", "paid_acceptance_sum", default=0)),
                    _money(_first(row, "deductionSum", "deduction_sum", default=0)),
                    _money(_first(row, "penaltySum", "penalty_sum", default=0)),
                    _money(_first(row, "additionalPaymentSum", "additional_payment_sum", default=0)),
                    _money(_first(row, "bankPaymentSum", "bank_payment_sum", default=0)),
                    json.dumps(row, ensure_ascii=False),
                ),
            )
            written += 1
        conn.commit()
    return written


def delete_finance_reports_for_period(
    path: str | Path,
    date_from: str,
    date_to: str,
    *,
    period_kind: str = "daily",
) -> int:
    """Delete only one report grouping kind in the requested source interval."""
    period_kind = str(period_kind or "daily").strip().lower()
    with _connect(path) as conn:
        cursor = conn.execute(
            """
            DELETE FROM economy_finance_reports
            WHERE period_kind=?
              AND COALESCE(NULLIF(date_to,''), date_from) >= ?
              AND COALESCE(NULLIF(date_from,''), date_to) <= ?
            """,
            (period_kind, _date_only(date_from), _date_only(date_to)),
        )
        conn.commit()
        return int(cursor.rowcount or 0)


def get_report_coverage(path: str | Path, period_kind: str = "daily") -> dict[str, Any]:
    with _connect(path) as conn:
        row = conn.execute(
            """
            SELECT MIN(NULLIF(date_from,'')) AS date_from,
                   MAX(COALESCE(NULLIF(date_to,''), date_from)) AS date_to,
                   COUNT(*) AS rows_count,
                   MAX(updated_at) AS updated_at
            FROM economy_finance_reports WHERE period_kind=?
            """,
            (period_kind,),
        ).fetchone()
        return dict(row) if row else {"date_from": "", "date_to": "", "rows_count": 0, "updated_at": ""}


def _cost_product_maps(conn: sqlite3.Connection) -> tuple[dict[str, sqlite3.Row], dict[str, sqlite3.Row]]:
    by_article: dict[str, sqlite3.Row] = {}
    by_nm: dict[str, sqlite3.Row] = {}
    for row in conn.execute(
        """
        SELECT seller_article, wb_article, product, volume, cost_price_rub
        FROM products WHERE active=1
        """
    ).fetchall():
        by_article[_article_key(row["seller_article"])] = row
        nm = _nm_key(row["wb_article"])
        if nm:
            by_nm[nm] = row
    return by_article, by_nm


def _cost_product(
    by_article: dict[str, sqlite3.Row],
    by_nm: dict[str, sqlite3.Row],
    article: Any,
    nm_id: Any,
):
    return by_article.get(_article_key(article)) or by_nm.get(_nm_key(nm_id))


def _coverage_row(conn: sqlite3.Connection, table: str, date_column: str) -> dict[str, Any]:
    row = conn.execute(
        f"SELECT MIN(NULLIF({date_column},'')) AS date_from, "
        f"MAX(NULLIF({date_column},'')) AS date_to, COUNT(*) AS rows_count, "
        f"MAX(updated_at) AS updated_at FROM {table}"
    ).fetchone()
    return dict(row) if row else {"date_from": "", "date_to": "", "rows_count": 0, "updated_at": ""}


def _bounded_ratio(numerator: float, denominator: float, *, upper: float = 1.0) -> float:
    if denominator <= 0:
        return 0.0
    return max(0.0, min(float(upper), float(numerator) / float(denominator)))


def _today_snapshot(conn: sqlite3.Connection, today_value: str) -> dict[str, Any]:
    """Build the always-visible operational snapshot for the current day.

    The purple column contains today's still-active orders at priceWithDisc:
    the seller price after the seller discount, before WB's customer discount
    and marketplace deductions.  The red column contains orders whose
    cancelDate is today.  The green column is explicitly a forecast: active
    order flow is multiplied by mature order-to-buyout conversion measured by
    SRID and by the historical Finance API bank-payment share.
    """
    today_obj = datetime.strptime(today_value, "%Y-%m-%d").date()

    order_rows = conn.execute(
        "SELECT * FROM economy_orders WHERE order_date=?",
        (today_value,),
    ).fetchall()
    created_orders_qty = created_orders_amount = 0.0
    active_orders_qty = active_orders_amount = 0.0
    for row in order_rows:
        qty_value = abs(float(row["quantity"] or 1)) or 1.0
        amount = abs(float(row["seller_amount"] or row["discounted_amount"] or 0)) * qty_value
        created_orders_qty += qty_value
        created_orders_amount += amount
        if not int(row["is_cancel"] or 0):
            active_orders_qty += qty_value
            active_orders_amount += amount

    # Refusals/cancellations are attributed to the day when WB changed the
    # order to cancelled, not to the original order day.
    cancel_rows = conn.execute(
        "SELECT * FROM economy_orders WHERE is_cancel=1 AND cancel_date=?",
        (today_value,),
    ).fetchall()
    cancel_qty = cancel_amount = 0.0
    for row in cancel_rows:
        qty_value = abs(float(row["quantity"] or 1)) or 1.0
        cancel_qty += qty_value
        cancel_amount += abs(float(row["seller_amount"] or row["discounted_amount"] or 0)) * qty_value

    sale_rows = conn.execute(
        "SELECT * FROM economy_sales WHERE sale_date=?",
        (today_value,),
    ).fetchall()
    buyout_qty = buyout_amount = buyout_seller_amount = buyout_for_pay = 0.0
    return_qty = return_amount = return_seller_amount = return_for_pay = 0.0
    for row in sale_rows:
        qty_value = abs(float(row["quantity"] or 1)) or 1.0
        amount = abs(float(row["amount"] or 0)) * qty_value
        seller_amount = abs(float(row["seller_amount"] or row["amount"] or 0)) * qty_value
        for_pay = abs(float(row["for_pay_amount"] or 0)) * qty_value
        if int(row["is_return"] or 0):
            return_qty += qty_value
            return_amount += amount
            return_seller_amount += seller_amount
            return_for_pay += for_pay
        else:
            buyout_qty += qty_value
            buyout_amount += amount
            buyout_seller_amount += seller_amount
            buyout_for_pay += for_pay
    net_buyout_qty = buyout_qty - return_qty
    net_buyout_amount = buyout_amount - return_amount
    net_buyout_seller_amount = buyout_seller_amount - return_seller_amount
    net_buyout_for_pay = buyout_for_pay - return_for_pay

    # Mature cohorts avoid treating orders still travelling to the customer as
    # failed buyouts.  Use up to 60 days of history, ending 14 days ago.
    cohort_from = (today_obj - timedelta(days=60)).isoformat()
    cohort_to = (today_obj - timedelta(days=14)).isoformat()
    cohort_rows = conn.execute(
        """
        SELECT o.order_date, o.quantity, o.seller_amount, o.discounted_amount,
               EXISTS(
                   SELECT 1 FROM economy_sales s
                   WHERE s.srid=o.srid AND s.is_return=0
               ) AS has_buyout,
               EXISTS(
                   SELECT 1 FROM economy_sales s
                   WHERE s.srid=o.srid AND s.is_return=1
               ) AS has_return
        FROM economy_orders o
        WHERE o.order_date BETWEEN ? AND ? AND o.srid<>''
        """,
        (cohort_from, cohort_to),
    ).fetchall()
    cohort_qty = cohort_amount = converted_qty = converted_amount = 0.0
    cohort_dates: list[str] = []
    for row in cohort_rows:
        if row["order_date"]:
            cohort_dates.append(str(row["order_date"]))
        qty_value = abs(float(row["quantity"] or 1)) or 1.0
        amount = abs(float(row["seller_amount"] or row["discounted_amount"] or 0)) * qty_value
        cohort_qty += qty_value
        cohort_amount += amount
        if int(row["has_buyout"] or 0) and not int(row["has_return"] or 0):
            converted_qty += qty_value
            converted_amount += amount

    if cohort_dates:
        cohort_from, cohort_to = min(cohort_dates), max(cohort_dates)

    conversion_source = "srid"
    conversion_qty = _bounded_ratio(converted_qty, cohort_qty)
    conversion_amount = _bounded_ratio(converted_amount, cohort_amount)

    # A new installation may not yet have a mature 14-day cohort.  Fall back
    # to a broad local-flow ratio, while keeping the forecast explicitly marked.
    if cohort_qty < 30 or cohort_amount <= 0:
        fallback_from = (today_obj - timedelta(days=30)).isoformat()
        fallback_to = (today_obj - timedelta(days=1)).isoformat()
        order_fallback = conn.execute(
            """
            SELECT COALESCE(SUM(ABS(quantity)),0) AS qty,
                   COALESCE(SUM(ABS(COALESCE(NULLIF(seller_amount,0), discounted_amount) * quantity)),0) AS amount
            FROM economy_orders WHERE order_date BETWEEN ? AND ?
            """,
            (fallback_from, fallback_to),
        ).fetchone()
        sales_fallback = conn.execute(
            """
            SELECT COALESCE(SUM(CASE WHEN is_return=0 THEN ABS(quantity) ELSE -ABS(quantity) END),0) AS qty,
                   COALESCE(SUM(CASE WHEN is_return=0
                        THEN ABS(COALESCE(NULLIF(seller_amount,0), amount) * quantity)
                        ELSE -ABS(COALESCE(NULLIF(seller_amount,0), amount) * quantity) END),0) AS amount
            FROM economy_sales WHERE sale_date BETWEEN ? AND ?
            """,
            (fallback_from, fallback_to),
        ).fetchone()
        conversion_qty = _bounded_ratio(float(sales_fallback["qty"] or 0), float(order_fallback["qty"] or 0))
        conversion_amount = _bounded_ratio(float(sales_fallback["amount"] or 0), float(order_fallback["amount"] or 0))
        cohort_from, cohort_to = fallback_from, fallback_to
        cohort_qty = float(order_fallback["qty"] or 0)
        cohort_amount = float(order_fallback["amount"] or 0)
        conversion_source = "flow"

    finance_from = (today_obj - timedelta(days=30)).isoformat()
    finance_to = (today_obj - timedelta(days=1)).isoformat()
    finance_rows = conn.execute(
        """
        SELECT retail_amount_sum, bank_payment_sum
        FROM economy_finance_reports
        WHERE period_kind='daily'
          AND COALESCE(NULLIF(date_from,''), date_to) >= ?
          AND COALESCE(NULLIF(date_to,''), date_from) <= ?
        """,
        (finance_from, finance_to),
    ).fetchall()
    payout_source = "daily"
    if not finance_rows:
        finance_rows = conn.execute(
            """
            SELECT retail_amount_sum, bank_payment_sum
            FROM economy_finance_reports
            WHERE period_kind='weekly'
              AND COALESCE(NULLIF(date_to,''), date_from) < ?
              AND COALESCE(NULLIF(date_to,''), date_from) >= ?
            """,
            (today_value, finance_from),
        ).fetchall()
        payout_source = "weekly"
    finance_retail = sum(float(row["retail_amount_sum"] or 0) for row in finance_rows)
    finance_bank = sum(float(row["bank_payment_sum"] or 0) for row in finance_rows)
    payout_ratio = _bounded_ratio(finance_bank, finance_retail, upper=1.2)

    # Known cancellations are already removed from the forecast base.  The
    # historical all-order conversion then keeps the estimate conservative for
    # active orders that may still be cancelled or not collected later.
    forecast_qty = active_orders_qty * conversion_qty
    forecast_realized_gross = active_orders_amount * conversion_amount
    forecast_net_amount = forecast_realized_gross * payout_ratio

    today_finance_rows = conn.execute(
        """
        SELECT retail_amount_sum, bank_payment_sum
        FROM economy_finance_reports
        WHERE period_kind='daily'
          AND COALESCE(NULLIF(date_from,''), date_to)=?
          AND COALESCE(NULLIF(date_to,''), date_from)=?
        """,
        (today_value, today_value),
    ).fetchall()
    if today_finance_rows:
        buyout_net_after_wb = sum(float(row["bank_payment_sum"] or 0) for row in today_finance_rows)
        buyout_net_mode = "fact"
    else:
        buyout_net_after_wb = net_buyout_seller_amount * payout_ratio
        # A forecast after all marketplace deductions should normally not be
        # higher than the operational amount remaining after sales commission.
        if net_buyout_for_pay > 0:
            buyout_net_after_wb = min(buyout_net_after_wb, net_buyout_for_pay)
        buyout_net_mode = "estimate"

    chart_max = max(active_orders_amount, forecast_net_amount, cancel_amount, 1.0)

    def chart_height(value: float) -> float:
        if value <= 0:
            return 3.0
        return max(12.0, min(100.0, value / chart_max * 100.0))

    forecast_ready = bool(cohort_qty >= 1 and payout_ratio > 0)
    return {
        "date": today_value,
        "date_label": today_obj.strftime("%d.%m.%Y"),
        "created_orders_qty": created_orders_qty,
        "created_orders_amount": created_orders_amount,
        "orders_qty": active_orders_qty,
        "orders_amount": active_orders_amount,
        "active_orders_qty": active_orders_qty,
        "active_orders_amount": active_orders_amount,
        "cancel_qty": cancel_qty,
        "cancel_amount": cancel_amount,
        "buyout_qty": buyout_qty,
        "buyout_amount": buyout_amount,
        "buyout_seller_amount": buyout_seller_amount,
        "return_qty": return_qty,
        "return_amount": return_amount,
        "return_seller_amount": return_seller_amount,
        "net_buyout_qty": net_buyout_qty,
        "net_buyout_amount": net_buyout_amount,
        "net_buyout_seller_amount": net_buyout_seller_amount,
        "net_buyout_for_pay": net_buyout_for_pay,
        "buyout_net_after_wb": buyout_net_after_wb,
        "buyout_net_mode": buyout_net_mode,
        "forecast_qty": forecast_qty,
        "forecast_realized_gross": forecast_realized_gross,
        "forecast_net_amount": forecast_net_amount,
        "forecast_ready": forecast_ready,
        "conversion_qty": conversion_qty,
        "conversion_amount": conversion_amount,
        "conversion_source": conversion_source,
        "cohort_from": cohort_from,
        "cohort_to": cohort_to,
        "cohort_qty": cohort_qty,
        "payout_ratio": payout_ratio,
        "payout_source": payout_source,
        "finance_retail": finance_retail,
        "finance_bank": finance_bank,
        "chart": {
            "orders_height": chart_height(active_orders_amount),
            "forecast_height": chart_height(forecast_net_amount),
            "cancel_height": chart_height(cancel_amount),
        },
    }


def _safe_ratio(numerator: float, denominator: float, multiplier: float = 1.0) -> float:
    return numerator / denominator * multiplier if denominator else 0.0


def _advertising_snapshot(conn: sqlite3.Connection, date_from: str, date_to: str, total_sales: float, sales_base_source: str) -> dict[str, Any]:
    stats = conn.execute(
        """SELECT advert_id, SUM(views) views, SUM(clicks) clicks, SUM(spend) spend,
                  SUM(atbs) atbs, SUM(orders) orders, SUM(shks) shks, SUM(revenue) revenue
           FROM economy_ad_stats WHERE stat_date BETWEEN ? AND ? GROUP BY advert_id""",
        (date_from, date_to),
    ).fetchall()
    expenses = conn.execute(
        """SELECT advert_id, MAX(campaign_name) campaign_name, SUM(expense_sum) actual_spend,
                  GROUP_CONCAT(DISTINCT payment_type) payment_types
           FROM economy_ad_expenses WHERE expense_date BETWEEN ? AND ? GROUP BY advert_id""",
        (date_from, date_to),
    ).fetchall()
    campaigns = {str(row["advert_id"]): dict(row) for row in conn.execute("SELECT * FROM economy_ad_campaigns").fetchall()}
    stats_map = {str(row["advert_id"]): dict(row) for row in stats}
    expense_map = {str(row["advert_id"]): dict(row) for row in expenses}
    ids = sorted(set(campaigns) | set(stats_map) | set(expense_map), key=lambda value: int(value) if value.isdigit() else value)
    rows: list[dict[str, Any]] = []
    for advert_id in ids:
        campaign = campaigns.get(advert_id, {})
        stat = stats_map.get(advert_id, {})
        expense = expense_map.get(advert_id, {})
        spend = float(stat.get("spend") or 0)
        actual_spend = float(expense.get("actual_spend") or 0)
        revenue = float(stat.get("revenue") or 0)
        orders = float(stat.get("orders") or 0)
        shks = float(stat.get("shks") or 0)
        views = float(stat.get("views") or 0)
        clicks = float(stat.get("clicks") or 0)
        status = int(campaign.get("status") or 0)
        campaign_type = int(campaign.get("campaign_type") or campaign.get("advert_type") or 0)
        name = str(campaign.get("name") or expense.get("campaign_name") or f"Кампания {advert_id}")
        payment_type = str(campaign.get("payment_type") or expense.get("payment_types") or "")
        effective_spend = actual_spend if actual_spend > 0 else spend
        rows.append({
            "advert_id": advert_id, "name": name, "status": status,
            "status_label": AD_STATUS_LABELS.get(status, f"Статус {status}" if status else "Не определен"),
            "status_class": "active" if status == 9 else ("paused" if status == 11 else "closed"),
            "campaign_type": campaign_type, "type_label": AD_TYPE_LABELS.get(campaign_type, f"Тип {campaign_type}" if campaign_type else "—"),
            "payment_type": payment_type, "views": views, "clicks": clicks,
            "spend": spend, "actual_spend": actual_spend, "effective_spend": effective_spend,
            "atbs": float(stat.get("atbs") or 0), "orders": orders, "shks": shks, "revenue": revenue,
            "drr_pct": _safe_ratio(effective_spend, revenue, 100),
            "ctr_pct": _safe_ratio(clicks, views, 100),
            "cpc": _safe_ratio(spend, clicks),
            "cpo": _safe_ratio(effective_spend, orders),
            "cost_per_ordered_item": _safe_ratio(effective_spend, shks),
            "cost_per_buyout": _safe_ratio(effective_spend, shks),
            "difference": actual_spend - spend if actual_spend else 0.0,
        })
    rows.sort(key=lambda row: (row["effective_spend"], row["revenue"], row["status"] == 9), reverse=True)
    total_stats_spend = sum(float(row["spend"]) for row in rows)
    total_actual_spend = sum(float(row["actual_spend"]) for row in rows)
    effective_total = total_actual_spend if total_actual_spend > 0 else total_stats_spend
    revenue = sum(float(row["revenue"]) for row in rows)
    views = sum(float(row["views"]) for row in rows)
    clicks = sum(float(row["clicks"]) for row in rows)
    orders = sum(float(row["orders"]) for row in rows)
    shks = sum(float(row["shks"]) for row in rows)
    coverage = conn.execute(
        "SELECT MIN(stat_date) date_from, MAX(stat_date) date_to, COUNT(*) rows_count, MAX(updated_at) updated_at FROM economy_ad_stats"
    ).fetchone()
    active_count = sum(1 for row in rows if row["status"] == 9)
    return {
        "rows": rows, "top_rows": rows[:5], "other_rows": rows[5:], "campaigns_count": len(rows), "active_count": active_count,
        "stats_spend": total_stats_spend, "actual_spend": total_actual_spend,
        "effective_spend": effective_total, "spend_source": "Списания WB" if total_actual_spend > 0 else "Статистика кампаний",
        "revenue": revenue, "views": views, "clicks": clicks, "orders": orders, "shks": shks,
        "drr_pct": _safe_ratio(effective_total, revenue, 100),
        "total_sales_drr_pct": _safe_ratio(effective_total, total_sales, 100),
        "sales_base": total_sales, "sales_base_source": sales_base_source,
        "ctr_pct": _safe_ratio(clicks, views, 100), "cpc": _safe_ratio(total_stats_spend, clicks),
        "cpo": _safe_ratio(effective_total, orders), "cost_per_ordered_item": _safe_ratio(effective_total, shks),
        "cost_per_buyout": _safe_ratio(effective_total, shks),
        "reconciliation_delta": total_actual_spend - total_stats_spend if total_actual_spend else 0.0,
        "coverage": dict(coverage) if coverage else {},
        "ready": bool(rows),
    }


def build_dashboard(path: str | Path, date_from: str, date_to: str) -> dict[str, Any]:
    """Build the 0.77.0 dashboard exclusively from the local SQLite cache."""
    date_from, date_to = _date_range(date_from, date_to)
    with _connect(path) as conn:
        by_article, by_nm = _cost_product_maps(conn)
        history = _history_map(conn)
        today = _today_snapshot(conn, date.today().isoformat())

        orders = conn.execute(
            "SELECT * FROM economy_orders WHERE order_date BETWEEN ? AND ?",
            (date_from, date_to),
        ).fetchall()
        order_qty = order_amount = cancel_qty = cancel_amount = 0.0
        for row in orders:
            qty_value = abs(float(row["quantity"] or 1)) or 1.0
            amount = abs(float(row["discounted_amount"] or 0)) * qty_value
            order_qty += qty_value
            order_amount += amount
            if int(row["is_cancel"] or 0):
                cancel_qty += qty_value
                cancel_amount += amount
        active_order_qty = order_qty - cancel_qty
        active_order_amount = order_amount - cancel_amount

        sales = conn.execute(
            "SELECT * FROM economy_sales WHERE sale_date BETWEEN ? AND ?",
            (date_from, date_to),
        ).fetchall()
        buyout_qty = buyout_amount = return_qty = return_amount = 0.0
        cogs = 0.0
        cost_transaction_units = 0.0
        cost_covered_units = 0.0
        catalog_matched_units = 0.0
        missing_cost_articles: set[str] = set()
        unmatched_articles: set[str] = set()
        sku_map: dict[str, dict[str, Any]] = {}

        for row in sales:
            qty_value = abs(float(row["quantity"] or 1)) or 1.0
            amount = abs(float(row["amount"] or 0)) * qty_value
            is_return = bool(int(row["is_return"] or 0))
            sign = -1.0 if is_return else 1.0
            if is_return:
                return_qty += qty_value
                return_amount += amount
            else:
                buyout_qty += qty_value
                buyout_amount += amount

            product = _cost_product(by_article, by_nm, row["supplier_article"], row["nm_id"])
            cost_transaction_units += qty_value
            if product:
                catalog_matched_units += qty_value
                article = str(product["seller_article"])
                fallback = float(product["cost_price_rub"] or 0)
                unit_cost = _cost_at(history, article, str(row["sale_date"] or date_to), fallback)
                if unit_cost > 0:
                    cogs += sign * qty_value * unit_cost
                    cost_covered_units += qty_value
                else:
                    missing_cost_articles.add(article)
                item = sku_map.setdefault(article, {
                    "seller_article": article,
                    "nm_id": _clean(product["wb_article"]),
                    "title": _clean(product["product"]) or article,
                    "volume": _clean(product["volume"]),
                    "buyout_qty": 0.0,
                    "return_qty": 0.0,
                    "net_qty": 0.0,
                    "buyout_amount": 0.0,
                    "return_amount": 0.0,
                    "net_amount": 0.0,
                    "cost_price_rub": unit_cost,
                    "cogs": 0.0,
                    "cost_ready": unit_cost > 0,
                })
                if is_return:
                    item["return_qty"] += qty_value
                    item["return_amount"] += amount
                else:
                    item["buyout_qty"] += qty_value
                    item["buyout_amount"] += amount
                item["net_qty"] += sign * qty_value
                item["net_amount"] += sign * amount
                if unit_cost > 0:
                    item["cogs"] += sign * qty_value * unit_cost
            else:
                external = _clean(row["supplier_article"]) or ("WB " + _clean(row["nm_id"]))
                if external:
                    unmatched_articles.add(external)

        net_buyout_qty = buyout_qty - return_qty
        net_buyout_amount = buyout_amount - return_amount
        cost_coverage_pct = cost_covered_units / cost_transaction_units * 100 if cost_transaction_units else 100.0
        catalog_match_pct = catalog_matched_units / cost_transaction_units * 100 if cost_transaction_units else 100.0

        # Daily summaries are the only financial input used in selected-period totals.
        daily_reports = conn.execute(
            """
            SELECT * FROM economy_finance_reports
            WHERE period_kind='daily'
              AND COALESCE(NULLIF(date_from,''), date_to) >= ?
              AND COALESCE(NULLIF(date_to,''), date_from) <= ?
            ORDER BY date_from, report_type, report_id
            """,
            (date_from, date_to),
        ).fetchall()
        # Defensive containment: never add a summary that reaches outside the filter.
        daily_reports = [
            row for row in daily_reports
            if (str(row["date_from"] or row["date_to"]) >= date_from)
            and (str(row["date_to"] or row["date_from"]) <= date_to)
        ]
        report_rows = [dict(row) for row in daily_reports]

        weekly_reports = conn.execute(
            """
            SELECT * FROM economy_finance_reports
            WHERE period_kind='weekly'
              AND COALESCE(NULLIF(date_from,''), date_to) >= ?
              AND COALESCE(NULLIF(date_to,''), date_from) <= ?
            ORDER BY date_from, report_type, report_id
            """,
            (date_from, date_to),
        ).fetchall()
        weekly_report_rows = [dict(row) for row in weekly_reports]

        report_sales = sum(float(row["retail_amount_sum"] or 0) for row in daily_reports)
        report_for_pay = sum(float(row["for_pay_sum"] or 0) for row in daily_reports)
        report_delivery = sum(float(row["delivery_service_sum"] or 0) for row in daily_reports)
        report_storage = sum(float(row["paid_storage_sum"] or 0) for row in daily_reports)
        report_acceptance = sum(float(row["paid_acceptance_sum"] or 0) for row in daily_reports)
        report_deduction = sum(float(row["deduction_sum"] or 0) for row in daily_reports)
        report_penalty = sum(float(row["penalty_sum"] or 0) for row in daily_reports)
        report_additional = sum(float(row["additional_payment_sum"] or 0) for row in daily_reports)
        bank_payment = sum(float(row["bank_payment_sum"] or 0) for row in daily_reports)
        wb_difference = report_sales - bank_payment
        wb_difference_pct = wb_difference / report_sales * 100 if report_sales else 0.0
        bank_share_pct = bank_payment / report_sales * 100 if report_sales else 0.0
        remaining_after_wb_and_cogs = bank_payment - cogs

        daily_coverage = conn.execute(
            """
            SELECT MIN(NULLIF(date_from,'')) AS date_from,
                   MAX(COALESCE(NULLIF(date_to,''), date_from)) AS date_to,
                   COUNT(*) AS rows_count, MAX(updated_at) AS updated_at
            FROM economy_finance_reports WHERE period_kind='daily'
            """
        ).fetchone()
        weekly_coverage = conn.execute(
            """
            SELECT MIN(NULLIF(date_from,'')) AS date_from,
                   MAX(COALESCE(NULLIF(date_to,''), date_from)) AS date_to,
                   COUNT(*) AS rows_count, MAX(updated_at) AS updated_at
            FROM economy_finance_reports WHERE period_kind='weekly'
            """
        ).fetchone()
        daily_coverage_dict = dict(daily_coverage) if daily_coverage else {}
        weekly_coverage_dict = dict(weekly_coverage) if weekly_coverage else {}
        finance_complete = bool(daily_reports)
        coverage_start = str(daily_coverage_dict.get("date_from") or "")
        coverage_end = str(daily_coverage_dict.get("date_to") or "")
        selected_inside_finance_coverage = bool(
            coverage_start and coverage_end and date_from >= coverage_start and date_to <= coverage_end
        )
        cost_complete = cost_coverage_pct >= 99.5 and catalog_match_pct >= 99.5
        can_show_remaining = finance_complete and cost_complete

        sku_rows = list(sku_map.values())
        sku_rows.sort(key=lambda x: (x["net_amount"], x["net_qty"]), reverse=True)

        sync_runs = [dict(row) for row in conn.execute(
            "SELECT * FROM economy_sync_runs ORDER BY id DESC LIMIT 20"
        ).fetchall()]
        last_success = conn.execute(
            """
            SELECT finished_at FROM economy_sync_runs
            WHERE status='ok' ORDER BY id DESC LIMIT 1
            """
        ).fetchone()
        cost_history = [dict(row) for row in conn.execute(
            """
            SELECT h.*, p.product, p.volume
            FROM economy_cost_history h
            LEFT JOIN products p ON p.seller_article=h.seller_article
            ORDER BY h.id DESC LIMIT 30
            """
        ).fetchall()]
        product_options = [dict(row) for row in conn.execute(
            "SELECT seller_article, product, volume FROM products WHERE active=1 ORDER BY product, volume"
        ).fetchall()]
        products_total = int(conn.execute("SELECT COUNT(*) FROM products WHERE active=1").fetchone()[0])
        products_with_cost = int(conn.execute(
            "SELECT COUNT(*) FROM products WHERE active=1 AND cost_price_rub>0"
        ).fetchone()[0])

        advertising = _advertising_snapshot(conn, date_from, date_to, report_sales or net_buyout_amount, "дневные финансовые отчеты" if report_sales else "оперативные чистые выкупы")
        coverage = {
            "orders": _coverage_row(conn, "economy_orders", "order_date"),
            "sales": _coverage_row(conn, "economy_sales", "sale_date"),
            "finance_daily": daily_coverage_dict,
            "finance_weekly": weekly_coverage_dict,
            "advertising": advertising.get("coverage", {}),
        }
        coverage_starts = [str(item.get("date_from") or "") for item in coverage.values() if item.get("date_from")]
        coverage_ends = [str(item.get("date_to") or "") for item in coverage.values() if item.get("date_to")]
        local_date_from = min(coverage_starts) if coverage_starts else ""
        local_date_to = max(coverage_ends) if coverage_ends else ""

        totals = {
            "orders_qty": order_qty,
            "orders_amount": order_amount,
            "cancel_qty": cancel_qty,
            "cancel_amount": cancel_amount,
            "active_orders_qty": active_order_qty,
            "active_orders_amount": active_order_amount,
            "buyout_qty": buyout_qty,
            "buyout_amount": buyout_amount,
            "return_qty": return_qty,
            "return_amount": return_amount,
            "net_buyout_qty": net_buyout_qty,
            "net_buyout_amount": net_buyout_amount,
            "report_sales": report_sales,
            "report_for_pay": report_for_pay,
            "bank_payment": bank_payment,
            "wb_difference": wb_difference,
            "wb_difference_pct": wb_difference_pct,
            "bank_share_pct": bank_share_pct,
            "cogs": cogs,
            "remaining_after_wb_and_cogs": remaining_after_wb_and_cogs,
            "fact_sales": report_sales,
            "profit": remaining_after_wb_and_cogs if can_show_remaining else 0.0,
            "ad_spend": advertising["effective_spend"],
            "drr_pct": advertising["drr_pct"],
            "total_sales_drr_pct": advertising["total_sales_drr_pct"],
        }
        quality = {
            "orders_rows": len(orders),
            "sales_rows": len(sales),
            "reports_count": len(daily_reports),
            "daily_reports_count": len(daily_reports),
            "weekly_reports_count": len(weekly_reports),
            "finance_complete": finance_complete,
            "selected_inside_finance_coverage": selected_inside_finance_coverage,
            "finance_source": "daily",
            "cost_complete": cost_complete,
            "can_show_remaining": can_show_remaining,
            "products_total": products_total,
            "products_with_cost": products_with_cost,
            "catalog_match_pct": catalog_match_pct,
            "cost_coverage_pct": cost_coverage_pct,
            "cost_transaction_units": cost_transaction_units,
            "cost_covered_units": cost_covered_units,
            "catalog_matched_units": catalog_matched_units,
            "missing_cost_articles": sorted(missing_cost_articles)[:30],
            "unmatched_articles": sorted(unmatched_articles)[:30],
            "finance_periods": [f"{coverage_start} → {coverage_end}"] if coverage_start else [],
            "daily_coverage": daily_coverage_dict,
            "weekly_coverage": weekly_coverage_dict,
            "ads_rows": sum(1 for _ in advertising["rows"]),
            "ads_coverage": advertising.get("coverage", {}),
        }
        report_breakdown = {
            "for_pay": report_for_pay,
            "delivery": report_delivery,
            "storage": report_storage,
            "acceptance": report_acceptance,
            "deduction": report_deduction,
            "penalty": report_penalty,
            "additional": report_additional,
        }
        status = {
            "last_updated_at": str(last_success[0] if last_success else ""),
            "local_date_from": local_date_from,
            "local_date_to": local_date_to,
            "coverage": coverage,
        }
        return {
            "date_from": date_from,
            "date_to": date_to,
            "totals": totals,
            "quality": quality,
            "rows": sku_rows,
            "report_rows": report_rows,
            "weekly_report_rows": weekly_report_rows,
            "report_breakdown": report_breakdown,
            "sync_runs": sync_runs,
            "cost_history": cost_history,
            "product_options": product_options,
            "status": status,
            "today": today,
            "advertising": advertising,
        }

