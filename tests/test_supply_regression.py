from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app as fbe
import marking_db


def recent_wb_time(minutes_ago: int = 0) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat().replace("+00:00", "Z")


@pytest.fixture()
def temp_db(tmp_path, monkeypatch):
    db = marking_db.ensure_database(tmp_path / "fbe-test.db")
    monkeypatch.setattr(fbe, "marking_db_path", db)
    return db


def test_exact_membership_clears_stale_live_link_but_preserves_history(temp_db):
    marking_db.upsert_fbs_orders(temp_db, [
        {"id": 1, "supplyId": "S1", "supplierStatus": "confirm", "wbStatus": "waiting"},
        {"id": 2, "supplyId": "S1", "supplierStatus": "confirm", "wbStatus": "waiting"},
        {"id": 3, "supplyId": "S1", "supplierStatus": "complete", "wbStatus": "sold"},
    ])

    marking_db.set_fbs_supply_order_ids(temp_db, supply_id="S1", order_ids=[1])
    rows = {r["order_id"]: r for r in marking_db.list_fbs_registry_rows(temp_db)}

    assert rows[1]["supply_id"] == "S1"
    assert rows[2]["supply_id"] == ""  # removed live assembly order must not linger
    assert rows[3]["supply_id"] == "S1"  # historical traceability is preserved
    assert marking_db.get_fbs_supply_order_count(temp_db, "S1") == 1


def test_open_empty_supply_is_visible_without_order_status_rows():
    groups = fbe._fbs_dashboard_supply_groups([], [{
        "supply_id": "S-EMPTY",
        "name": "Новая поставка",
        "done": 0,
        "created_at_wb": "2026-08-18T08:00:00Z",
        "closed_at_wb": "",
        "scan_dt_wb": "",
        "order_count": 0,
    }])
    assert [s["id"] for s in groups["assembling"]] == ["S-EMPTY"]
    assert groups["assembling"][0]["order_count"] == 0


def test_done_supply_vetoes_stale_confirm_row():
    rows = [{
        "order_id": 101,
        "supply_id": "S-DONE",
        "supply_name": "Старая",
        "supply_created_at_wb": "2026-08-18T08:00:00Z",
        "created_at_wb": "2026-08-18T08:01:00Z",
        "supplier_status": "confirm",
        "wb_status": "waiting",
    }]
    supplies = [{
        "supply_id": "S-DONE",
        "name": "Старая",
        "done": 1,
        "created_at_wb": "2026-08-18T08:00:00Z",
        "order_count": 1,
    }]
    groups = fbe._fbs_dashboard_supply_groups(rows, supplies)
    assert groups["assembling"] == []


def test_add_batch_reconciles_full_target_membership(temp_db, monkeypatch):
    marking_db.upsert_fbs_orders(temp_db, [
        {"id": 10, "supplyId": "TARGET", "supplierStatus": "confirm", "wbStatus": "waiting"},
    ])

    class FakeWB:
        def add_orders_to_supply(self, supply_id, order_ids):
            assert supply_id == "TARGET"
            assert order_ids == [11]

        def get_supply_order_ids(self, supply_id):
            return [10, 11]

    monkeypatch.setattr(fbe, "wb", FakeWB())
    assert fbe.add_orders_to_supply_in_batches("TARGET", [11]) == 1
    rows = {r["order_id"]: r for r in marking_db.list_fbs_registry_rows(temp_db)}
    assert rows[10]["supply_id"] == "TARGET"
    assert rows[11]["supply_id"] == "TARGET"
    assert marking_db.get_fbs_supply_order_count(temp_db, "TARGET") == 2


