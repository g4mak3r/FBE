from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable


SCHEMA_VERSION = 13

# WB wbStatus values that mean the order is no longer operational.
# Keep canceled variants out of "new" / "assembling" fallbacks even when
# supplierStatus remains new/confirm in historical registry rows.
WB_TERMINAL_STATUSES = {
    "sold",
    "canceled",
    "canceled_by_client",
    "declined_by_client",
    "defect",
    "canceled_by_missed_call",
}
WB_CANCELED_STATUSES = WB_TERMINAL_STATUSES - {"sold"}


DDL = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA busy_timeout=20000;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS marking_codes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    raw_code TEXT NOT NULL UNIQUE,
    gtin TEXT NOT NULL,
    serial TEXT DEFAULT '',
    seller_article TEXT DEFAULT '',
    supply_id TEXT DEFAULT '',
    order_id INTEGER,
    status TEXT NOT NULL DEFAULT 'received',
    wb_decision TEXT DEFAULT '',
    wb_details TEXT DEFAULT '',
    suz_order_id INTEGER,
    suz_block_id TEXT DEFAULT '',
    assigned_at TEXT DEFAULT '',
    printed_at TEXT DEFAULT '',
    wb_sent_at TEXT DEFAULT '',
    utilization_status TEXT DEFAULT '',
    utilization_report_id TEXT DEFAULT '',
    circulation_status TEXT DEFAULT '',
    circulation_document_id TEXT DEFAULT '',
    error_text TEXT DEFAULT '',
    wb_status TEXT DEFAULT '',
    post_sale_status TEXT DEFAULT '',
    post_sale_updated_at TEXT DEFAULT '',
    retire_document_id TEXT DEFAULT '',
    retired_at TEXT DEFAULT '',
    return_document_id TEXT DEFAULT '',
    returned_at TEXT DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_marking_codes_order_unique
ON marking_codes(order_id)
WHERE order_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_marking_codes_gtin_status
ON marking_codes(gtin, status);

CREATE INDEX IF NOT EXISTS idx_marking_codes_order_id
ON marking_codes(order_id);

CREATE INDEX IF NOT EXISTS idx_marking_codes_supply_order
ON marking_codes(supply_id, order_id);

CREATE INDEX IF NOT EXISTS idx_marking_codes_utilisation_report
ON marking_codes(utilization_report_id);

CREATE INDEX IF NOT EXISTS idx_marking_codes_circulation_document
ON marking_codes(circulation_document_id);

CREATE TABLE IF NOT EXISTS marking_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    marking_code_id INTEGER,
    event_type TEXT NOT NULL,
    supply_id TEXT DEFAULT '',
    order_id INTEGER,
    details TEXT DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(marking_code_id) REFERENCES marking_codes(id)
);

CREATE TABLE IF NOT EXISTS suz_orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    supply_id TEXT NOT NULL,
    oms_order_id TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'created',
    payload_json TEXT NOT NULL DEFAULT '{}',
    response_json TEXT NOT NULL DEFAULT '{}',
    expected_complete_ms INTEGER DEFAULT 0,
    error_text TEXT DEFAULT '',
    closed INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_suz_orders_supply
ON suz_orders(supply_id, created_at DESC);

