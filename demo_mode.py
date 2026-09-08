from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from catalog.repository import ensure_catalog_schema
from economy.repository import ensure_economy_schema, save_orders, save_sales
from marking_db import ensure_database

DEMO_SEED_VERSION = "1"


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=20)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _gtin14(body13: str) -> str:
    digits = [int(x) for x in body13]
    total = sum(d * (3 if (len(digits) - i) % 2 else 1) for i, d in enumerate(digits))
    return body13 + str((10 - total % 10) % 10)


def prepare_demo_database(path: str | Path) -> Path:
    """Create a tiny deterministic synthetic workspace for DEMO mode.

    The database is generated locally and can be deleted at any time. No
    production identifiers or user data are copied into it.
    """
    db_path = Path(path).resolve()
    ensure_database(db_path)
    ensure_catalog_schema(db_path)
    ensure_economy_schema(db_path)

    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT value FROM schema_meta WHERE key='demo_seed_version'"
        ).fetchone()
        if row and str(row[0]) == DEMO_SEED_VERSION:
            return db_path

        perfume_gtin = _gtin14("4600000000010")
        deodorant_gtin = _gtin14("4600000000020")
        sample_gtin = _gtin14("4600000000030")
        products = [
            {
                "seller_article": "PRF-R0301",
                "product": "Demo No.17 Eau de Parfum",
                "volume": "30 мл",
                "category": "Парфюмерия",
                "marking_profile": "PERFUMERY",
                "kiz_required": 1,
                "gtin": perfume_gtin,
                "product_group": "perfumery",
                "template_id": 9,
                "cis_type": "UNIT",
                "tnved_code": "3303009000",
                "marketing_title": "Demo No.17",
                "cost_price_rub": 390,
                "seller_price_rub": 1490,
                "active": 1,
            },
            {
                "seller_article": "SPA-DEOD07",
                "product": "Demo Natural Deodorant",
                "volume": "50 мл",
                "category": "СПА",
                "marking_profile": "CHEMISTRY",
                "kiz_required": 1,
                "gtin": deodorant_gtin,
                "product_group": "chemistry",
                "template_id": 46,
                "cis_type": "UNIT",
                "tnved_code": "3307200000",
                "marketing_title": "Demo Deodorant",
                "cost_price_rub": 170,
                "seller_price_rub": 690,
                "active": 1,
            },
            {
                "seller_article": "DEMO-SAMPLE",
                "product": "Demo Sample",
                "volume": "3 мл",
                "category": "Парфюмерия",
                "marking_profile": "NONE",
                "kiz_required": 0,
                "gtin": sample_gtin,
                "product_group": "",
                "template_id": 0,
                "cis_type": "UNIT",
                "marketing_title": "Demo Sample",
                "cost_price_rub": 60,
                "seller_price_rub": 279,
                "active": 1,
            },
        ]
        columns = [
            "seller_article", "product", "volume", "category", "marking_profile",
            "kiz_required", "gtin", "product_group", "template_id", "cis_type",
            "tnved_code", "marketing_title", "cost_price_rub", "seller_price_rub", "active",
        ]
        placeholders = ",".join("?" for _ in columns)
        updates = ",".join(f"{c}=excluded.{c}" for c in columns if c != "seller_article")
        for item in products:
            conn.execute(
                f"INSERT INTO products({','.join(columns)}) VALUES({placeholders}) "
                f"ON CONFLICT(seller_article) DO UPDATE SET {updates}, updated_at=CURRENT_TIMESTAMP",
                [item.get(c, "") for c in columns],
            )

        supplies = [
            ("WB-GI-DEMO-ASSEMBLY", "Demo · На сборке", 0, "2026-09-07T08:10:00Z", "", "", "На сборке", 2),
            ("WB-GI-DEMO-READY", "Demo · Ждет отгрузки", 1, "2026-09-06T11:20:00Z", "2026-09-07T07:45:00Z", "", "Передано WB", 1),
            ("WB-GI-DEMO-DONE", "Demo · Доставлено", 1, "2026-09-03T09:00:00Z", "2026-09-03T14:00:00Z", "2026-09-04T10:30:00Z", "Доставлено", 1),
        ]
        for sid, name, done, created, closed, scan, label, count in supplies:
            conn.execute(
                """
                INSERT INTO fbs_supply_registry(
                    supply_id,name,done,created_at_wb,closed_at_wb,scan_dt_wb,
                    status_label,order_count,raw_json,last_sync_at
                ) VALUES(?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)
                ON CONFLICT(supply_id) DO UPDATE SET
                    name=excluded.name,done=excluded.done,created_at_wb=excluded.created_at_wb,
                    closed_at_wb=excluded.closed_at_wb,scan_dt_wb=excluded.scan_dt_wb,
                    status_label=excluded.status_label,order_count=excluded.order_count,
                    raw_json=excluded.raw_json,last_sync_at=CURRENT_TIMESTAMP
                """,
                (sid, name, done, created, closed, scan, label, count, json.dumps({"demo": True}, ensure_ascii=False)),
            )

        demo_orders = [
            (910000001, "WB-GI-DEMO-ASSEMBLY", "PRF-R0301", 1, "2026-09-07T08:12:00Z", "waiting", "confirm"),
            (910000002, "WB-GI-DEMO-ASSEMBLY", "SPA-DEOD07", 2, "2026-09-07T08:14:00Z", "waiting", "confirm"),
            (910000003, "WB-GI-DEMO-READY", "PRF-R0301", 1, "2026-09-06T11:25:00Z", "sorted", "complete"),
            (910000004, "WB-GI-DEMO-DONE", "DEMO-SAMPLE", 1, "2026-09-03T09:05:00Z", "sold", "complete"),
        ]
        for oid, sid, article, wh, created, wb_status, supplier_status in demo_orders:
            conn.execute(
                """
                INSERT INTO fbs_order_registry(
                    order_id,supply_id,seller_article,warehouse_id,created_at_wb,
                    wb_status,supplier_status,raw_json,last_sync_at
                ) VALUES(?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)
                ON CONFLICT(order_id) DO UPDATE SET
                    supply_id=excluded.supply_id,seller_article=excluded.seller_article,
                    warehouse_id=excluded.warehouse_id,created_at_wb=excluded.created_at_wb,
                    wb_status=excluded.wb_status,supplier_status=excluded.supplier_status,
                    raw_json=excluded.raw_json,last_sync_at=CURRENT_TIMESTAMP
                """,
                (oid, sid, article, wh, created, wb_status, supplier_status, json.dumps({"demo": True}, ensure_ascii=False)),
            )

        # A small mixed marking state: perfume introduced, deodorant applied.
        perfume_code = f"01{perfume_gtin}21DEMO000001\x1d91DEMO\x1d92SYNTHETIC"
        deodorant_code = f"01{deodorant_gtin}21DEMO000002\x1d91DEMO\x1d92SYNTHETIC"
        conn.execute(
            """
            INSERT OR IGNORE INTO marking_codes(
                raw_code,gtin,serial,seller_article,supply_id,order_id,status,
                utilization_status,circulation_status,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)
            """,
            (perfume_code, perfume_gtin, "DEMO000001", "PRF-R0301", "WB-GI-DEMO-ASSEMBLY", 910000001, "assigned", "", "INTRODUCED"),
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO marking_codes(
                raw_code,gtin,serial,seller_article,supply_id,order_id,status,
                utilization_status,circulation_status,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)
            """,
            (deodorant_code, deodorant_gtin, "DEMO000002", "SPA-DEOD07", "WB-GI-DEMO-ASSEMBLY", 910000002, "assigned", "APPLIED", ""),
        )
        conn.execute(
            "INSERT INTO schema_meta(key,value) VALUES('demo_seed_version',?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (DEMO_SEED_VERSION,),
        )
        conn.commit()

    save_orders(
        db_path,
        [
            {"date": "2026-09-05T10:00:00", "supplierArticle": "PRF-R0301", "totalPrice": 1690, "priceWithDisc": 1490, "finishedPrice": 1415, "srid": "demo-order-e1"},
            {"date": "2026-09-06T12:00:00", "supplierArticle": "SPA-DEOD07", "totalPrice": 890, "priceWithDisc": 690, "finishedPrice": 655, "srid": "demo-order-e2"},
            {"date": "2026-09-07T09:30:00", "supplierArticle": "DEMO-SAMPLE", "totalPrice": 349, "priceWithDisc": 279, "finishedPrice": 265, "srid": "demo-order-e3"},
        ],
    )
    save_sales(
        db_path,
        [
            {"date": "2026-09-05T16:00:00", "supplierArticle": "PRF-R0301", "saleID": "S-DEMO-1", "priceWithDisc": 1490, "finishedPrice": 1415, "forPay": 1070, "srid": "demo-sale-e1"},
            {"date": "2026-09-06T18:00:00", "supplierArticle": "SPA-DEOD07", "saleID": "S-DEMO-2", "priceWithDisc": 690, "finishedPrice": 655, "forPay": 480, "srid": "demo-sale-e2"},
        ],
    )
    return db_path