def test_operational_sync_covers_every_open_supply_and_records_checkpoint(temp_db, monkeypatch):
    open_supplies = [
        {"id": f"OPEN-{i}", "name": f"Open {i}", "done": False, "createdAt": "2026-08-18T08:00:00Z"}
        for i in range(15)
    ]
    closed_supplies = [
        {"id": f"CLOSED-{i}", "name": f"Closed {i}", "done": True, "createdAt": "2026-08-17T08:00:00Z", "closedAt": "2026-08-17T09:00:00Z"}
        for i in range(20)
    ]

    class FakeWB:
        def __init__(self):
            self.membership_calls = []

        def get_all_supplies(self, max_pages=1):
            return open_supplies + closed_supplies

        def get_supply_order_ids(self, supply_id):
            self.membership_calls.append(supply_id)
            if supply_id.startswith("OPEN-"):
                return [1000 + int(supply_id.split("-")[1])]
            return []

        def get_orders_last_days(self, days=7):
            return [
                {
                    "id": 1000 + i,
                    "supplyId": f"OPEN-{i}",
                    "createdAt": "2026-08-18T08:01:00Z",
                    "supplierStatus": "confirm",
                    "wbStatus": "waiting",
                }
                for i in range(15)
            ]

        def get_order_statuses(self, order_ids):
            return [
                {"id": oid, "supplierStatus": "confirm", "wbStatus": "waiting"}
                for oid in order_ids
            ]

    fake = FakeWB()
    monkeypatch.setattr(fbe, "wb", fake)
    monkeypatch.setitem(fbe.config, "fbs_operational_supply_sync_limit", 8)
    monkeypatch.setitem(fbe.config, "fbs_operational_history_days", 7)

    stats = fbe._sync_fbs_lifecycle_registry(include_supplies=True, deep=False)

    # Every open supply is authoritative and must be reconciled even if the old
    # config says 8. A bounded tail of recently closed supplies is allowed too.
    for row in open_supplies:
        assert row["id"] in fake.membership_calls
    closed_calls = [sid for sid in fake.membership_calls if sid.startswith("CLOSED-")]
    assert len(closed_calls) <= 8  # historical membership reads stay bounded
    assert stats["core"] == {
        "supplies_ok": True,
        "membership_ok": True,
        "orders_ok": True,
        "statuses_ok": True,
    }

    checkpoint = marking_db.get_fbs_sync_state(temp_db, sync_key="operational")
    assert checkpoint["ok"] is True
    assert checkpoint["completed_at"]


def test_failed_core_sync_does_not_mark_registry_fresh(temp_db, monkeypatch):
    class FakeWB:
        def get_all_supplies(self, max_pages=1):
            raise RuntimeError("WB unavailable")

        def get_orders_last_days(self, days=7):
            return []

        def get_order_statuses(self, order_ids):
            return []

    monkeypatch.setattr(fbe, "wb", FakeWB())
    fbe._sync_fbs_lifecycle_registry(include_supplies=True, deep=False)
    checkpoint = marking_db.get_fbs_sync_state(temp_db, sync_key="operational")
    assert checkpoint["ok"] is False
    assert "Поставки WB" in checkpoint["error_text"]