CREATE TABLE IF NOT EXISTS suz_order_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    suz_order_id INTEGER NOT NULL,
    gtin TEXT NOT NULL,
    requested_quantity INTEGER NOT NULL,
    received_quantity INTEGER NOT NULL DEFAULT 0,
    buffer_status TEXT DEFAULT '',
    rejection_reason TEXT DEFAULT '',
    template_id INTEGER DEFAULT 0,
    block_id TEXT DEFAULT '',
    status_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(suz_order_id, gtin),
    FOREIGN KEY(suz_order_id) REFERENCES suz_orders(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS suz_order_drafts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    supply_id TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'draft',
    external_order_id TEXT DEFAULT '',
    error_text TEXT DEFAULT '',
    wb_status TEXT DEFAULT '',
    post_sale_status TEXT DEFAULT '',
    post_sale_updated_at TEXT DEFAULT '',
    retire_document_id TEXT DEFAULT '',
    retired_at TEXT DEFAULT '',
    return_document_id TEXT DEFAULT '',
    returned_at TEXT DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS circulation_documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    supply_id TEXT NOT NULL,
    document_uuid TEXT NOT NULL UNIQUE,
    document_type TEXT NOT NULL DEFAULT 'LP_INTRODUCE_GOODS',
    product_group TEXT NOT NULL DEFAULT 'perfumery',
    status TEXT NOT NULL DEFAULT 'submitted',
    payload_json TEXT NOT NULL DEFAULT '{}',
    response_json TEXT NOT NULL DEFAULT '{}',
    error_text TEXT DEFAULT '',
    wb_status TEXT DEFAULT '',
    post_sale_status TEXT DEFAULT '',
    post_sale_updated_at TEXT DEFAULT '',
    retire_document_id TEXT DEFAULT '',
    retired_at TEXT DEFAULT '',
    return_document_id TEXT DEFAULT '',
    returned_at TEXT DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_circulation_documents_supply
ON circulation_documents(supply_id, id DESC);

CREATE TABLE IF NOT EXISTS suz_utilisation_reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    supply_id TEXT NOT NULL,
    report_id TEXT NOT NULL UNIQUE,
    product_group TEXT NOT NULL DEFAULT 'chemistry',
    gtin TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'SUBMITTED',
    payload_json TEXT NOT NULL DEFAULT '{}',
    response_json TEXT NOT NULL DEFAULT '{}',
    error_text TEXT DEFAULT '',
    wb_status TEXT DEFAULT '',
    post_sale_status TEXT DEFAULT '',
    post_sale_updated_at TEXT DEFAULT '',
    retire_document_id TEXT DEFAULT '',
    retired_at TEXT DEFAULT '',
    return_document_id TEXT DEFAULT '',
    returned_at TEXT DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_suz_utilisation_reports_supply
ON suz_utilisation_reports(supply_id, id DESC);

CREATE TABLE IF NOT EXISTS fbs_supply_registry (
    supply_id TEXT PRIMARY KEY,
    name TEXT DEFAULT '',
    done INTEGER NOT NULL DEFAULT 0,
    created_at_wb TEXT DEFAULT '',
    closed_at_wb TEXT DEFAULT '',
    scan_dt_wb TEXT DEFAULT '',
    status_label TEXT DEFAULT '',
    order_count INTEGER NOT NULL DEFAULT -1,
    raw_json TEXT NOT NULL DEFAULT '{}',
    last_sync_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_fbs_supply_registry_sync
ON fbs_supply_registry(last_sync_at DESC);

CREATE TABLE IF NOT EXISTS fbs_order_registry (
    order_id INTEGER PRIMARY KEY,
    supply_id TEXT DEFAULT '',
    seller_article TEXT DEFAULT '',
    warehouse_id INTEGER NOT NULL DEFAULT 0,
    created_at_wb TEXT DEFAULT '',
    wb_status TEXT DEFAULT '',
    supplier_status TEXT DEFAULT '',
    wb_sgtin_state TEXT DEFAULT '',
    wb_sgtin_count INTEGER NOT NULL DEFAULT 0,
    wb_sgtin_preview TEXT DEFAULT '',
    wb_sgtin_values_json TEXT NOT NULL DEFAULT '[]',
    wb_meta_synced_at TEXT DEFAULT '',
    raw_json TEXT NOT NULL DEFAULT '{}',
    last_sync_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_fbs_order_registry_supply
ON fbs_order_registry(supply_id, order_id);
CREATE INDEX IF NOT EXISTS idx_fbs_order_registry_wb_status
ON fbs_order_registry(wb_status, last_sync_at DESC);
CREATE INDEX IF NOT EXISTS idx_fbs_order_registry_created
ON fbs_order_registry(created_at_wb, order_id);
CREATE INDEX IF NOT EXISTS idx_fbs_order_registry_supplier_status
ON fbs_order_registry(supplier_status, wb_status, supply_id);

CREATE TABLE IF NOT EXISTS fbs_sync_state (
    sync_key TEXT PRIMARY KEY,
    ok INTEGER NOT NULL DEFAULT 0,
    completed_at TEXT NOT NULL DEFAULT '',
    details_json TEXT NOT NULL DEFAULT '{}',
    error_text TEXT DEFAULT ''
);
"""


ALTERS = [
    "ALTER TABLE marking_codes ADD COLUMN wb_decision TEXT DEFAULT ''",
    "ALTER TABLE marking_codes ADD COLUMN wb_details TEXT DEFAULT ''",
    "ALTER TABLE marking_codes ADD COLUMN suz_order_id INTEGER",
    "ALTER TABLE marking_codes ADD COLUMN suz_block_id TEXT DEFAULT ''",
    "ALTER TABLE marking_codes ADD COLUMN assigned_at TEXT DEFAULT ''",
    "ALTER TABLE marking_codes ADD COLUMN printed_at TEXT DEFAULT ''",
    "ALTER TABLE marking_codes ADD COLUMN wb_sent_at TEXT DEFAULT ''",
    "ALTER TABLE marking_codes ADD COLUMN utilization_status TEXT DEFAULT ''",
    "ALTER TABLE marking_codes ADD COLUMN utilization_report_id TEXT DEFAULT ''",
    "ALTER TABLE marking_codes ADD COLUMN circulation_status TEXT DEFAULT ''",
    "ALTER TABLE marking_codes ADD COLUMN circulation_document_id TEXT DEFAULT ''",
    "ALTER TABLE marking_codes ADD COLUMN error_text TEXT DEFAULT ''",
    "ALTER TABLE marking_codes ADD COLUMN wb_status TEXT DEFAULT ''",
    "ALTER TABLE marking_codes ADD COLUMN post_sale_status TEXT DEFAULT ''",
    "ALTER TABLE marking_codes ADD COLUMN post_sale_updated_at TEXT DEFAULT ''",
    "ALTER TABLE marking_codes ADD COLUMN retire_document_id TEXT DEFAULT ''",
    "ALTER TABLE marking_codes ADD COLUMN retired_at TEXT DEFAULT ''",
    "ALTER TABLE marking_codes ADD COLUMN return_document_id TEXT DEFAULT ''",
    "ALTER TABLE marking_codes ADD COLUMN returned_at TEXT DEFAULT ''",
    "ALTER TABLE fbs_order_registry ADD COLUMN wb_sgtin_state TEXT DEFAULT ''",
    "ALTER TABLE fbs_order_registry ADD COLUMN wb_sgtin_count INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE fbs_order_registry ADD COLUMN wb_sgtin_preview TEXT DEFAULT ''",
    "ALTER TABLE fbs_order_registry ADD COLUMN wb_sgtin_values_json TEXT NOT NULL DEFAULT '[]'",
    "ALTER TABLE fbs_order_registry ADD COLUMN wb_meta_synced_at TEXT DEFAULT ''",
]


class MarkingDbError(RuntimeError):
    pass


def _connect(path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(Path(path), timeout=20)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=20000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def ensure_database(path: str | Path) -> Path:
    db_path = Path(path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with _connect(db_path) as conn:
        conn.executescript(DDL)
        for statement in ALTERS:
            try:
                conn.execute(statement)
            except sqlite3.OperationalError as exc:
                if "duplicate column name" not in str(exc).lower():
                    raise
        conn.execute(
            "INSERT INTO schema_meta(key, value) VALUES('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(SCHEMA_VERSION),),
        )
        conn.commit()
    return db_path


def _row_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def list_assignments(path: str | Path, order_ids: Iterable[int]) -> dict[int, dict[str, Any]]:
    ids = sorted({int(x) for x in order_ids})
    if not ids:
        return {}
    placeholders = ",".join("?" for _ in ids)
    with _connect(path) as conn:
        rows = conn.execute(
            f"SELECT * FROM marking_codes WHERE order_id IN ({placeholders})",
            ids,
        ).fetchall()
    return {int(row["order_id"]): dict(row) for row in rows if row["order_id"] is not None}


def get_assignment(path: str | Path, order_id: int) -> dict[str, Any] | None:
    with _connect(path) as conn:
        row = conn.execute(
            "SELECT * FROM marking_codes WHERE order_id = ?",
            (int(order_id),),
        ).fetchone()
    return _row_dict(row)


def validate_assignment_candidate(
    path: str | Path,
    *,
    raw_code: str,
    order_id: int,
) -> dict[str, Any]:
    """Validate uniqueness without writing anything to the database."""
    raw_code = str(raw_code or "")
    if not raw_code:
        raise MarkingDbError("Пустой код маркировки")

    with _connect(path) as conn:
        duplicate = conn.execute(
            "SELECT * FROM marking_codes WHERE raw_code = ?",
            (raw_code,),
        ).fetchone()
        existing = conn.execute(
            "SELECT * FROM marking_codes WHERE order_id = ?",
            (int(order_id),),
        ).fetchone()

    if duplicate is not None and duplicate["order_id"] is not None and int(duplicate["order_id"]) != int(order_id):
        raise MarkingDbError(
            f"Этот КИЗ уже закреплен за другим сборочным заданием: {duplicate['order_id']}"
        )
    if existing is not None and str(existing["raw_code"] or "") != raw_code:
        status = str(existing["status"] or "")
        if status in {"sent_wb", "accepted_wb"}:
            raise MarkingDbError("Для задания уже отправлен другой КИЗ в WB")
        raise MarkingDbError("Для этого задания уже сохранен другой КИЗ. Сначала удалите локальную привязку")

    return {
        "already_saved_for_order": bool(existing is not None and str(existing["raw_code"] or "") == raw_code),
        "code_id": int(duplicate["id"]) if duplicate is not None else None,
        "source": "suz" if duplicate is not None and duplicate["suz_order_id"] is not None else "scan",
    }


def assign_code(
    path: str | Path,
    *,
    raw_code: str,
    gtin: str,
    serial: str,
    seller_article: str,
    supply_id: str,
    order_id: int,
) -> dict[str, Any]:
    raw_code = str(raw_code)
    if not raw_code:
        raise MarkingDbError("Пустой код маркировки")

    with _connect(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        duplicate = conn.execute(
            "SELECT * FROM marking_codes WHERE raw_code = ?",
            (raw_code,),
        ).fetchone()
        if duplicate is not None and duplicate["order_id"] is not None and int(duplicate["order_id"]) != int(order_id):
            raise MarkingDbError(
                f"Этот КИЗ уже закреплен за другим сборочным заданием: {duplicate['order_id']}"
            )

        existing = conn.execute(
            "SELECT * FROM marking_codes WHERE order_id = ?",
            (int(order_id),),
        ).fetchone()
        if existing is not None and existing["raw_code"] != raw_code:
            status = str(existing["status"] or "")
            if status in {"sent_wb", "accepted_wb"}:
                raise MarkingDbError("Для задания уже отправлен другой КИЗ в WB. Сначала удалите метаданные в WB вручную.")
            raise MarkingDbError("Для этого задания уже сохранен другой КИЗ. Сначала удалите локальную привязку.")

        if duplicate is None:
            cur = conn.execute(
                """
                INSERT INTO marking_codes(
                    raw_code, gtin, serial, seller_article, supply_id, order_id, status
                ) VALUES (?, ?, ?, ?, ?, ?, 'assigned_local')
                """,
                (raw_code, gtin, serial, seller_article, supply_id, int(order_id)),
            )
            code_id = int(cur.lastrowid)
            conn.execute("UPDATE marking_codes SET assigned_at=CURRENT_TIMESTAMP WHERE id=?", (code_id,))
        else:
            code_id = int(duplicate["id"])
            conn.execute(
                """
                UPDATE marking_codes
                SET gtin=?, serial=?, seller_article=?, supply_id=?, order_id=?,
                    status='assigned_local', assigned_at=COALESCE(NULLIF(assigned_at,''), CURRENT_TIMESTAMP), updated_at=CURRENT_TIMESTAMP
                WHERE id=?
                """,
                (gtin, serial, seller_article, supply_id, int(order_id), code_id),
            )

        conn.execute(
            "INSERT INTO marking_events(marking_code_id, event_type, supply_id, order_id, details) VALUES (?, ?, ?, ?, ?)",
            (code_id, "assigned_local", supply_id, int(order_id), json.dumps({"gtin": gtin}, ensure_ascii=False)),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM marking_codes WHERE id=?", (code_id,)).fetchone()
    return dict(row)


def remove_local_assignment(
    path: str | Path,
    order_id: int,
    *,
    supply_id: str | None = None,
) -> None:
    """Remove a local assignment without depending on a fresh WB/product lookup.

    A malformed scan must remain removable even when the order has moved, WB is
    temporarily unavailable, or the product catalog was updated after the scan.
    """
    with _connect(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM marking_codes WHERE order_id=?", (int(order_id),)).fetchone()
        if row is None:
            return
        if supply_id is not None and str(row["supply_id"] or "") not in {"", str(supply_id)}:
            raise MarkingDbError("КИЗ закреплен за заданием из другой поставки")
        status = str(row["status"] or "")
        if status in {"sent_wb", "accepted_wb"}:
            raise MarkingDbError("КИЗ уже отправлен в WB. Локальное удаление заблокировано.")
        conn.execute(
            "INSERT INTO marking_events(marking_code_id, event_type, supply_id, order_id, details) VALUES (?, 'removed_local', ?, ?, '')",
            (int(row["id"]), str(row["supply_id"] or ""), int(order_id)),
        )
        # Codes obtained from SUZ return to the free pool instead of being deleted.
        if row["suz_order_id"] is not None:
            conn.execute(
                """
                UPDATE marking_codes
                SET seller_article='', supply_id='', order_id=NULL, status='received_suz',
                    updated_at=CURRENT_TIMESTAMP
                WHERE id=?
                """,
                (int(row["id"]),),
            )
        else:
            # Preserve the audit trail. Deleting the code row would violate the
            # foreign key from marking_events; a removed manual scan is kept as
            # an inactive reusable record instead.
            conn.execute(
                """
                UPDATE marking_codes
                SET seller_article='', supply_id='', order_id=NULL, status='removed_local',
                    wb_decision='', wb_details='', updated_at=CURRENT_TIMESTAMP
                WHERE id=?
                """,
                (int(row["id"]),),
            )
        conn.commit()


def update_wb_state(
    path: str | Path,
    order_id: int,
    *,
    status: str | None = None,
    decision: str | None = None,
    details: str | None = None,
    event_type: str | None = None,
) -> dict[str, Any] | None:
    with _connect(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM marking_codes WHERE order_id=?", (int(order_id),)).fetchone()
        if row is None:
            return None
        new_status = status if status is not None else str(row["status"] or "")
        new_decision = decision if decision is not None else str(row["wb_decision"] or "")
        new_details = details if details is not None else str(row["wb_details"] or "")
        conn.execute(
            """
            UPDATE marking_codes
            SET status=?, wb_decision=?, wb_details=?,
                wb_sent_at=CASE WHEN ? IN ('sent_wb','accepted_wb') AND COALESCE(wb_sent_at,'')='' THEN CURRENT_TIMESTAMP ELSE wb_sent_at END,
                updated_at=CURRENT_TIMESTAMP
            WHERE id=?
            """,
            (new_status, new_decision, new_details, new_status, int(row["id"])),
        )
        if event_type:
            conn.execute(
                "INSERT INTO marking_events(marking_code_id, event_type, supply_id, order_id, details) VALUES (?, ?, ?, ?, ?)",
                (int(row["id"]), event_type, str(row["supply_id"] or ""), int(order_id), new_details),
            )
        conn.commit()
        updated = conn.execute("SELECT * FROM marking_codes WHERE id=?", (int(row["id"]),)).fetchone()
    return _row_dict(updated)


def create_suz_order(
    path: str | Path,
    *,
    supply_id: str,
    oms_order_id: str,
    expected_complete_ms: int,
    payload: dict[str, Any],
    response: dict[str, Any],
) -> dict[str, Any]:
    with _connect(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute(
            """
            INSERT INTO suz_orders(
                supply_id, oms_order_id, status, payload_json, response_json, expected_complete_ms
            ) VALUES (?, ?, 'created', ?, ?, ?)
            """,
            (
                str(supply_id),
                str(oms_order_id),
                json.dumps(payload, ensure_ascii=False),
                json.dumps(response, ensure_ascii=False),
                int(expected_complete_ms or 0),
            ),
        )
        local_id = int(cur.lastrowid)
        for product in payload.get("products") or []:
            conn.execute(
                """
                INSERT INTO suz_order_items(
                    suz_order_id, gtin, requested_quantity, template_id
                ) VALUES (?, ?, ?, ?)
                """,
                (
                    local_id,
                    str(product.get("gtin") or ""),
                    int(product.get("quantity") or 0),
                    int(product.get("templateId") or 0),
                ),
            )
        conn.commit()
        row = conn.execute("SELECT * FROM suz_orders WHERE id=?", (local_id,)).fetchone()
    return dict(row)


def get_suz_order(path: str | Path, local_id: int) -> dict[str, Any] | None:
    with _connect(path) as conn:
        order = conn.execute("SELECT * FROM suz_orders WHERE id=?", (int(local_id),)).fetchone()
        if order is None:
            return None
        items = conn.execute(
            "SELECT * FROM suz_order_items WHERE suz_order_id=? ORDER BY id",
            (int(local_id),),
        ).fetchall()
    result = dict(order)
    result["items"] = [dict(item) for item in items]
    return result


def list_suz_orders(path: str | Path, supply_id: str, limit: int = 20) -> list[dict[str, Any]]:
    with _connect(path) as conn:
        orders = conn.execute(
            "SELECT * FROM suz_orders WHERE supply_id=? ORDER BY id DESC LIMIT ?",
            (str(supply_id), int(limit)),
        ).fetchall()
        result: list[dict[str, Any]] = []
        for order in orders:
            items = conn.execute(
                "SELECT * FROM suz_order_items WHERE suz_order_id=? ORDER BY id",
                (int(order["id"]),),
            ).fetchall()
            row = dict(order)
            row["items"] = [dict(item) for item in items]
            result.append(row)
    return result


def list_open_suz_orders(path: str | Path, supply_id: str, limit: int = 50) -> list[dict[str, Any]]:
    with _connect(path) as conn:
        rows = conn.execute(
            """
            SELECT id FROM suz_orders
            WHERE supply_id=? AND closed=0 AND status NOT IN ('rejected', 'closed', 'closed_auto')
            ORDER BY id
            LIMIT ?
            """,
            (str(supply_id), int(limit)),
        ).fetchall()
    return [get_suz_order(path, int(row["id"])) for row in rows if row["id"] is not None]


def update_suz_item_status(
    path: str | Path,
    *,
    local_order_id: int,
    gtin: str,
    status_data: dict[str, Any],
) -> None:
    with _connect(path) as conn:
        conn.execute(
            """
            UPDATE suz_order_items
            SET buffer_status=?, rejection_reason=?, template_id=?, status_json=?, updated_at=CURRENT_TIMESTAMP
            WHERE suz_order_id=? AND gtin=?
            """,
            (
                str(status_data.get("bufferStatus") or ""),
                str(status_data.get("rejectionReason") or ""),
                int(status_data.get("templateId") or 0),
                json.dumps(status_data, ensure_ascii=False),
                int(local_order_id),
                str(gtin),
            ),
        )
        conn.commit()


def update_suz_order_state(
    path: str | Path,
    local_order_id: int,
    *,
    status: str | None = None,
    error_text: str | None = None,
    closed: bool | None = None,
) -> None:
    updates: list[str] = ["updated_at=CURRENT_TIMESTAMP"]
    values: list[Any] = []
    if status is not None:
        updates.append("status=?")
        values.append(str(status))
    if error_text is not None:
        updates.append("error_text=?")
        values.append(str(error_text))
    if closed is not None:
        updates.append("closed=?")
        values.append(1 if closed else 0)
    values.append(int(local_order_id))
    with _connect(path) as conn:
        conn.execute(f"UPDATE suz_orders SET {', '.join(updates)} WHERE id=?", values)
        conn.commit()


def save_suz_codes(
    path: str | Path,
    *,
    local_order_id: int,
    gtin: str,
    block_id: str,
    parsed_codes: list[dict[str, str]],
) -> int:
    inserted = 0
    with _connect(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        for code in parsed_codes:
            raw_code = str(code.get("raw_code") or "")
            if not raw_code:
                continue
            existing = conn.execute("SELECT id FROM marking_codes WHERE raw_code=?", (raw_code,)).fetchone()
            if existing is not None:
                continue
            cur = conn.execute(
                """
                INSERT INTO marking_codes(
                    raw_code, gtin, serial, status, suz_order_id, suz_block_id
                ) VALUES (?, ?, ?, 'received_suz', ?, ?)
                """,
                (
                    raw_code,
                    str(code.get("gtin") or gtin),
                    str(code.get("serial") or ""),
                    int(local_order_id),
                    str(block_id or ""),
                ),
            )
            code_id = int(cur.lastrowid)
            conn.execute(
                "INSERT INTO marking_events(marking_code_id, event_type, details) VALUES (?, 'received_suz', ?)",
                (code_id, json.dumps({"gtin": gtin, "block_id": block_id}, ensure_ascii=False)),
            )
            inserted += 1
        if inserted:
            conn.execute(
                """
                UPDATE suz_order_items
                SET received_quantity=received_quantity+?, block_id=?, updated_at=CURRENT_TIMESTAMP
                WHERE suz_order_id=? AND gtin=?
                """,
                (inserted, str(block_id or ""), int(local_order_id), str(gtin)),
            )
        conn.commit()
    return inserted


def count_supply_suz_ordered_codes_by_gtin(path: str | Path, supply_id: str) -> dict[str, int]:
    """Count all successfully created SUZ quantities for one WB supply.

    v0.66 no longer treats raw/scanned codes as a reusable global pool. A code
    order belongs to the supply for which it was created. Closed orders still
    count, so reopening the screen cannot accidentally order the same deficit a
    second time. Only explicitly rejected orders are ignored.
    """
    with _connect(path) as conn:
        rows = conn.execute(
            """
            SELECT item.gtin, SUM(item.requested_quantity) AS qty
            FROM suz_order_items AS item
            JOIN suz_orders AS ord ON ord.id = item.suz_order_id
            WHERE ord.supply_id = ?
              AND ord.status NOT IN ('rejected', 'create_error')
            GROUP BY item.gtin
            """,
            (str(supply_id),),
        ).fetchall()
    return {str(row["gtin"]): int(row["qty"] or 0) for row in rows}



def assign_suz_codes_to_supply_orders(
    path: str | Path,
    *,
    supply_id: str,
    order_rows: list[dict[str, Any]],
) -> dict[str, int]:
    """Permanently assign downloaded OMS codes to concrete WB assembly orders.

    Existing assignments are never rearranged. New assignments are deterministic:
    WB order IDs are sorted ascending inside each GTIN, and OMS codes are taken in
    their local receipt order. Only codes ordered for this exact supply are used.
    """
    eligible: dict[str, list[dict[str, Any]]] = {}
    for item in order_rows:
        order_id = int(item.get("order_id") or 0)
        gtin = str(item.get("gtin14") or item.get("gtin") or "")
        if not order_id or len(gtin) != 14 or item.get("gtin_error"):
            continue
        if item.get("assignment"):
            continue
        eligible.setdefault(gtin, []).append(item)
    for rows in eligible.values():
        rows.sort(key=lambda row: int(row.get("order_id") or 0))

    assigned = 0
    shortages = 0
    with _connect(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        for gtin, rows in sorted(eligible.items()):
            free_codes = conn.execute(
                """
                SELECT mc.*
                FROM marking_codes AS mc
                JOIN suz_orders AS so ON so.id = mc.suz_order_id
                WHERE so.supply_id = ?
                  AND mc.gtin = ?
                  AND mc.order_id IS NULL
                  AND mc.status IN ('received_suz', 'available')
                ORDER BY mc.id
                """,
                (str(supply_id), str(gtin)),
            ).fetchall()
            for order_row, code_row in zip(rows, free_codes):
                order_id = int(order_row.get("order_id") or 0)
                seller_article = str(order_row.get("seller_article") or "")
                cur = conn.execute(
                    """
                    UPDATE marking_codes
                    SET seller_article=?, supply_id=?, order_id=?, status='assigned_local',
                        assigned_at=COALESCE(NULLIF(assigned_at,''), CURRENT_TIMESTAMP),
                        updated_at=CURRENT_TIMESTAMP
                    WHERE id=? AND order_id IS NULL
                    """,
                    (seller_article, str(supply_id), order_id, int(code_row["id"])),
                )
                if cur.rowcount:
                    conn.execute(
                        """
                        INSERT INTO marking_events(
                            marking_code_id, event_type, supply_id, order_id, details
                        ) VALUES (?, 'assigned_from_suz', ?, ?, ?)
                        """,
                        (
                            int(code_row["id"]),
                            str(supply_id),
                            order_id,
                            json.dumps(
                                {
                                    "gtin": gtin,
                                    "seller_article": seller_article,
                                    "assignment_rule": "order_id_asc/code_id_asc",
                                },
                                ensure_ascii=False,
                            ),
                        ),
                    )
                    assigned += 1
            shortages += max(0, len(rows) - len(free_codes))
        conn.commit()
    return {"assigned": assigned, "shortages": shortages}


def list_supply_code_assignments(path: str | Path, supply_id: str) -> list[dict[str, Any]]:
    """Return complete, lossless code records for one supply with OMS references."""
    with _connect(path) as conn:
        rows = conn.execute(
            """
            SELECT mc.*, so.oms_order_id, so.payload_json AS suz_payload_json
            FROM marking_codes AS mc
            LEFT JOIN suz_orders AS so ON so.id = mc.suz_order_id
            WHERE mc.supply_id = ? AND mc.order_id IS NOT NULL
            ORDER BY mc.order_id, mc.id
            """,
            (str(supply_id),),
        ).fetchall()
    result: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        try:
            payload = json.loads(str(item.get("suz_payload_json") or "{}"))
        except Exception:
            payload = {}
        item["product_group"] = str(payload.get("productGroup") or "") if isinstance(payload, dict) else ""
        result.append(item)
    return result


def mark_supply_codes_printed(path: str | Path, supply_id: str, order_ids: Iterable[int]) -> int:
    ids = sorted({int(value) for value in order_ids if int(value) > 0})
    if not ids:
        return 0
    placeholders = ",".join("?" for _ in ids)
    params: list[Any] = [str(supply_id), *ids]
    with _connect(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(
            f"SELECT id, order_id FROM marking_codes WHERE supply_id=? AND order_id IN ({placeholders})",
            params,
        ).fetchall()
        conn.execute(
            f"""
            UPDATE marking_codes
            SET printed_at=CASE WHEN COALESCE(printed_at,'')='' THEN CURRENT_TIMESTAMP ELSE printed_at END,
                status=CASE WHEN status='assigned_local' THEN 'printed' ELSE status END,
                updated_at=CURRENT_TIMESTAMP
            WHERE supply_id=? AND order_id IN ({placeholders})
            """,
            params,
        )
        for row in rows:
            conn.execute(
                """
                INSERT INTO marking_events(marking_code_id, event_type, supply_id, order_id, details)
                VALUES (?, 'printed_30x20', ?, ?, '')
                """,
                (int(row["id"]), str(supply_id), int(row["order_id"])),
            )
        conn.commit()
    return len(rows)


# ---------------- OMS utilisation/application reports ----------------

def create_suz_utilisation_report(
    path: str | Path,
    *,
    supply_id: str,
    report_id: str,
    product_group: str,
    gtin: str,
    payload: dict[str, Any],
    response: dict[str, Any],
    order_ids: Iterable[int],
) -> dict[str, Any]:
    ids = sorted({int(value) for value in order_ids if int(value) > 0})
    with _connect(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """
            INSERT INTO suz_utilisation_reports(
                supply_id, report_id, product_group, gtin, status, payload_json, response_json
            ) VALUES (?, ?, ?, ?, 'SUBMITTED', ?, ?)
            ON CONFLICT(report_id) DO UPDATE SET
                response_json=excluded.response_json, updated_at=CURRENT_TIMESTAMP
            """,
            (
                str(supply_id), str(report_id), str(product_group), str(gtin),
                json.dumps(payload, ensure_ascii=False), json.dumps(response, ensure_ascii=False),
            ),
        )
        if ids:
            placeholders = ",".join("?" for _ in ids)
            rows = conn.execute(
                f"SELECT id, order_id FROM marking_codes WHERE supply_id=? AND order_id IN ({placeholders})",
                [str(supply_id), *ids],
            ).fetchall()
            conn.execute(
                f"""
                UPDATE marking_codes
                SET utilization_report_id=?,
                    utilization_status=CASE
                        WHEN UPPER(COALESCE(utilization_status,'')) IN ('APPLIED','APPLIED_NOT_PAID') THEN utilization_status
                        WHEN UPPER(COALESCE(circulation_status,''))='INTRODUCED' THEN utilization_status
                        ELSE 'SUBMITTED'
                    END,
                    error_text='', updated_at=CURRENT_TIMESTAMP
                WHERE supply_id=? AND order_id IN ({placeholders})
                """,
                [str(report_id), str(supply_id), *ids],
            )
            for row in rows:
                conn.execute(
                    """
                    INSERT INTO marking_events(marking_code_id, event_type, supply_id, order_id, details)
                    VALUES (?, 'utilisation_submitted', ?, ?, ?)
                    """,
                    (int(row['id']), str(supply_id), int(row['order_id']), str(report_id)),
                )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM suz_utilisation_reports WHERE report_id=?", (str(report_id),)
        ).fetchone()
    return dict(row)


def list_suz_utilisation_reports(path: str | Path, supply_id: str, limit: int = 50) -> list[dict[str, Any]]:
    with _connect(path) as conn:
        rows = conn.execute(
            "SELECT * FROM suz_utilisation_reports WHERE supply_id=? ORDER BY id DESC LIMIT ?",
            (str(supply_id), int(limit)),
        ).fetchall()
    return [dict(row) for row in rows]


def update_suz_utilisation_report(
    path: str | Path,
    report_id: str,
    *,
    status: str,
    response: dict[str, Any] | None = None,
    error_text: str = '',
) -> dict[str, Any] | None:
    with _connect(path) as conn:
        conn.execute(
            """
            UPDATE suz_utilisation_reports
            SET status=?, response_json=?, error_text=?, updated_at=CURRENT_TIMESTAMP
            WHERE report_id=?
            """,
            (
                str(status), json.dumps(response or {}, ensure_ascii=False), str(error_text), str(report_id)
            ),
        )
        status_upper = str(status).upper()
        if status_upper == 'SUCCESS':
            conn.execute(
                """
                UPDATE marking_codes
                SET utilization_status='APPLIED', error_text='', updated_at=CURRENT_TIMESTAMP
                WHERE utilization_report_id=?
                  AND UPPER(COALESCE(circulation_status,''))!='INTRODUCED'
                """, (str(report_id),)
            )
        elif status_upper in {'PARTIALLY','REJECTED','ERROR','FAILED','CANCELLED'}:
            conn.execute(
                """
                UPDATE marking_codes
                SET utilization_status=?, error_text=?, updated_at=CURRENT_TIMESTAMP
                WHERE utilization_report_id=?
                  AND UPPER(COALESCE(utilization_status,'')) NOT IN ('APPLIED','APPLIED_NOT_PAID')
                  AND UPPER(COALESCE(circulation_status,''))!='INTRODUCED'
                """, (status_upper, str(error_text or status), str(report_id))
            )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM suz_utilisation_reports WHERE report_id=?", (str(report_id),)
        ).fetchone()
    return _row_dict(row)


# ---------------- True API / circulation lifecycle ----------------

def create_circulation_document(
    path: str | Path,
    *,
    supply_id: str,
    document_uuid: str,
    document_type: str,
    product_group: str,
    payload: dict[str, Any],
    response: dict[str, Any],
    order_ids: Iterable[int] = (),
    submission_kind: str = "",
) -> dict[str, Any]:
    ids = sorted({int(value) for value in order_ids if int(value) > 0})
    kind = str(submission_kind or "").strip().lower()
    if kind not in {"", "introduction", "retirement", "return"}:
        raise ValueError(f"Unknown circulation submission kind: {submission_kind}")
    with _connect(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """
            INSERT INTO circulation_documents(
                supply_id, document_uuid, document_type, product_group,
                status, payload_json, response_json
            ) VALUES (?, ?, ?, ?, 'submitted', ?, ?)
            ON CONFLICT(document_uuid) DO UPDATE SET
                response_json=excluded.response_json,
                updated_at=CURRENT_TIMESTAMP
            """,
            (
                str(supply_id),
                str(document_uuid),
                str(document_type),
                str(product_group),
                json.dumps(payload, ensure_ascii=False),
                json.dumps(response, ensure_ascii=False),
            ),
        )
        if ids and kind:
            placeholders = ",".join("?" for _ in ids)
            if kind == "introduction":
                rows = conn.execute(
                    f"""SELECT id, supply_id, order_id FROM marking_codes
                        WHERE supply_id=? AND order_id IN ({placeholders})
                          AND NOT (
                            circulation_document_id=?
                            AND LOWER(COALESCE(circulation_status,''))='submitted'
                          )""",
                    [str(supply_id), *ids, str(document_uuid)],
                ).fetchall()
                conn.execute(
                    f"""UPDATE marking_codes
                        SET circulation_status='submitted', circulation_document_id=?,
                            error_text='', updated_at=CURRENT_TIMESTAMP
                        WHERE supply_id=? AND order_id IN ({placeholders})""",
                    [str(document_uuid), str(supply_id), *ids],
                )
                event_type = "circulation_submitted"
            elif kind == "retirement":
                rows = conn.execute(
                    f"""SELECT id, supply_id, order_id FROM marking_codes
                        WHERE order_id IN ({placeholders})
                          AND NOT (
                            retire_document_id=?
                            AND post_sale_status='retirement_submitted'
                          )""",
                    [*ids, str(document_uuid)],
                ).fetchall()
                conn.execute(
                    f"""UPDATE marking_codes
                        SET post_sale_status='retirement_submitted', retire_document_id=?,
                            error_text='', post_sale_updated_at=CURRENT_TIMESTAMP,
                            updated_at=CURRENT_TIMESTAMP
                        WHERE order_id IN ({placeholders})""",
                    [str(document_uuid), *ids],
                )
                event_type = "retirement_submitted"
            else:
                rows = conn.execute(
                    f"""SELECT id, supply_id, order_id FROM marking_codes
                        WHERE order_id IN ({placeholders})
                          AND NOT (
                            return_document_id=?
                            AND post_sale_status='return_submitted'
                          )""",
                    [*ids, str(document_uuid)],
                ).fetchall()
                conn.execute(
                    f"""UPDATE marking_codes
                        SET post_sale_status='return_submitted', return_document_id=?,
                            error_text='', post_sale_updated_at=CURRENT_TIMESTAMP,
                            updated_at=CURRENT_TIMESTAMP
                        WHERE order_id IN ({placeholders})""",
                    [str(document_uuid), *ids],
                )
                event_type = "return_submitted"
            for linked in rows:
                conn.execute(
                    """INSERT INTO marking_events(
                           marking_code_id,event_type,supply_id,order_id,details
                       ) VALUES(?, ?, ?, ?, ?)""",
                    (
                        int(linked["id"]), event_type,
                        str(linked["supply_id"] or ""), int(linked["order_id"]),
                        str(document_uuid),
                    ),
                )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM circulation_documents WHERE document_uuid=?",
            (str(document_uuid),),
        ).fetchone()
    return dict(row)


def list_circulation_documents(path: str | Path, supply_id: str, limit: int = 20) -> list[dict[str, Any]]:
    with _connect(path) as conn:
        rows = conn.execute(
            "SELECT * FROM circulation_documents WHERE supply_id=? ORDER BY id DESC LIMIT ?",
            (str(supply_id), int(limit)),
        ).fetchall()
    return [dict(row) for row in rows]


def get_circulation_document(path: str | Path, document_uuid: str) -> dict[str, Any] | None:
    with _connect(path) as conn:
        row = conn.execute(
            "SELECT * FROM circulation_documents WHERE document_uuid=?",
            (str(document_uuid),),
        ).fetchone()
    return _row_dict(row)


def update_circulation_document(
    path: str | Path,
    document_uuid: str,
    *,
    status: str | None = None,
    response: dict[str, Any] | None = None,
    error_text: str | None = None,
) -> dict[str, Any] | None:
    updates = ["updated_at=CURRENT_TIMESTAMP"]
    values: list[Any] = []
    if status is not None:
        updates.append("status=?")
        values.append(str(status))
    if response is not None:
        updates.append("response_json=?")
        values.append(json.dumps(response, ensure_ascii=False))
    if error_text is not None:
        updates.append("error_text=?")
        values.append(str(error_text))
    values.append(str(document_uuid))
    with _connect(path) as conn:
        conn.execute(
            f"UPDATE circulation_documents SET {', '.join(updates)} WHERE document_uuid=?",
            values,
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM circulation_documents WHERE document_uuid=?",
            (str(document_uuid),),
        ).fetchone()
    return _row_dict(row)


def update_supply_code_lifecycle(
    path: str | Path,
    *,
    supply_id: str,
    statuses_by_identification_code: dict[str, str],
    document_uuid: str | None = None,
) -> dict[str, int]:
    """Persist lifecycle states returned by True API /cises/info.

    The mapping keys are identification codes without crypto verification data.
    Full raw codes remain untouched in SQLite.
    """
    counts = {"updated": 0, "applied": 0, "introduced": 0, "errors": 0}
    normalized_map = {
        str(key or ""): str(value or "").strip().upper()
        for key, value in statuses_by_identification_code.items()
        if str(key or "")
    }
    with _connect(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(
            "SELECT * FROM marking_codes WHERE supply_id=? AND order_id IS NOT NULL",
            (str(supply_id),),
        ).fetchall()
        for row in rows:
            raw_code = str(row["raw_code"] or "")
            identification = raw_code.split("\x1d", 1)[0]
            true_status = normalized_map.get(identification)
            if not true_status:
                continue
            old_utilization_status = str(row["utilization_status"] or "")
            old_circulation_status = str(row["circulation_status"] or "")
            old_document_uuid = str(row["circulation_document_id"] or "")
            utilization_status = old_utilization_status
            circulation_status = old_circulation_status
            if true_status in {"APPLIED", "APPLIED_NOT_PAID"}:
                utilization_status = true_status
                counts["applied"] += 1
            elif true_status == "INTRODUCED":
                utilization_status = utilization_status or "APPLIED"
                circulation_status = "INTRODUCED"
                counts["introduced"] += 1
            elif true_status:
                # Keep the exact True API state for diagnostics without overwriting
                # an already confirmed INTRODUCED status.
                if circulation_status.upper() != "INTRODUCED":
                    circulation_status = true_status
            target_document_uuid = str(document_uuid or "") or old_document_uuid
            if (
                utilization_status == old_utilization_status
                and circulation_status == old_circulation_status
                and target_document_uuid == old_document_uuid
            ):
                continue
            conn.execute(
                """
                UPDATE marking_codes
                SET utilization_status=?, circulation_status=?,
                    circulation_document_id=CASE WHEN ?<>'' THEN ? ELSE circulation_document_id END,
                    updated_at=CURRENT_TIMESTAMP
                WHERE id=?
                """,
                (
                    utilization_status,
                    circulation_status,
                    str(document_uuid or ""),
                    str(document_uuid or ""),
                    int(row["id"]),
                ),
            )
            conn.execute(
                """
                INSERT INTO marking_events(marking_code_id, event_type, supply_id, order_id, details)
                VALUES (?, 'true_api_status', ?, ?, ?)
                """,
                (
                    int(row["id"]),
                    str(supply_id),
                    int(row["order_id"]),
                    json.dumps({"status": true_status, "identification_code": identification}, ensure_ascii=False),
                ),
            )
            counts["updated"] += 1
        conn.commit()
    return counts


def mark_supply_circulation_error(
    path: str | Path,
    *,
    supply_id: str,
    document_uuid: str,
    error_text: str,
) -> int:
    with _connect(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute(
            """
            UPDATE marking_codes
            SET circulation_status='ERROR', error_text=?, updated_at=CURRENT_TIMESTAMP
            WHERE supply_id=? AND circulation_document_id=? AND order_id IS NOT NULL
              AND UPPER(COALESCE(circulation_status,''))<>'INTRODUCED'
            """,
            (str(error_text), str(supply_id), str(document_uuid)),
        )
        conn.commit()
    return int(cur.rowcount or 0)


def upsert_fbs_supplies(path: str | Path, supplies: Iterable[dict[str, Any]]) -> int:
    rows = [dict(item) for item in supplies if isinstance(item, dict) and str(item.get("id") or "").strip()]
    if not rows:
        return 0
    with _connect(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        for item in rows:
            sid = str(item.get("id") or "").strip()
            if "done" in item:
                done = 1 if bool(item.get("done")) else 0
            else:
                previous = conn.execute(
                    "SELECT done FROM fbs_supply_registry WHERE supply_id=?", (sid,)
                ).fetchone()
                done = int(previous["done"] or 0) if previous is not None else 0
            conn.execute(
                """
                INSERT INTO fbs_supply_registry(
                    supply_id,name,done,created_at_wb,closed_at_wb,scan_dt_wb,status_label,raw_json,last_sync_at
                ) VALUES(?,?,?,?,?,?,?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(supply_id) DO UPDATE SET
                    name=excluded.name,
                    done=excluded.done,
                    created_at_wb=CASE WHEN excluded.created_at_wb<>'' THEN excluded.created_at_wb ELSE fbs_supply_registry.created_at_wb END,
                    closed_at_wb=CASE WHEN excluded.closed_at_wb<>'' THEN excluded.closed_at_wb ELSE fbs_supply_registry.closed_at_wb END,
                    scan_dt_wb=CASE WHEN excluded.scan_dt_wb<>'' THEN excluded.scan_dt_wb ELSE fbs_supply_registry.scan_dt_wb END,
                    status_label=CASE WHEN excluded.status_label<>'' THEN excluded.status_label ELSE fbs_supply_registry.status_label END,
                    raw_json=excluded.raw_json,
                    last_sync_at=CURRENT_TIMESTAMP
                """,
                (
                    sid,
                    str(item.get("name") or ""),
                    done,
                    str(item.get("createdAt") or ""),
                    str(item.get("closedAt") or ""),
                    str(item.get("scanDt") or item.get("scannedAt") or ""),
                    str(item.get("fbe_status") or item.get("status_label") or ""),
                    json.dumps(item, ensure_ascii=False),
                ),
            )
        conn.commit()
    return len(rows)


def set_fbs_supply_order_ids(path: str | Path, *, supply_id: str, order_ids: Iterable[int]) -> int:
    """Persist the *current* WB membership of one supply.

    GET /supplies/{id}/order-ids is authoritative for the current composition.
    Older FBE builds only added/overwrote links and never removed stale links.
    That could resurrect removed assembly orders and phantom supplies forever.

    Historical links for completed/terminal orders are intentionally preserved for
    post-sale/KIZ traceability; only non-terminal assembly links absent from the
    authoritative response are cleared.
    """
    ids = sorted({int(x) for x in order_ids if int(x) > 0})
    sid = str(supply_id or "").strip()
    if not sid:
        return 0
    id_set = set(ids)
    with _connect(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            """
            SELECT order_id, supplier_status, wb_status
            FROM fbs_order_registry
            WHERE supply_id=?
            """,
            (sid,),
        ).fetchall()

        conn.execute(
            """
            INSERT INTO fbs_supply_registry(supply_id,order_count,last_sync_at)
            VALUES(?,?,CURRENT_TIMESTAMP)
            ON CONFLICT(supply_id) DO UPDATE SET order_count=excluded.order_count,last_sync_at=CURRENT_TIMESTAMP
            """,
            (sid, len(ids)),
        )
        for oid in ids:
            conn.execute(
                """
                INSERT INTO fbs_order_registry(order_id,supply_id,last_sync_at)
                VALUES(?,?,CURRENT_TIMESTAMP)
                ON CONFLICT(order_id) DO UPDATE SET
                    supply_id=excluded.supply_id,
                    last_sync_at=CURRENT_TIMESTAMP
                """,
                (oid, sid),
            )

        # Exact reconciliation for live assembly rows. If WB says that a
        # confirm/new order is no longer in this supply, do not keep the stale
        # association. Completed/terminal rows keep the historical supply ID.
        for row in existing:
            oid = int(row["order_id"] or 0)
            if not oid or oid in id_set:
                continue
            supplier = str(row["supplier_status"] or "").strip().lower()
            wb_status = str(row["wb_status"] or "").strip().lower()
            if supplier in {"", "new", "confirm"} and wb_status not in WB_TERMINAL_STATUSES:
                conn.execute(
                    """
                    UPDATE fbs_order_registry
                    SET supply_id='', last_sync_at=CURRENT_TIMESTAMP
                    WHERE order_id=? AND supply_id=?
                    """,
                    (oid, sid),
                )
        conn.commit()
    return len(ids)


def list_fbs_supply_registry_rows(
    path: str | Path, *, limit: int = 10000, date_from: str = ""
) -> list[dict[str, Any]]:
    """Return persisted supply-level state independently from order links."""
    where = ""
    params: list[Any] = []
    date_from = str(date_from or "").strip()[:10]
    if date_from:
        where = "WHERE substr(created_at_wb,1,10)>=? OR created_at_wb=''"
        params.append(date_from)
    params.append(max(1, int(limit)))
    with _connect(path) as conn:
        rows = conn.execute(
            f"""
            SELECT supply_id,name,done,created_at_wb,closed_at_wb,scan_dt_wb,
                   status_label,order_count,raw_json,last_sync_at
            FROM fbs_supply_registry
            {where}
            ORDER BY CASE WHEN created_at_wb<>'' THEN created_at_wb ELSE last_sync_at END DESC, supply_id DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
    return [dict(row) for row in rows]


def record_fbs_sync_state(
    path: str | Path,
    *,
    sync_key: str,
    ok: bool,
    details: dict[str, Any] | None = None,
    error_text: str = "",
) -> None:
    """Persist a checkpoint for a completed WB registry synchronization."""
    with _connect(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """
            INSERT INTO fbs_sync_state(sync_key,ok,completed_at,details_json,error_text)
            VALUES(?,?,CURRENT_TIMESTAMP,?,?)
            ON CONFLICT(sync_key) DO UPDATE SET
                ok=excluded.ok,
                completed_at=excluded.completed_at,
                details_json=excluded.details_json,
                error_text=excluded.error_text
            """,
            (
                str(sync_key or "operational"),
                1 if ok else 0,
                json.dumps(details or {}, ensure_ascii=False),
                str(error_text or ""),
            ),
        )
        conn.commit()


def get_fbs_sync_state(path: str | Path, *, sync_key: str = "operational") -> dict[str, Any]:
    with _connect(path) as conn:
        row = conn.execute(
            "SELECT sync_key,ok,completed_at,details_json,error_text FROM fbs_sync_state WHERE sync_key=?",
            (str(sync_key),),
        ).fetchone()
    if row is None:
        return {"sync_key": str(sync_key), "ok": False, "completed_at": "", "details": {}, "error_text": ""}
    result = dict(row)
    try:
        result["details"] = json.loads(str(result.pop("details_json") or "{}"))
    except Exception:
        result["details"] = {}
        result.pop("details_json", None)
    result["ok"] = bool(result.get("ok"))
    return result


def get_fbs_supply_order_count(path: str | Path, supply_id: str) -> int | None:
    with _connect(path) as conn:
        row = conn.execute("SELECT order_count FROM fbs_supply_registry WHERE supply_id=?", (str(supply_id),)).fetchone()
    if row is None:
        return None
    value = int(row["order_count"] or -1)
    return value if value >= 0 else None


def upsert_fbs_orders(path: str | Path, orders: Iterable[dict[str, Any]]) -> int:
    rows = [dict(item) for item in orders if isinstance(item, dict) and int(item.get("id") or item.get("orderId") or 0) > 0]
    if not rows:
        return 0
    with _connect(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        for item in rows:
            oid = int(item.get("id") or item.get("orderId") or 0)
            supply_id = str(item.get("supplyId") or item.get("supply_id") or "").strip()
            article = str(item.get("article") or item.get("supplierArticle") or "").strip()
            try:
                warehouse_id = int(item.get("warehouseId") or item.get("warehouseID") or 0)
            except (TypeError, ValueError):
                warehouse_id = 0
            conn.execute(
                """
                INSERT INTO fbs_order_registry(
                    order_id,supply_id,seller_article,warehouse_id,created_at_wb,wb_status,supplier_status,raw_json,last_sync_at
                ) VALUES(?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)
                ON CONFLICT(order_id) DO UPDATE SET
                    supply_id=CASE WHEN excluded.supply_id<>'' THEN excluded.supply_id ELSE fbs_order_registry.supply_id END,
                    seller_article=CASE WHEN excluded.seller_article<>'' THEN excluded.seller_article ELSE fbs_order_registry.seller_article END,
                    warehouse_id=CASE WHEN excluded.warehouse_id<>0 THEN excluded.warehouse_id ELSE fbs_order_registry.warehouse_id END,
                    created_at_wb=CASE WHEN excluded.created_at_wb<>'' THEN excluded.created_at_wb ELSE fbs_order_registry.created_at_wb END,
                    wb_status=CASE WHEN excluded.wb_status<>'' THEN excluded.wb_status ELSE fbs_order_registry.wb_status END,
                    supplier_status=CASE WHEN excluded.supplier_status<>'' THEN excluded.supplier_status ELSE fbs_order_registry.supplier_status END,
                    raw_json=excluded.raw_json,
                    last_sync_at=CURRENT_TIMESTAMP
                """,
                (
                    oid, supply_id, article, warehouse_id,
                    str(item.get("createdAt") or ""),
                    str(item.get("wbStatus") or "").strip().lower(),
                    str(item.get("supplierStatus") or "").strip().lower(),
                    json.dumps(item, ensure_ascii=False),
                ),
            )
        conn.commit()
    return len(rows)


def update_fbs_order_statuses(path: str | Path, statuses: Iterable[dict[str, Any]]) -> int:
    rows = [dict(item) for item in statuses if isinstance(item, dict) and int(item.get("id") or item.get("orderId") or 0) > 0]
    if not rows:
        return 0
    with _connect(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        for item in rows:
            oid = int(item.get("id") or item.get("orderId") or 0)
            conn.execute(
                """
                INSERT INTO fbs_order_registry(order_id,wb_status,supplier_status,raw_json,last_sync_at)
                VALUES(?,?,?,?,CURRENT_TIMESTAMP)
                ON CONFLICT(order_id) DO UPDATE SET
                    wb_status=CASE WHEN excluded.wb_status<>'' THEN excluded.wb_status ELSE fbs_order_registry.wb_status END,
                    supplier_status=CASE WHEN excluded.supplier_status<>'' THEN excluded.supplier_status ELSE fbs_order_registry.supplier_status END,
                    raw_json=excluded.raw_json,
                    last_sync_at=CURRENT_TIMESTAMP
                """,
                (
                    oid,
                    str(item.get("wbStatus") or "").strip().lower(),
                    str(item.get("supplierStatus") or "").strip().lower(),
                    json.dumps(item, ensure_ascii=False),
                ),
            )
        conn.commit()
    return len(rows)


def update_fbs_order_sgtin_meta(
    path: str | Path,
    *,
    order_id: int,
    state: str,
    values: Iterable[str],
) -> None:
    """Persist what WB currently reports about SGTIN metadata for an assembly order.

    The WB metadata endpoint can report that SGTIN is filled even when a concrete
    code is not returned. Keeping that distinction in the registry prevents FBE
    from treating "not present locally" as "not present in WB".
    """
    clean_values = [str(v) for v in values if str(v)]
    preview = ""
    if clean_values:
        raw = clean_values[0].replace("\x1d", "<GS>")
        preview = raw if len(raw) <= 54 else raw[:53] + "…"
    with _connect(path) as conn:
        conn.execute(
            """
            INSERT INTO fbs_order_registry(
                order_id, wb_sgtin_state, wb_sgtin_count, wb_sgtin_preview,
                wb_sgtin_values_json, wb_meta_synced_at, last_sync_at
            ) VALUES(?,?,?,?,?,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)
            ON CONFLICT(order_id) DO UPDATE SET
                wb_sgtin_state=excluded.wb_sgtin_state,
                wb_sgtin_count=excluded.wb_sgtin_count,
                wb_sgtin_preview=excluded.wb_sgtin_preview,
                wb_sgtin_values_json=excluded.wb_sgtin_values_json,
                wb_meta_synced_at=CURRENT_TIMESTAMP,
                last_sync_at=CURRENT_TIMESTAMP
            """,
            (
                int(order_id), str(state or ""), len(clean_values), preview,
                json.dumps(clean_values, ensure_ascii=False),
            ),
        )
        conn.commit()


def recover_marking_code_from_wb(
    path: str | Path,
    *,
    order_id: int,
    raw_code: str,
    gtin: str,
    serial: str,
    seller_article: str,
    supply_id: str,
    wb_decision: str = "",
) -> dict[str, Any]:
    """Restore a missing local order→KIZ link from WB metadata.

    Existing FBE assignments are never overwritten. If WB reports a code already
    attached to another local order, the function returns a conflict instead of
    silently moving the code.
    """
    raw_code = str(raw_code or "")
    if not raw_code:
        return {"status": "empty", "order_id": int(order_id)}
    with _connect(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        existing_order = conn.execute(
            "SELECT * FROM marking_codes WHERE order_id=?", (int(order_id),)
        ).fetchone()
        existing_code = conn.execute(
            "SELECT * FROM marking_codes WHERE raw_code=?", (raw_code,)
        ).fetchone()

        if existing_order is not None:
            if str(existing_order["raw_code"] or "") != raw_code:
                conn.rollback()
                return {
                    "status": "order_conflict",
                    "order_id": int(order_id),
                    "existing_code_id": int(existing_order["id"]),
                }
            conn.execute(
                """
                UPDATE marking_codes
                SET wb_decision=CASE WHEN ?<>'' THEN ? ELSE wb_decision END,
                    wb_status=COALESCE(NULLIF(wb_status,''), ''),
                    updated_at=CURRENT_TIMESTAMP
                WHERE id=?
                """,
                (str(wb_decision or ""), str(wb_decision or ""), int(existing_order["id"])),
            )
            conn.commit()
            return {"status": "already_linked", "order_id": int(order_id), "code_id": int(existing_order["id"])}

        if existing_code is not None and existing_code["order_id"] is not None:
            conn.rollback()
            return {
                "status": "code_conflict",
                "order_id": int(order_id),
                "other_order_id": int(existing_code["order_id"]),
                "code_id": int(existing_code["id"]),
            }

        if existing_code is None:
            cur = conn.execute(
                """
                INSERT INTO marking_codes(
                    raw_code, gtin, serial, seller_article, supply_id, order_id,
                    status, wb_decision, assigned_at
                ) VALUES(?,?,?,?,?,?,'recovered_wb',?,CURRENT_TIMESTAMP)
                """,
                (
                    raw_code, str(gtin or ""), str(serial or ""),
                    str(seller_article or ""), str(supply_id or ""), int(order_id),
                    str(wb_decision or ""),
                ),
            )
            code_id = int(cur.lastrowid)
        else:
            code_id = int(existing_code["id"])
            conn.execute(
                """
                UPDATE marking_codes
                SET gtin=CASE WHEN ?<>'' THEN ? ELSE gtin END,
                    serial=CASE WHEN ?<>'' THEN ? ELSE serial END,
                    seller_article=CASE WHEN ?<>'' THEN ? ELSE seller_article END,
                    supply_id=CASE WHEN ?<>'' THEN ? ELSE supply_id END,
                    order_id=?, status='recovered_wb',
                    wb_decision=CASE WHEN ?<>'' THEN ? ELSE wb_decision END,
                    assigned_at=COALESCE(NULLIF(assigned_at,''), CURRENT_TIMESTAMP),
                    updated_at=CURRENT_TIMESTAMP
                WHERE id=? AND order_id IS NULL
                """,
                (
                    str(gtin or ""), str(gtin or ""),
                    str(serial or ""), str(serial or ""),
                    str(seller_article or ""), str(seller_article or ""),
                    str(supply_id or ""), str(supply_id or ""),
                    int(order_id), str(wb_decision or ""), str(wb_decision or ""), code_id,
                ),
            )
        conn.execute(
            """
            INSERT INTO marking_events(marking_code_id,event_type,supply_id,order_id,details)
            VALUES(?, 'recovered_from_wb', ?, ?, ?)
            """,
            (
                code_id, str(supply_id or ""), int(order_id),
                json.dumps({"source": "WB metadata", "decision": str(wb_decision or "")}, ensure_ascii=False),
            ),
        )
        conn.commit()
        return {"status": "recovered", "order_id": int(order_id), "code_id": code_id}


def update_marking_true_statuses(
    path: str | Path,
    *,
    statuses_by_identification_code: dict[str, str],
) -> dict[str, int]:
    """Apply True API statuses to any locally linked KIZ, including WB-recovered rows."""
    normalized = {
        str(k or ""): str(v or "").strip().upper()
        for k, v in statuses_by_identification_code.items()
        if str(k or "") and str(v or "").strip()
    }
    counts = {"updated": 0, "introduced": 0, "retired": 0}
    if not normalized:
        return counts
    with _connect(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(
            "SELECT * FROM marking_codes WHERE order_id IS NOT NULL AND raw_code<>''"
        ).fetchall()
        for row in rows:
            raw = str(row["raw_code"] or "")
            identification = raw.split("\x1d", 1)[0]
            status = normalized.get(identification)
            if not status:
                continue
            old_utilization = str(row["utilization_status"] or "")
            old_circulation = str(row["circulation_status"] or "")
            utilization = old_utilization
            circulation = old_circulation
            if status in {"APPLIED", "APPLIED_NOT_PAID"}:
                utilization = status
            elif status == "INTRODUCED":
                utilization = utilization or "APPLIED"
                circulation = "INTRODUCED"
                counts["introduced"] += 1
            elif status.startswith("RETIRED") or status in {"RETIRED", "WITHDRAWN"}:
                circulation = status
                counts["retired"] += 1
            elif circulation.upper() != "INTRODUCED":
                circulation = status
            if utilization == old_utilization and circulation == old_circulation:
                continue
            conn.execute(
                """
                UPDATE marking_codes
                SET utilization_status=?, circulation_status=?, updated_at=CURRENT_TIMESTAMP
                WHERE id=?
                """,
                (utilization, circulation, int(row["id"])),
            )
            conn.execute(
                """
                INSERT INTO marking_events(marking_code_id,event_type,supply_id,order_id,details)
                VALUES(?, 'true_api_status', ?, ?, ?)
                """,
                (
                    int(row["id"]), str(row["supply_id"] or ""), int(row["order_id"]),
                    json.dumps({"status": status, "identification_code": identification, "source": "post_sale_reconcile"}, ensure_ascii=False),
                ),
            )
            counts["updated"] += 1
        conn.commit()
    return counts


def list_fbs_lifecycle_rows(
    path: str | Path, *, limit: int = 5000, query: str = "", date_from: str = ""
) -> list[dict[str, Any]]:
    q = str(query or "").strip()
    date_from = str(date_from or "").strip()
    clauses: list[str] = []
    params: list[Any] = []
    if q:
        like = f"%{q}%"
        clauses.append("(CAST(fo.order_id AS TEXT) LIKE ? OR fo.seller_article LIKE ? OR fo.supply_id LIKE ? OR fs.name LIKE ? OR mc.raw_code LIKE ? OR mc.gtin LIKE ?)")
        params.extend([like, like, like, like, like, like])
    if date_from:
        # WB timestamps are ISO strings. YYYY-MM-DD lexicographic comparison is
        # stable for the UTC ISO values stored in created_at_wb.
        clauses.append("substr(COALESCE(fo.created_at_wb,''),1,10) >= ?")
        params.append(date_from[:10])
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    params.append(int(limit))
    with _connect(path) as conn:
        rows = conn.execute(
            f"""
            SELECT
                fo.order_id,
                fo.supply_id,
                fo.seller_article AS registry_article,
                fo.warehouse_id,
                fo.created_at_wb,
                fo.wb_status AS registry_wb_status,
                fo.supplier_status,
                fo.wb_sgtin_state,
                fo.wb_sgtin_count,
                fo.wb_sgtin_preview,
                fo.wb_sgtin_values_json,
                fo.wb_meta_synced_at,
                fo.last_sync_at,
                fs.name AS supply_name,
                fs.status_label AS supply_status_label,
                fs.scan_dt_wb,
                fs.closed_at_wb,
                mc.id AS marking_code_id,
                mc.raw_code,
                mc.gtin,
                mc.seller_article AS marking_article,
                mc.status AS marking_status,
                mc.wb_sent_at,
                mc.circulation_status,
                mc.wb_status AS marking_wb_status,
                mc.post_sale_status,
                mc.post_sale_updated_at,
                mc.retire_document_id,
                mc.retired_at,
                mc.return_document_id,
                mc.returned_at
            FROM fbs_order_registry fo
            LEFT JOIN fbs_supply_registry fs ON fs.supply_id=fo.supply_id
            LEFT JOIN marking_codes mc ON mc.order_id=fo.order_id
            {where}
            ORDER BY fo.last_sync_at DESC, fo.order_id DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
    result=[]
    for row in rows:
        item=dict(row)
        item["seller_article"] = str(item.get("marking_article") or item.get("registry_article") or "")
        item["wb_status"] = str(item.get("registry_wb_status") or item.get("marking_wb_status") or "").lower()
        result.append(item)
    return result


def list_fbs_registry_rows(path: str | Path, *, limit: int = 10000, date_from: str = "") -> list[dict[str, Any]]:
    date_from = str(date_from or "").strip()
    where = ""
    params: list[Any] = []
    if date_from:
        where = "WHERE substr(COALESCE(fo.created_at_wb,''),1,10) >= ?"
        params.append(date_from[:10])
    params.append(int(limit))
    with _connect(path) as conn:
        rows = conn.execute(
            f"""
            SELECT fo.*, fs.name AS supply_name, fs.created_at_wb AS supply_created_at_wb,
                   fs.closed_at_wb, fs.scan_dt_wb, fs.done AS supply_done
            FROM fbs_order_registry fo
            LEFT JOIN fbs_supply_registry fs ON fs.supply_id=fo.supply_id
            {where}
            ORDER BY fo.created_at_wb DESC, fo.order_id DESC
            LIMIT ?
            """, params
        ).fetchall()
    return [dict(row) for row in rows]


def ensure_fbs_registry_from_marking_codes(path: str | Path) -> int:
    with _connect(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(
            "SELECT order_id,supply_id,seller_article,wb_status FROM marking_codes WHERE order_id IS NOT NULL"
        ).fetchall()
        for row in rows:
            conn.execute(
                """
                INSERT INTO fbs_order_registry(order_id,supply_id,seller_article,wb_status,last_sync_at)
                VALUES(?,?,?,?,CURRENT_TIMESTAMP)
                ON CONFLICT(order_id) DO UPDATE SET
                    supply_id=CASE WHEN fbs_order_registry.supply_id='' THEN excluded.supply_id ELSE fbs_order_registry.supply_id END,
                    seller_article=CASE WHEN fbs_order_registry.seller_article='' THEN excluded.seller_article ELSE fbs_order_registry.seller_article END,
                    wb_status=CASE WHEN fbs_order_registry.wb_status='' THEN excluded.wb_status ELSE fbs_order_registry.wb_status END
                """,
                (int(row["order_id"]), str(row["supply_id"] or ""), str(row["seller_article"] or ""), str(row["wb_status"] or "")),
            )
        conn.commit()
    return len(rows)


def list_post_sale_assignments(path: str | Path, *, limit: int = 1000) -> list[dict[str, Any]]:
    with _connect(path) as conn:
        rows = conn.execute(
            """
            SELECT * FROM marking_codes
            WHERE order_id IS NOT NULL
              AND UPPER(COALESCE(circulation_status,'')) IN ('INTRODUCED','RETIRED')
            ORDER BY updated_at DESC, id DESC
            LIMIT ?
            """,
            (int(limit),),
        ).fetchall()
    return [dict(row) for row in rows]


def update_post_sale_wb_statuses(
    path: str | Path, statuses: Iterable[dict[str, Any]]
) -> int:
    """Apply one WB status batch in one transaction and log real changes only.

    Operational synchronisation can contain thousands of rows. The old per-row
    connection/transaction multiplied lock contention and appended an identical
    audit event on every poll. Submitted legal operations are also monotonic: a
    later repeated ``sold`` status must never make them eligible for resubmission.
    """
    normalized: dict[int, str] = {}
    for item in statuses:
        try:
            order_id = int(item.get("id") or item.get("orderId") or 0)
        except (TypeError, ValueError):
            continue
        wb_status = str(item.get("wbStatus") or item.get("wb_status") or "").strip().lower()
        if order_id > 0 and wb_status:
            normalized[order_id] = wb_status
    if not normalized:
        return 0

    placeholders = ",".join("?" for _ in normalized)
    changed = 0
    protected = {"retirement_submitted", "retired", "return_submitted", "returned"}
    with _connect(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(
            f"SELECT id, order_id, supply_id, wb_status, post_sale_status "
            f"FROM marking_codes WHERE order_id IN ({placeholders})",
            list(normalized),
        ).fetchall()
        for row in rows:
            order_id = int(row["order_id"])
            wb_status = normalized[order_id]
            current_post_sale = str(row["post_sale_status"] or "")
            mapped = {
                "waiting": "at_wb",
                "sorted": "at_wb",
                "sold": "ready_to_retire",
                "canceled": "canceled",
            }.get(wb_status, "at_wb")
            if current_post_sale in protected:
                mapped = current_post_sale
            if str(row["wb_status"] or "").lower() == wb_status and current_post_sale == mapped:
                continue
            conn.execute(
                """UPDATE marking_codes
                   SET wb_status=?, post_sale_status=?,
                       post_sale_updated_at=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP
                   WHERE id=?""",
                (wb_status, mapped, int(row["id"])),
            )
            conn.execute(
                """INSERT INTO marking_events(
                       marking_code_id,event_type,supply_id,order_id,details
                   ) VALUES(?, 'wb_post_sale_status', ?, ?, ?)""",
                (
                    int(row["id"]), str(row["supply_id"] or ""), order_id,
                    json.dumps(
                        {"wbStatus": wb_status, "postSaleStatus": mapped},
                        ensure_ascii=False,
                    ),
                ),
            )
            changed += 1
        conn.commit()
    return changed


def mark_post_sale_document_failed(
    path: str | Path,
    *,
    document_uuid: str,
    document_type: str,
    error_text: str,
) -> int:
    """Reopen only the codes linked to a rejected post-sale document.

    A True API document ID proves that submission happened, so it remains in
    the audit tables. Once that document reaches a terminal failure, however,
    its linked codes must become eligible for a new document. Other product
    groups and already successful codes are left untouched.
    """
    kind = str(document_type or "").strip().upper()
    if kind == "LK_RECEIPT":
        link_field = "retire_document_id"
        expected_status = "retirement_submitted"
        retry_status = "ready_to_retire"
    elif kind == "LP_RETURN":
        link_field = "return_document_id"
        expected_status = "return_submitted"
        retry_status = "retired"
    else:
        return 0

    with _connect(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(
            f"""SELECT id, supply_id, order_id FROM marking_codes
                WHERE {link_field}=? AND post_sale_status=?""",
            (str(document_uuid), expected_status),
        ).fetchall()
        if rows:
            conn.execute(
                f"""UPDATE marking_codes
                    SET post_sale_status=?, error_text=?,
                        post_sale_updated_at=CURRENT_TIMESTAMP,
                        updated_at=CURRENT_TIMESTAMP
                    WHERE {link_field}=? AND post_sale_status=?""",
                (retry_status, str(error_text), str(document_uuid), expected_status),
            )
            details = json.dumps(
                {
                    "document_uuid": str(document_uuid),
                    "document_type": kind,
                    "error": str(error_text),
                    "retry_status": retry_status,
                },
                ensure_ascii=False,
            )
            for row in rows:
                conn.execute(
                    """INSERT INTO marking_events(
                           marking_code_id,event_type,supply_id,order_id,details
                       ) VALUES(?, 'post_sale_document_failed', ?, ?, ?)""",
                    (
                        int(row["id"]), str(row["supply_id"] or ""),
                        int(row["order_id"]), details,
                    ),
                )
        conn.commit()
    return len(rows)


def update_post_sale_true_status(path: str | Path, *, statuses_by_identification_code: dict[str,str]) -> dict[str,int]:
    norm={str(k):str(v or '').strip().upper() for k,v in statuses_by_identification_code.items() if str(k)}
    counts={'retired':0,'returned':0,'updated':0}
    with _connect(path) as conn:
        rows=conn.execute("SELECT * FROM marking_codes WHERE order_id IS NOT NULL AND post_sale_status<>''").fetchall()
        for row in rows:
            ident=str(row['raw_code'] or '').split('\x1d',1)[0]
            st=norm.get(ident)
            if not st: continue
            post=str(row['post_sale_status'] or '')
            if (st.startswith('RETIRED') or st in {'RETIRED', 'WITHDRAWN'}) and post == 'retirement_submitted':
                post='retired'; counts['retired']+=1
                conn.execute("UPDATE marking_codes SET circulation_status='RETIRED', post_sale_status=?, retired_at=CASE WHEN retired_at='' THEN CURRENT_TIMESTAMP ELSE retired_at END, post_sale_updated_at=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP WHERE id=?",(post,int(row['id'])))
            elif st=='INTRODUCED' and post == 'return_submitted':
                post='returned'; counts['returned']+=1
                conn.execute("UPDATE marking_codes SET circulation_status='INTRODUCED', post_sale_status=?, returned_at=CASE WHEN returned_at='' THEN CURRENT_TIMESTAMP ELSE returned_at END, post_sale_updated_at=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP WHERE id=?",(post,int(row['id'])))
            else:
                continue
            counts['updated']+=1
        conn.commit()
    return counts
