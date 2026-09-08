from __future__ import annotations

import json
import http.client
import sqlite3
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app as fbe
import marking_db
import print_queue
from printers.bartender_label import print_internal_label
from economy.wb_client import EconomyWBClient
from settings import Settings
from suz_client import SuzApiError, SuzClient
from wb_api import WBApiError, WBClient


@pytest.fixture()
def temp_db(tmp_path, monkeypatch):
    path = marking_db.ensure_database(tmp_path / "reliability.db")
    monkeypatch.setattr(fbe, "marking_db_path", path)
    return path


def add_code(
    path: Path,
    *,
    order_id: int,
    raw_code: str,
    supply_id: str = "S1",
    post_sale_status: str = "",
    utilization_status: str = "",
    circulation_status: str = "",
    utilization_report_id: str = "",
) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute(
            """INSERT INTO marking_codes(
                   raw_code,gtin,serial,seller_article,supply_id,order_id,status,
                   post_sale_status,utilization_status,circulation_status,utilization_report_id
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (
                raw_code, "04600000000001", str(order_id), f"A-{order_id}", supply_id,
                order_id, "assigned_local", post_sale_status, utilization_status,
                circulation_status, utilization_report_id,
            ),
        )


def event_count(path: Path, event_type: str) -> int:
    with sqlite3.connect(path) as connection:
        return int(connection.execute(
            "SELECT COUNT(*) FROM marking_events WHERE event_type=?", (event_type,)
        ).fetchone()[0])


def test_wb_status_batch_is_idempotent_and_preserves_submitted_operation(temp_db):
    add_code(temp_db, order_id=1, raw_code="CODE-1", post_sale_status="retirement_submitted")
    rows = [{"id": 1, "wbStatus": "sold"}]

    assert marking_db.update_post_sale_wb_statuses(temp_db, rows) == 1
    assert marking_db.update_post_sale_wb_statuses(temp_db, rows) == 0

    row = marking_db.get_assignment(temp_db, 1)
    assert row["post_sale_status"] == "retirement_submitted"
    assert event_count(temp_db, "wb_post_sale_status") == 1


def test_true_status_poll_does_not_append_duplicate_events(temp_db):
    add_code(temp_db, order_id=2, raw_code="IDENT-2\x1dCRYPTO")
    statuses = {"IDENT-2": "INTRODUCED"}

    first = marking_db.update_supply_code_lifecycle(
        temp_db, supply_id="S1", statuses_by_identification_code=statuses
    )
    second = marking_db.update_supply_code_lifecycle(
        temp_db, supply_id="S1", statuses_by_identification_code=statuses
    )

    assert first["updated"] == 1
    assert second["updated"] == 0
    assert event_count(temp_db, "true_api_status") == 1


def test_terminal_cis_states_are_not_polled_or_reintroduced(temp_db, monkeypatch):
    add_code(temp_db, order_id=3, raw_code="INTRO", circulation_status="INTRODUCED")
    add_code(temp_db, order_id=4, raw_code="RETIRED", circulation_status="RETIRED")
    add_code(temp_db, order_id=6, raw_code="REJECTED", circulation_status="REJECTED")
    add_code(temp_db, order_id=5, raw_code="PENDING", circulation_status="APPLIED")
    calls = []

    class FakeSuz:
        def get_cises_info(self, codes, *, product_group):
            calls.append((list(codes), product_group))
            return [{"requestedCis": code, "status": "APPLIED"} for code in codes]

    monkeypatch.setattr(fbe, "suz", FakeSuz())
    monkeypatch.setattr(fbe, "_assignment_product_group", lambda row: "chemistry")
    result = fbe._refresh_supply_true_statuses("S1")

    assert calls == [(["PENDING"], "chemistry")]
    assert result["unresolved"] == 0
    assert fbe._circulation_code_terminal("REJECTED")
    assert not fbe._circulation_code_success_terminal("REJECTED")
    assert fbe._circulation_code_success_terminal("RETIRED")

    with sqlite3.connect(temp_db) as connection:
        connection.execute("UPDATE marking_codes SET supply_id='TERMINAL' WHERE order_id IN (3, 4, 6)")
        connection.execute("UPDATE marking_codes SET supply_id='ACTIVE' WHERE order_id=5")
    assert fbe._marking_supply_refresh_needed("TERMINAL") == {"suz": False, "true": False}


def test_partial_utilisation_keeps_successful_code_and_reopens_only_failed_code(temp_db):
    add_code(
        temp_db, order_id=10, raw_code="OK", utilization_status="APPLIED",
        utilization_report_id="REPORT-PART",
    )
    add_code(
        temp_db, order_id=11, raw_code="BAD", utilization_status="SUBMITTED",
        utilization_report_id="REPORT-PART",
    )
    with sqlite3.connect(temp_db) as connection:
        connection.execute(
            """INSERT INTO suz_utilisation_reports(
                   supply_id,report_id,product_group,gtin,status
               ) VALUES('S1','REPORT-PART','chemistry','04600000000001','SUBMITTED')"""
        )

    marking_db.update_suz_utilisation_report(
        temp_db,
        "REPORT-PART",
        status="PARTIALLY",
        response={"reportStatus": "PARTIALLY"},
        error_text="one code rejected",
    )

    rows = {row["order_id"]: row for row in marking_db.list_supply_code_assignments(temp_db, "S1")}
    assert rows[10]["utilization_status"] == "APPLIED"
    assert rows[11]["utilization_status"] == "PARTIALLY"
    assert "one code rejected" in rows[11]["error_text"]
    assert "PARTIALLY" in fbe.UTILISATION_TERMINAL_STATUSES


def test_completed_codes_stop_polling_stale_sent_report(monkeypatch):
    assignments = [{
        "order_id": 1,
        "raw_code": "C1",
        "product_group": "chemistry",
        "utilization_report_id": "R1",
        "utilization_status": "APPLIED",
        "circulation_status": "",
    }]
    monkeypatch.setattr(fbe, "list_supply_code_assignments", lambda path, sid: assignments)
    monkeypatch.setattr(
        fbe, "list_suz_utilisation_reports",
        lambda path, sid: [{"report_id": "R1", "status": "SENT"}],
    )
    monkeypatch.setattr(fbe, "list_circulation_documents", lambda path, sid: [])
    monkeypatch.setattr(fbe, "_assignment_product_group", lambda row: row["product_group"])
    monkeypatch.setattr(
        fbe, "_refresh_supply_true_statuses",
        lambda sid: {"updated": 0, "errors": [], "unresolved": 0},
    )
    monkeypatch.setattr(
        fbe, "load_marking_workspace",
        lambda sid, fetch_wb=False: {"applied_codes": 1, "introduced_codes": 0, "marking_orders": 1},
    )

    class FakeSuz:
        def get_report_info(self, report_id):
            raise AssertionError("terminal-by-code report must not be polled")

    monkeypatch.setattr(fbe, "suz", FakeSuz())
    result = fbe._refresh_circulation_status_impl("S1")
    assert result["ok"] is True
    assert result["errors"] == []


def test_mixed_introduction_preserves_first_group_and_retries_only_remaining(monkeypatch):
    rows = [
        *[
            {
                "order_id": 100 + index, "raw_code": f"PERF-{index}",
                "gtin": "04600000000001", "seller_article": f"P-{index}",
                "product_group": "perfumery", "utilization_status": "APPLIED",
                "circulation_status": "", "circulation_document_id": "",
            }
            for index in range(6)
        ],
        {
            "order_id": 200, "raw_code": "CHEM-1", "gtin": "04600000000002",
            "seller_article": "D-1", "product_group": "chemistry",
            "utilization_status": "APPLIED", "circulation_status": "",
            "circulation_document_id": "",
        },
    ]
    monkeypatch.setattr(fbe, "list_supply_code_assignments", lambda path, sid: rows)
    monkeypatch.setattr(fbe, "_assignment_product_group", lambda row: row["product_group"])
    monkeypatch.setattr(fbe, "_refresh_supply_true_statuses", lambda sid: {"updated": 0})
    monkeypatch.setattr(
        fbe, "load_marking_workspace",
        lambda sid, fetch_wb=False: {"all_marking_assigned": True, "compliance_conflicts": []},
    )
    monkeypatch.setattr(fbe, "get_catalog", lambda: object())
    monkeypatch.setattr(
        fbe, "_catalog_compliance_for_identity",
        lambda catalog, articles, gtin: ({"tnved_code": "3303009000"}, articles, []),
    )
    active_documents = []
    monkeypatch.setattr(fbe, "list_circulation_documents", lambda path, sid: active_documents)
    persisted = []
    linked = []

    def persist_document(path, **kwargs):
        persisted.append(dict(kwargs))
        linked.append({
            "order_ids": list(kwargs.get("order_ids") or []),
            "document_uuid": kwargs.get("document_uuid"),
        })
        return kwargs

    monkeypatch.setattr(
        fbe, "create_circulation_document",
        persist_document,
    )
    monkeypatch.setattr(fbe, "save_suz_local_settings", lambda changes: changes)
    monkeypatch.setattr(fbe, "_save_true_api_diagnostic", lambda **kwargs: "")

    class FakeSuz:
        def __init__(self):
            self.calls = []
            self.fail_chemistry = True

        @staticmethod
        def identification_code(value):
            return value

        def create_true_document(self, *, product_group, document_type, document_payload):
            self.calls.append(product_group)
            if product_group == "chemistry" and self.fail_chemistry:
                raise SuzApiError("temporary chemistry failure", status_code=503)
            return {"uuid": f"DOC-{product_group}"}

    fake = FakeSuz()
    monkeypatch.setattr(fbe, "suz", fake)
    payload = fbe.CirculationIntroducePayload(
        confirmed=True,
        participant_inn="0000000000",
        production_date=datetime.now(timezone.utc).date().isoformat(),
        compliance_by_gtin={
            "04600000000001": {"tnved_code": "3303009000"},
            "04600000000002": {"tnved_code": "3307200000"},
        },
    )

    first = fbe.marking_introduce_into_circulation("MIXED-7", payload)
    assert first.status_code == 503
    assert fake.calls == ["perfumery", "chemistry"]
    assert [row["product_group"] for row in persisted] == ["perfumery"]
    assert linked[0]["order_ids"] == [100, 101, 102, 103, 104, 105]

    for row in rows[:6]:
        row["circulation_status"] = "submitted"
        row["circulation_document_id"] = "DOC-perfumery"
    # Document completion can become visible before the individual CIS switches
    # to INTRODUCED. CHECKED_OK must still block a duplicate document.
    active_documents[:] = [{"document_uuid": "DOC-perfumery", "status": "CHECKED_OK"}]
    fake.fail_chemistry = False
    fake.calls.clear()

    second = fbe.marking_introduce_into_circulation("MIXED-7", payload)
    assert second["ok"] is True
    assert fake.calls == ["chemistry"]
    assert linked[-1]["order_ids"] == [200]
    assert second["already_submitted_codes"] == 6

    # A CIS rejection can arrive before the document status endpoint catches
    # up. It must reopen only that failed document's codes instead of leaving
    # the stale ``submitted`` UUID as an infinite retry barrier.
    rows[-1]["circulation_status"] = "REJECTED"
    rows[-1]["circulation_document_id"] = "DOC-chemistry-stale"
    active_documents.append({"document_uuid": "DOC-chemistry-stale", "status": "submitted"})
    fake.calls.clear()
    third = fbe.marking_introduce_into_circulation("MIXED-7", payload)
    assert third["ok"] is True
    assert fake.calls == ["chemistry"]


def test_background_operation_key_deduplicates_concurrent_submission():
    started = threading.Event()
    release = threading.Event()

    def worker(report):
        started.set()
        release.wait(timeout=2)
        return {"ok": True, "message": "done"}

    first = fbe.submit_background_job("one", worker, operation_key="same-operation")
    assert started.wait(timeout=1)
    second = fbe.submit_background_job("two", worker, operation_key="same-operation")
    release.set()
    assert second == first


def test_supply_transfer_ui_stages_use_background_jobs(monkeypatch):
    submitted = []

    def fake_submit(title, worker, *, operation_key=None):
        submitted.append((title, operation_key, worker))
        return f"transfer-job-{len(submitted)}"

    monkeypatch.setattr(fbe, "submit_background_job", fake_submit)

    preflight = fbe.transfer_supplies_preflight_job(
        fbe.SupplyTransferPreflightPayload(
            supply_ids=["S2", "S1"], confirmed_assembled=True
        )
    )
    cargo = fbe.transfer_supplies_cargo_job(
        fbe.SupplyTransferCargoPayload(
            items=[
                fbe.SupplyTransferCargoItem(supply_id="S1", amount=2),
                fbe.SupplyTransferCargoItem(supply_id="S2", amount=1),
            ]
        )
    )
    deliver = fbe.transfer_supplies_deliver_job(
        fbe.SupplyTransferDeliverPayload(
            supply_ids=["S1"], confirmed_labels_applied=True
        )
    )
    supply_qr = fbe.transfer_supplies_supply_qr_job(
        fbe.SupplyTransferQrPayload(supply_ids=["S1"])
    )

    assert [preflight["job_id"], cargo["job_id"], deliver["job_id"], supply_qr["job_id"]] == [
        "transfer-job-1", "transfer-job-2", "transfer-job-3", "transfer-job-4"
    ]
    assert [entry[1] for entry in submitted] == [
        "transfer-preflight:S1,S2",
        "transfer-cargo:S1=2,S2=1",
        "transfer-deliver:S1",
        "transfer-supply-qr:S1",
    ]

    monkeypatch.setattr(
        fbe,
        "transfer_supplies_preflight",
        lambda payload: {"ok": True, "all_ready": True, "supplies": []},
    )
    progress = []
    result = fbe._transfer_preflight_worker(
        fbe.SupplyTransferPreflightPayload(
            supply_ids=["S1"], confirmed_assembled=True
        ),
        lambda **changes: progress.append(changes),
    )
    assert result["all_ready"] is True
    assert progress[-1]["progress"] == 100


def test_failed_wb_refresh_has_bounded_backoff(monkeypatch):
    monkeypatch.setitem(fbe.config, "fbs_operational_retry_seconds", 30)
    assert fbe._fbs_operational_refresh_due({"fresh": False, "sync_ok": False, "age_seconds": 4}) == (False, 26)
    assert fbe._fbs_operational_refresh_due({"fresh": False, "sync_ok": False, "age_seconds": 30}) == (True, 0)
    assert fbe._fbs_operational_refresh_due({"fresh": False, "sync_ok": True, "age_seconds": 999}) == (True, 0)


def test_failed_supply_membership_does_not_publish_fresh_checkpoint(temp_db, monkeypatch):
    monkeypatch.setattr(
        fbe.wb,
        "get_all_supplies",
        lambda max_pages: [{"id": "OPEN-1", "name": "Open", "done": False}],
    )
    monkeypatch.setattr(
        fbe.wb,
        "get_supply_order_ids",
        lambda supply_id: (_ for _ in ()).throw(WBApiError("temporary")),
    )
    monkeypatch.setattr(fbe.wb, "get_orders_last_days", lambda days: [])

    result = fbe._sync_fbs_lifecycle_registry(include_supplies=True, deep=False)
    checkpoint = marking_db.get_fbs_sync_state(temp_db, sync_key="operational")

    assert result["core"]["supplies_ok"] is True
    assert result["core"]["membership_ok"] is False
    assert checkpoint["ok"] is False
    assert "Состав поставки OPEN-1" in checkpoint["error_text"]


def test_current_wb_open_supply_is_not_hidden_only_because_it_is_old():
    supplies = [{
        "supply_id": "OPEN-OLD", "name": "Долго открытая", "done": 0,
        "created_at_wb": "2024-01-01T00:00:00Z", "order_count": 0,
    }]
    groups = fbe._fbs_dashboard_supply_groups(
        [], supplies, authoritative_open_supply_ids={"OPEN-OLD"}
    )
    assert [row["id"] for row in groups["assembling"]] == ["OPEN-OLD"]


class DummyResponse:
    def __init__(self, status_code, payload, headers=None):
        self.status_code = status_code
        self._payload = payload
        self.headers = headers or {}
        self.text = json.dumps(payload)
        self.content = self.text.encode("utf-8")
        self.reason = "error" if status_code >= 400 else "ok"
        self.ok = 200 <= status_code < 300

    def json(self):
        return self._payload

    def close(self):
        return None


def test_wb_retries_safe_status_read_but_not_mutation(monkeypatch):
    client = WBClient("token")
    responses = [
        DummyResponse(429, {"error": "rate"}, {"Retry-After": "0"}),
        DummyResponse(200, {"orders": [{"id": 1, "wbStatus": "waiting"}]}),
    ]

    class Session:
        def request(self, *args, **kwargs):
            return responses.pop(0)

    monkeypatch.setattr(client, "_get_session", lambda: Session())
    assert client.get_order_statuses([1])[0]["id"] == 1
    assert responses == []

    mutation_calls = []

    class FailingSession:
        def request(self, *args, **kwargs):
            mutation_calls.append(1)
            return DummyResponse(503, {"error": "temporary"})

    monkeypatch.setattr(client, "_get_session", lambda: FailingSession())
    with pytest.raises(WBApiError):
        client.create_supply("name")
    assert len(mutation_calls) == 1


def test_wb_recovers_remote_disconnected_for_read_only_call(monkeypatch):
    client = WBClient("token")
    attempts = []

    class FlakySession:
        def request(self, *args, **kwargs):
            attempts.append(1)
            if len(attempts) == 1:
                raise requests.ConnectionError(http.client.RemoteDisconnected("closed"))
            return DummyResponse(200, {"orders": []})

    session = FlakySession()
    monkeypatch.setattr(client, "_get_session", lambda: session)
    monkeypatch.setattr("wb_api.time.sleep", lambda seconds: None)
    assert client.get_new_orders() == []
    assert len(attempts) == 2


def test_economy_read_client_retries_network_and_5xx(monkeypatch):
    client = EconomyWBClient(
        default_token="token",
        max_retries=3,
        retry_fallback_seconds=1,
        request_timeout_seconds=2,
    )
    responses = [
        requests.ConnectionError(http.client.RemoteDisconnected("closed")),
        DummyResponse(503, {"error": "temporary"}),
        DummyResponse(200, [{"srid": "ok"}]),
    ]

    class Session:
        def request(self, *args, **kwargs):
            value = responses.pop(0)
            if isinstance(value, Exception):
                raise value
            return value

    client.session = Session()
    monkeypatch.setattr("economy.wb_client.time.sleep", lambda seconds: None)
    assert client.get_orders("2026-01-01") == [{"srid": "ok"}]
    assert responses == []
    events = client.drain_request_events()
    assert [event["status_code"] for event in events] == [None, 503, 200]


def test_bartender_print_process_has_bounded_timeout(tmp_path, monkeypatch):
    executable = tmp_path / "bartender.exe"
    template = tmp_path / "label.btw"
    executable.write_bytes(b"")
    template.write_bytes(b"")
    observed = {}

    def fake_run(command, **kwargs):
        observed.update(kwargs)

    monkeypatch.setattr("printers.bartender_label.subprocess.run", fake_run)
    print_internal_label(
        {
            "bartender_csv": str(tmp_path / "label.csv"),
            "bartender_template": str(template),
            "bartender_exe": str(executable),
            "internal_label_printer_name": "TEST",
            "dry_run_print": False,
            "bartender_print_timeout_seconds": 17,
        },
        {"seller_article": "A-1", "product": "Test"},
    )
    assert observed["check"] is True
    assert observed["timeout"] == 17


def test_true_api_cis_info_accepts_nested_response_wrapper(tmp_path, monkeypatch):
    client = SuzClient(base_dir=tmp_path, settings_loader=lambda: {})
    monkeypatch.setattr(client, "get_true_api_token", lambda: "token")
    monkeypatch.setattr(
        client,
        "_request",
        lambda *args, **kwargs: DummyResponse(
            200, {"data": {"items": [{"requestedCis": "CODE", "status": "APPLIED"}]}}
        ),
    )
    rows = client.get_cises_info(["CODE"], product_group="chemistry")
    assert rows == [{"requestedCis": "CODE", "status": "APPLIED"}]


def test_suz_retries_safe_5xx_but_never_retries_ambiguous_mutation(tmp_path, monkeypatch):
    client = SuzClient(base_dir=tmp_path, settings_loader=lambda: {
        "suz_connect_timeout_seconds": 1,
        "suz_status_timeout_seconds": 3,
    })
    responses = [
        DummyResponse(503, {"error": "temporary"}),
        DummyResponse(200, {"ok": True}),
    ]

    class Session:
        def request(self, *args, **kwargs):
            return responses.pop(0)

    monkeypatch.setattr(client, "_http_session", lambda: Session())
    monkeypatch.setattr("suz_client.time.sleep", lambda seconds: None)
    response = client._request("GET", "https://example.invalid/status", retry_connection=True)
    assert response.status_code == 200
    assert responses == []

    calls = []

    class ConflictSession:
        def request(self, *args, **kwargs):
            calls.append(1)
            return DummyResponse(409, {"error": "conflict"})

    monkeypatch.setattr(client, "_http_session", lambda: ConflictSession())
    with pytest.raises(SuzApiError) as error:
        client._request("POST", "https://example.invalid/mutation")
    assert error.value.status_code == 409
    assert len(calls) == 1


def test_submitted_external_document_survives_database_reopen(temp_db):
    add_code(
        temp_db, order_id=50, raw_code="PERSIST", utilization_status="APPLIED",
        circulation_status="APPLIED",
    )
    marking_db.create_circulation_document(
        temp_db,
        supply_id="S1",
        document_uuid="DOC-PERSIST",
        document_type="LP_INTRODUCE_GOODS",
        product_group="perfumery",
        payload={"products": [{"uit_code": "PERSIST"}]},
        response={"uuid": "DOC-PERSIST"},
        order_ids=[50],
        submission_kind="introduction",
    )

    # All repository calls open a new SQLite connection. Seeing the same UUID and
    # link here models a process restart: callers can poll, but must not resubmit.
    document = marking_db.list_circulation_documents(temp_db, "S1")[0]
    assignment = marking_db.get_assignment(temp_db, 50)
    assert document["document_uuid"] == "DOC-PERSIST"
    assert document["status"] == "submitted"
    assert assignment["circulation_document_id"] == "DOC-PERSIST"
    assert assignment["circulation_status"] == "submitted"


def test_document_and_code_link_are_one_sqlite_transaction(temp_db):
    add_code(
        temp_db, order_id=51, raw_code="ATOMIC", utilization_status="APPLIED",
        circulation_status="APPLIED",
    )
    with sqlite3.connect(temp_db) as connection:
        connection.execute(
            """CREATE TRIGGER fail_atomic_link BEFORE UPDATE ON marking_codes
               BEGIN SELECT RAISE(ABORT, 'forced link failure'); END"""
        )

    with pytest.raises(sqlite3.IntegrityError):
        marking_db.create_circulation_document(
            temp_db,
            supply_id="S1",
            document_uuid="DOC-ROLLBACK",
            document_type="LP_INTRODUCE_GOODS",
            product_group="perfumery",
            payload={},
            response={"uuid": "DOC-ROLLBACK"},
            order_ids=[51],
            submission_kind="introduction",
        )

    assert marking_db.get_circulation_document(temp_db, "DOC-ROLLBACK") is None
    assert marking_db.get_assignment(temp_db, 51)["circulation_document_id"] == ""


def test_settings_are_centralized_and_local_update_is_atomic(tmp_path, monkeypatch):
    monkeypatch.delenv("WB_TOKEN", raising=False)
    monkeypatch.delenv("FBE_WB_TOKEN", raising=False)
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "economy.json").write_text(
        json.dumps({"unified_token": "legacy-token"}), encoding="utf-8"
    )
    (tmp_path / "config.json").write_text(
        json.dumps({
            "suz_local_settings_path": "data/suz.json",
            "economy_settings_path": "data/economy.json",
            "suz_oms_id": "config",
        }),
        encoding="utf-8",
    )
    # Public release intentionally rejects unverified legacy/env credentials. Preserve the
    # atomic-update regression while asserting the new seller-scoped boundary.
    settings = Settings(tmp_path / "config.json")
    assert settings.data["wb_token"] == ""
    settings.set_runtime_wb_connection("synthetic-token", {"sid": "test-seller"})
    settings = Settings(tmp_path / "config.json")
    effective = settings.update_external_api_settings({"suz_oms_id": "local", "unrelated": "ignored"})
    assert effective["suz_oms_id"] == "local"
    stored = json.loads((tmp_path / "data/local/connections.json").read_text(encoding="utf-8"))
    assert stored["marking_by_sid"]["test-seller"] == {"suz_oms_id": "local"}
    assert not list((tmp_path / "data/local").glob("*.tmp"))
    assert not (tmp_path / "data/suz.json").exists()
    monkeypatch.setenv("WB_TOKEN", "environment-token")
    with pytest.raises(ValueError):
        settings.set_value("wb_token", "form-token")
    assert Settings(tmp_path / "config.json").data["wb_token"] == "synthetic-token"
    persisted = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert "wb_token" not in persisted


def test_retire_payload_and_grouping_are_explicit(monkeypatch):
    monkeypatch.setattr(fbe, "suz", type("S", (), {"identification_code": staticmethod(lambda value: value)})())
    payload = fbe._retire_payload(
        [{"order_id": 1, "raw_code": "C1"}], participant_inn="0000000000"
    )
    assert payload["action"] == "DISTANCE"
    assert payload["products"] == [{"cis": "C1"}]
    with pytest.raises(marking_db.MarkingDbError, match="группа"):
        fbe._post_sale_group_rows([{"order_id": 1, "product_group": ""}])


def test_print_pairs_are_serialized_as_whole_jobs(monkeypatch):
    first_entered = threading.Event()
    release_first = threading.Event()
    second_entered = threading.Event()
    calls: list[str] = []

    def fake_wb(*, sticker_path, **kwargs):
        marker = Path(sticker_path).stem
        calls.append(f"wb-{marker}")
        if marker == "1":
            first_entered.set()
            release_first.wait(timeout=2)
        else:
            second_entered.set()

    def fake_internal(config, product):
        calls.append(f"internal-{product['seller_article']}")

    monkeypatch.setattr(print_queue, "print_wb_sticker", fake_wb)
    monkeypatch.setattr(print_queue, "print_internal_label", fake_internal)
    config = {
        "wb_printer_name": "dry",
        "wb_sticker_type": "png",
        "dry_run_print": True,
        "print_gap_seconds": 0,
    }

    def run(order_id):
        print_queue.print_order_pair(
            config, order_id, str(order_id), f"/{order_id}.png",
            {"seller_article": str(order_id)},
        )

    first = threading.Thread(target=run, args=(1,))
    second = threading.Thread(target=run, args=(2,))
    first.start()
    assert first_entered.wait(timeout=1)
    second.start()
    assert not second_entered.wait(timeout=0.05)
    release_first.set()
    first.join(timeout=1)
    second.join(timeout=1)
    assert calls == ["wb-1", "internal-1", "wb-2", "internal-2"]


def test_post_sale_rejection_reopens_only_its_linked_codes(temp_db):
    add_code(
        temp_db, order_id=70, raw_code="RETIRE-FAIL",
        circulation_status="INTRODUCED", post_sale_status="retirement_submitted",
    )
    add_code(
        temp_db, order_id=71, raw_code="RETURN-FAIL",
        circulation_status="RETIRED", post_sale_status="return_submitted",
    )
    with sqlite3.connect(temp_db) as connection:
        connection.execute(
            "UPDATE marking_codes SET retire_document_id='DOC-R' WHERE order_id=70"
        )
        connection.execute(
            "UPDATE marking_codes SET return_document_id='DOC-B' WHERE order_id=71"
        )

    assert marking_db.mark_post_sale_document_failed(
        temp_db,
        document_uuid="DOC-R",
        document_type="LK_RECEIPT",
        error_text="rejected",
    ) == 1
    assert marking_db.mark_post_sale_document_failed(
        temp_db,
        document_uuid="DOC-B",
        document_type="LP_RETURN",
        error_text="rejected",
    ) == 1
    assert marking_db.get_assignment(temp_db, 70)["post_sale_status"] == "ready_to_retire"
    assert marking_db.get_assignment(temp_db, 71)["post_sale_status"] == "retired"
    assert event_count(temp_db, "post_sale_document_failed") == 2
    assert marking_db.mark_post_sale_document_failed(
        temp_db,
        document_uuid="DOC-R",
        document_type="LK_RECEIPT",
        error_text="rejected",
    ) == 0


def test_post_sale_mixed_group_retry_does_not_resubmit_successful_group(temp_db, monkeypatch):
    add_code(
        temp_db, order_id=80, raw_code="PERF-CODE",
        circulation_status="INTRODUCED", post_sale_status="ready_to_retire",
    )
    add_code(
        temp_db, order_id=81, raw_code="CHEM-CODE",
        circulation_status="INTRODUCED", post_sale_status="ready_to_retire",
    )
    monkeypatch.setattr(
        fbe,
        "_circulation_defaults",
        lambda: {"participant_inn": "0000000000"},
    )

    def group_rows(rows):
        grouped = {"perfumery": [], "chemistry": []}
        for row in rows:
            grouped["perfumery" if int(row["order_id"]) == 80 else "chemistry"].append(row)
        return {key: value for key, value in grouped.items() if value}

    monkeypatch.setattr(fbe, "_post_sale_group_rows", group_rows)

    class FakeSuz:
        fail_chemistry = True
        calls: list[str] = []

        @staticmethod
        def identification_code(value):
            return value

        def create_true_document(self, *, product_group, **kwargs):
            self.calls.append(product_group)
            if product_group == "chemistry" and self.fail_chemistry:
                raise SuzApiError("temporary")
            return {"uuid": f"DOC-{product_group}"}

    fake = FakeSuz()
    monkeypatch.setattr(fbe, "suz", fake)
    payload = fbe.PostSaleIdsPayload(order_ids=[80, 81], confirmed=True)
    with pytest.raises(SuzApiError):
        fbe._retire_post_sale_impl(payload)

    assert marking_db.get_assignment(temp_db, 80)["post_sale_status"] == "retirement_submitted"
    assert marking_db.get_assignment(temp_db, 81)["post_sale_status"] == "ready_to_retire"

    fake.fail_chemistry = False
    result = fbe._retire_post_sale_impl(payload)
    assert result["documents"] == ["DOC-chemistry"]
    assert result["already_submitted"] == 1
    assert fake.calls == ["perfumery", "chemistry", "chemistry"]