def test_successful_deliver_updates_local_operational_state_immediately(temp_db, tmp_path, monkeypatch):
    sid = "DELIVER-1"
    created = recent_wb_time(5)
    marking_db.upsert_fbs_supplies(temp_db, [{
        "id": sid, "name": "Поставка", "done": False, "createdAt": created
    }])
    marking_db.upsert_fbs_orders(temp_db, [
        {"id": 501, "supplyId": sid, "createdAt": created, "supplierStatus": "confirm", "wbStatus": "waiting"},
        {"id": 502, "supplyId": sid, "createdAt": created, "supplierStatus": "confirm", "wbStatus": "waiting"},
    ])
    marking_db.set_fbs_supply_order_ids(temp_db, supply_id=sid, order_ids=[501, 502])

    archive = tmp_path / sid
    archive.mkdir(parents=True)
    (archive / "manifest.json").write_text(json.dumps({
        "units": [{"trbx_id": "TRBX-1"}]
    }), encoding="utf-8")

    class FakeWB:
        def get_supply_shipping_units(self, supply_id):
            return ["TRBX-1"]

        def deliver_supply(self, supply_id):
            assert supply_id == sid

        def get_supply_order_ids(self, supply_id):
            return [501, 502]

    monkeypatch.setattr(fbe, "wb", FakeWB())
    monkeypatch.setattr(fbe, "_shipping_archive_dir", lambda supply_id: archive)
    monkeypatch.setattr(fbe, "_supply_transfer_preflight_one", lambda supply_id: {
        "supply_id": sid,
        "name": "Поставка",
        "ready": True,
        "order_count": 2,
        "order_ids": [501, 502],
        "blockers": [],
    })

    result = fbe.transfer_supplies_deliver(fbe.SupplyTransferDeliverPayload(
        supply_ids=[sid], confirmed_labels_applied=True
    ))
    assert result["all_ok"] is True

    supply = {r["supply_id"]: r for r in marking_db.list_fbs_supply_registry_rows(temp_db)}[sid]
    rows = [r for r in marking_db.list_fbs_registry_rows(temp_db) if r["supply_id"] == sid]
    assert supply["done"] == 1
    assert {r["supplier_status"] for r in rows} == {"complete"}

    groups = fbe._fbs_dashboard_supply_groups(rows, [supply])
    assert groups["assembling"] == []
    assert [s["id"] for s in groups["handed"]] == [sid]


def test_local_supply_snapshot_keeps_empty_supply(temp_db):
    marking_db.upsert_fbs_supplies(temp_db, [{
        "id": "EMPTY-LOCAL",
        "name": "Пустая",
        "done": False,
        "createdAt": "2026-08-18T09:00:00Z",
    }])
    rows = {row["id"]: row for row in fbe._local_supply_snapshot()}
    assert rows["EMPTY-LOCAL"]["name"] == "Пустая"
    assert rows["EMPTY-LOCAL"]["done"] is False
    assert rows["EMPTY-LOCAL"]["ordersCount"] == 0


def test_closed_supply_count_falls_back_to_historical_rows():
    created = recent_wb_time(5)
    rows = [
        {
            "order_id": 601,
            "supply_id": "CLOSED-HISTORY",
            "supplier_status": "complete",
            "wb_status": "sorted",
            "created_at_wb": created,
        },
        {
            "order_id": 602,
            "supply_id": "CLOSED-HISTORY",
            "supplier_status": "complete",
            "wb_status": "sorted",
            "created_at_wb": created,
        },
    ]
    supplies = [{
        "supply_id": "CLOSED-HISTORY",
        "name": "Передана",
        "done": 1,
        "created_at_wb": created,
        "order_count": 0,
    }]
    groups = fbe._fbs_dashboard_supply_groups(rows, supplies)
    assert groups["handed"][0]["order_count"] == 2


def test_cold_dashboard_never_flashes_stale_supply_and_self_refreshes(temp_db, monkeypatch):
    import time
    from fastapi.testclient import TestClient

    marking_db.upsert_fbs_supplies(temp_db, [{
        "id": "STALE-OLD",
        "name": "Старая поставка",
        "done": False,
        "createdAt": "2026-08-18T07:00:00Z",
    }])
    marking_db.upsert_fbs_orders(temp_db, [{
        "id": 700,
        "supplyId": "STALE-OLD",
        "createdAt": "2026-08-18T07:01:00Z",
        "supplierStatus": "confirm",
        "wbStatus": "waiting",
    }])

    class FakeWB:
        def get_new_orders(self):
            return []

        def get_all_supplies(self, max_pages=1):
            time.sleep(0.12)
            return [{
                "id": "CURRENT-NEW",
                "name": "Текущая поставка",
                "done": False,
                "createdAt": "2026-08-18T10:00:00Z",
            }]

        def get_supply_order_ids(self, supply_id):
            return [701] if supply_id == "CURRENT-NEW" else []

        def get_orders_last_days(self, days=7):
            return [{
                "id": 701,
                "supplyId": "CURRENT-NEW",
                "createdAt": "2026-08-18T10:01:00Z",
                "supplierStatus": "confirm",
                "wbStatus": "waiting",
            }]

        def get_order_statuses(self, order_ids):
            return [{"id": oid, "supplierStatus": "confirm", "wbStatus": "waiting"} for oid in order_ids]

    monkeypatch.setattr(fbe, "wb", FakeWB())
    with fbe._cache_lock:
        fbe._cache.clear()
        fbe._cache_refreshing.clear()

    client = TestClient(fbe.app)
    first = client.get("/")
    assert first.status_code == 200
    assert "Старая поставка" not in first.text
    assert "Получаю актуальное состояние поставок WB" in first.text

    deadline = time.time() + 2.0
    state = None
    while time.time() < deadline:
        state = client.get("/api/fbs/operational-state").json()
        if state.get("fresh"):
            break
        time.sleep(0.03)
    assert state and state.get("fresh") is True

    second = client.get("/")
    assert second.status_code == 200
    assert "Текущая поставка" in second.text
    assert "Старая поставка" not in second.text


def test_partial_supply_upsert_does_not_reopen_closed_supply(temp_db):
    marking_db.upsert_fbs_supplies(temp_db, [{
        "id": "CLOSED-PARTIAL", "name": "Закрыта", "done": True,
        "createdAt": "2026-08-18T08:00:00Z",
    }])
    marking_db.upsert_fbs_supplies(temp_db, [{
        "id": "CLOSED-PARTIAL", "name": "Новое имя",
    }])
    row = {r["supply_id"]: r for r in marking_db.list_fbs_supply_registry_rows(temp_db)}["CLOSED-PARTIAL"]
    assert row["done"] == 1
    assert row["name"] == "Новое имя"


def test_handed_tab_does_not_resurrect_unverified_old_complete_rows(monkeypatch):
    monkeypatch.setitem(fbe.config, "fbs_operational_history_days", 7)
    rows = [{
        "order_id": 801,
        "supply_id": "OLD-COMPLETE",
        "supplier_status": "complete",
        "wb_status": "sorted",
        "created_at_wb": "2026-07-01T08:00:00Z",
    }]
    supplies = [{
        "supply_id": "OLD-COMPLETE",
        "name": "Историческая",
        "done": 1,
        "created_at_wb": "2026-07-01T08:00:00Z",
        "order_count": 1,
    }]
    groups = fbe._fbs_dashboard_supply_groups(rows, supplies)
    assert groups["handed"] == []


def test_bulk_wb_kiz_worker_exposes_partial_result_without_blocking_route(monkeypatch):
    reports = []
    monkeypatch.setattr(fbe, "marking_send_all_to_wb", lambda supply_id: {
        "ok": False,
        "message": "Передано в WB: 2. Ошибки: 1",
        "sent": 2,
        "skipped": 0,
        "errors": ["3: temporary"],
    })
    result = fbe._marking_send_all_to_wb_worker("S1", lambda **row: reports.append(row))
    assert result["sent"] == 2
    assert result["errors"] == ["3: temporary"]
    assert reports[0]["phase"] == "preflight"
    assert reports[-1]["phase"] == "partial"


def test_bulk_wb_kiz_worker_raises_on_total_failure(monkeypatch):
    monkeypatch.setattr(fbe, "marking_send_all_to_wb", lambda supply_id: {
        "ok": False,
        "message": "WB недоступен",
        "sent": 0,
        "skipped": 0,
        "errors": ["connection"],
    })
    with pytest.raises(marking_db.MarkingDbError, match="WB недоступен"):
        fbe._marking_send_all_to_wb_worker("S1", lambda **row: None)


def test_mixed_supply_report_gate_is_scoped_to_linked_chemistry_codes(monkeypatch):
    monkeypatch.setattr(
        fbe,
        "_assignment_product_group",
        lambda row: str(row.get("product_group") or ""),
    )
    report = {"report_id": "R-CHEM-1", "status": "SUBMITTED"}
    rows = [
        {
            "order_id": 1,
            "product_group": "chemistry",
            "utilization_report_id": "R-CHEM-1",
            "utilization_status": "SUBMITTED",
            "circulation_status": "",
        },
        # Perfume codes belong to another lifecycle. They must never be counted
        # as outstanding responses for the chemistry application report.
        *[
            {
                "order_id": 100 + i,
                "product_group": "perfumery",
                "utilization_report_id": "",
                "utilization_status": "",
                "circulation_status": "",
            }
            for i in range(6)
        ],
    ]
    assert fbe._utilisation_report_blocks_application(report, rows) is True

    rows[0]["utilization_status"] = "APPLIED"
    assert fbe._utilisation_report_blocks_application(report, rows) is False


def test_application_worker_releases_ui_after_report_submission(monkeypatch):
    payload = fbe.CirculationIntroducePayload(
        confirmed=True,
        participant_inn="0000000000",
        production_date="2026-08-18",
    )
    monkeypatch.setattr(
        fbe,
        "_marking_lifecycle_payload_call",
        lambda supply_id, payload: {
            "ok": True,
            "stage": "utilisation",
            "report_ids": ["REPORT-1"],
            "codes": 1,
        },
    )

    # Regression guard: this worker must not poll CRPT itself anymore. Polling is
    # owned by the independent live watcher so the browser is usable immediately.
    monkeypatch.setattr(
        fbe,
        "_refresh_circulation_status_impl",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("worker must not poll")),
    )
    events = []
    result = fbe._application_report_worker("MIXED-7", payload, lambda **kw: events.append(kw))

    assert result["ok"] is True
    assert result["processing"] is True
    assert result["report_ids"] == ["REPORT-1"]
    assert events[-1]["phase"] == "submitted"
    assert events[-1]["progress"] == 100


def test_utilisation_report_parser_handles_list_wrapped_response():
    payload = {"result": [{"reportStatus": "SUCCESS", "errorReason": ""}]}
    assert fbe._utilisation_report_status(payload) == "SUCCESS"
    assert fbe._utilisation_report_error(payload) == ""

    error_payload = {"data": [{"status": "REJECTED", "errorReason": "bad code"}]}
    assert fbe._utilisation_report_status(error_payload) == "REJECTED"
    assert fbe._utilisation_report_error(error_payload) == "bad code"


def test_true_status_refresh_commits_successful_group_when_other_group_fails(monkeypatch):
    rows = [
        {"raw_code": "CHEM1", "seller_article": "D", "product_group": "chemistry"},
        {"raw_code": "PERF1", "seller_article": "P", "product_group": "perfumery"},
    ]
    monkeypatch.setattr(fbe, "list_supply_code_assignments", lambda path, supply_id: rows)
    monkeypatch.setattr(fbe, "_assignment_product_group", lambda row: row["product_group"])

    class FakeSuz:
        def get_cises_info(self, codes, product_group):
            if product_group == "perfumery":
                raise RuntimeError("temporary perfumery disconnect")
            return [{"requestedCis": "CHEM1", "status": "APPLIED"}]

    monkeypatch.setattr(fbe, "suz", FakeSuz())
    committed = []
    monkeypatch.setattr(
        fbe,
        "update_supply_code_lifecycle",
        lambda path, supply_id, statuses_by_identification_code: (
            committed.append(dict(statuses_by_identification_code))
            or {"updated": len(statuses_by_identification_code), "applied": 1, "introduced": 0, "errors": 0}
        ),
    )

    result = fbe._refresh_supply_true_statuses("MIXED")
    assert committed == [{"CHEM1": "APPLIED"}]
    assert result["statuses"] == {"CHEM1": "APPLIED"}
    assert any("perfumery" in error for error in result["errors"])
