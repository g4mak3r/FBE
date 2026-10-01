import base64
import copy
import json
from datetime import date
from uuid import uuid4

import pytest

from fbe_flow.core.errors import Conflict, InvalidInput, NotFound
from fbe_flow.integrations.chz.http import Reply
from fbe_flow.modules.connections import connection_context

from .test_chz_workflows import send


def configure_suz(workspace):
    state, client, seller, connection, provider, *_ = workspace
    response = client.put(
        f"/api/sellers/{seller}/marking/{connection['id']}/suz",
        json={"oms_id": provider.oms_id, "oms_connection": provider.oms_connection},
    )
    assert response.status_code == 200, response.text
    return state.registry.get("chz")


def order_payload(workspace, *, key="production-task-1", quantity=3):
    provider = workspace[4]
    return {
        "request_key": key,
        "product_group": provider.group,
        "document": {
            "productGroup": provider.group,
            "products": [
                {
                    "gtin": "00000000000001",
                    "quantity": quantity,
                    "serialNumberType": "OPERATOR",
                    "templateId": 1,
                    "cisType": "UNIT",
                }
            ],
            "attributes": {"account_specific": "Значение"},
        },
    }


def prepare_order(workspace, **kwargs):
    state, _, seller, connection, *_ = workspace
    return state.marking.prepare(
        seller, connection["id"], "suz_order", order_payload(workspace, **kwargs)
    )


def ready_order(workspace, **kwargs):
    configure_suz(workspace)
    accepted, job = send(workspace, prepare_order(workspace, **kwargs))
    assert job["status"] == "succeeded" and accepted["state"] == "accepted"
    result, _ = send(workspace, accepted, poll=True)
    assert result["state"] == "succeeded"
    return result


def prepare_codes(workspace, order, quantity=2):
    state, _, seller, connection, provider, *_ = workspace
    return state.marking.prepare(
        seller,
        connection["id"],
        "suz_codes",
        {
            "product_group": provider.group,
            "order_id": order["external_id"],
            "gtin": "00000000000001",
            "quantity": quantity,
        },
    )


def issued_codes(workspace):
    order = ready_order(workspace)
    doc, job = send(workspace, prepare_codes(workspace, order))
    assert job["status"] == "succeeded" and doc["state"] == "succeeded"
    return order, doc


def prepare_application(workspace):
    state, _, seller, connection, provider, *_ = workspace
    items = state.marking.codes(seller, connection["id"])["items"]
    return state.marking.utilisation_from_codes(
        seller,
        connection["id"],
        {
            "code_ids": [v["id"] for v in items],
            "product_group": provider.group,
            "attributes": {"productionDate": date.today().isoformat()},
        },
    )


def test_suz_authentication_is_attached_and_cached_separately(workspace):
    adapter = configure_suz(workspace)
    state, _, seller, connection, provider, _, signer = workspace
    config = adapter.config(connection_context(state.connections.get(seller, connection["id"])))
    adapter.suz_ping(config)
    adapter.account(config)
    assert len([v for v in provider.calls if "/auth/simpleSignIn/" in v[1]]) == 1
    assert all(not detached for _, _, detached in signer.calls)
    assert config.oms_id == provider.oms_id


def test_order_signs_exact_utf8_bytes_and_waits_for_ready(workspace):
    configure_suz(workspace)
    doc = prepare_order(workspace)
    assert not workspace[4].orders
    accepted, job = send(workspace, doc)
    assert accepted["state"] == "accepted" and job["status"] == "succeeded"
    internal = workspace[0].marking.get_document(workspace[2], doc["id"], internal=True)
    raw = base64.b64decode(internal["wire"]["data"])
    request = next(v for v in workspace[4].raw_requests if v[0] == "/api/v3/order")
    assert raw == request[1] and json.loads(raw) == doc["body"]["document"]
    assert request[2]["X-Signature"] == internal["wire"]["signature"]
    assert any(v[0] == raw and v[2] for v in workspace[6].calls)
    result, _ = send(workspace, accepted, poll=True)
    assert result["state"] == "succeeded" and result["external_status"] == "READY"


def test_full_emission_application_introduction_and_withdrawal(workspace):
    state, client, seller, connection, provider, *_ = workspace
    order, doc = issued_codes(workspace)
    items = state.marking.codes(seller, connection["id"])["items"]
    assert len(items) == 2 and all(v["has_full_code"] for v in items)
    full = state.marking.code_export(seller, connection["id"], doc["external_id"])["codes"]
    assert full == provider.blocks[doc["external_id"]]["codes"] and "\x1d" in full[0]
    export = client.get(
        f"/api/sellers/{seller}/marking/{connection['id']}/blocks/{doc['external_id']}/export"
    )
    assert export.status_code == 200 and export.json()["codes"] == full
    application = prepare_application(workspace)
    assert sorted(application["body"]["document"]["sntins"]) == sorted(full)
    accepted, _ = send(workspace, application)
    final, _ = send(workspace, accepted, poll=True)
    assert final["state"] == "succeeded" and final["external_status"] == "SUCCESS"
    base = {
        "type": "LP_INTRODUCE_GOODS",
        "product_group": provider.group,
        "document": {
            "participant_inn": provider.inn,
            "producer_inn": provider.inn,
            "owner_inn": provider.inn,
            "production_type": "OWN_PRODUCTION",
            "production_date": date.today().isoformat(),
            "products": [{"uit_code": v["code"]} for v in items],
        },
    }
    introduction = state.marking.prepare(seller, connection["id"], "true", base)
    accepted, _ = send(workspace, introduction)
    final, _ = send(workspace, accepted, poll=True)
    assert final["state"] == "succeeded"
    withdrawal = state.marking.prepare(
        seller,
        connection["id"],
        "true",
        {
            "type": "LK_RECEIPT",
            "product_group": provider.group,
            "document": {
                "inn": provider.inn,
                "action": "OTHER",
                "withdrawal_type_other": "Использование",
                "action_date": date.today().isoformat(),
                "document_type": "OTHER",
                "document_number": "TASK-1",
                "document_date": date.today().isoformat(),
                "products": [{"cis": v["code"]} for v in items],
            },
        },
    )
    accepted, _ = send(workspace, withdrawal)
    final, _ = send(workspace, accepted, poll=True)
    assert final["state"] == "succeeded"
    assert all(provider.cises[v["code"]]["cisInfo"]["status"] == "WRITTEN_OFF" for v in items)
    check = state.operations.enqueue(
        seller,
        connection["id"],
        "codes.check",
        {"product_group": provider.group, "codes": [v["code"] for v in items]},
    )
    assert (
        state.operations.run_once()
        and state.operations.get(seller, check["id"])["status"] == "succeeded"
    )
    refreshed = state.marking.codes(seller, connection["id"])["items"]
    assert {v["id"] for v in refreshed} == {v["id"] for v in items}
    assert all(v["external_status"] == "WRITTEN_OFF" for v in refreshed)
    assert state.marking.code_export(seller, connection["id"], doc["external_id"])["codes"] == full
    close = state.marking.prepare(
        seller,
        connection["id"],
        "suz_close",
        {
            "product_group": provider.group,
            "order_id": order["external_id"],
            "gtin": "00000000000001",
        },
    )
    accepted, _ = send(workspace, close)
    final, _ = send(workspace, accepted, poll=True)
    assert final["state"] == "succeeded" and final["external_status"] == "CLOSED"


@pytest.mark.parametrize(
    "change",
    [
        {"quantity": 0},
        {"quantity": True},
        {"quantity": 2000001},
        {"gtin": "1"},
        {"templateId": 0},
        {"templateId": True},
        {"serialNumberType": "RANDOM"},
        {"cisType": ""},
        {"serialNumbers": ["SERIAL"]},
        {"serialNumberType": "SELF_MADE", "serialNumbers": ["x", "x", "x"]},
    ],
)
def test_invalid_emission_products_never_submit(workspace, change):
    configure_suz(workspace)
    payload = order_payload(workspace)
    payload["document"]["products"][0].update(change)
    with pytest.raises(InvalidInput):
        workspace[0].marking.prepare(workspace[2], workspace[3]["id"], "suz_order", payload)
    assert not workspace[4].orders


def test_self_made_serials_and_account_attributes_are_preserved(workspace):
    configure_suz(workspace)
    payload = order_payload(workspace)
    payload["document"]["products"][0].update(
        serialNumberType="SELF_MADE", serialNumbers=["one", "two", "three"]
    )
    doc = workspace[0].marking.prepare(workspace[2], workspace[3]["id"], "suz_order", payload)
    assert doc["body"]["document"] == payload["document"]
    accepted, _ = send(workspace, doc)
    assert accepted["state"] == "accepted"


def test_gtin_group_and_unique_task_must_come_from_account(workspace):
    configure_suz(workspace)
    payload = order_payload(workspace)
    payload["product_group"] = payload["document"]["productGroup"] = "foreign-group"
    with pytest.raises(InvalidInput):
        workspace[0].marking.prepare(workspace[2], workspace[3]["id"], "suz_order", payload)
    workspace[4].cards[1]["good_mark_flag"] = False
    with pytest.raises(InvalidInput):
        prepare_order(workspace)
    workspace[4].cards[1]["good_mark_flag"] = True
    doc = prepare_order(workspace)
    with pytest.raises(Conflict):
        prepare_order(workspace)
    with pytest.raises(InvalidInput):
        prepare_order(workspace, key=" ")
    final, _ = send(workspace, doc)
    send(workspace, final, poll=True)
    assert prepare_order(workspace, key="new-task")["id"] != doc["id"]


@pytest.mark.parametrize(
    "status,expected",
    [
        ("READY", "succeeded"),
        ("DECLINED", "rejected"),
        ("CLOSED", "succeeded"),
        ("APPROVED", "processing"),
        ("FUTURE_STATE", "processing"),
    ],
)
def test_order_status_is_conservative(workspace, status, expected):
    configure_suz(workspace)
    accepted, _ = send(workspace, prepare_order(workspace))
    workspace[4].orders[accepted["external_id"]]["orderStatus"] = status
    result, _ = send(workspace, accepted, poll=True)
    assert result["state"] == expected


@pytest.mark.parametrize(
    "status,expected",
    [
        ("SUCCESS", "succeeded"),
        ("FAILED", "rejected"),
        ("REJECTED", "rejected"),
        ("PARTIALLY", "partial"),
        ("SENT", "processing"),
        ("CHECK", "processing"),
        ("PROCESSED", "processing"),
        ("UNKNOWN", "processing"),
    ],
)
def test_application_report_status_never_confuses_sent_with_success(workspace, status, expected):
    issued_codes(workspace)
    accepted, _ = send(workspace, prepare_application(workspace))
    workspace[4].reports[accepted["external_id"]]["reportStatus"] = status
    result, _ = send(workspace, accepted, poll=True)
    assert result["state"] == expected


@pytest.mark.parametrize("quantity", [0, True, 5001, 4])
def test_codes_limits_and_available_buffer(workspace, quantity):
    order = ready_order(workspace)
    with pytest.raises(InvalidInput):
        prepare_codes(workspace, order, quantity)
    assert not workspace[4].blocks


def test_changed_baseline_before_send_keeps_document_prepared(workspace):
    order = ready_order(workspace)
    doc = prepare_codes(workspace, order)
    workspace[4].blocks[str(uuid4())] = {
        "order_id": order["external_id"],
        "gtin": "00000000000001",
        "codes": ["external-client-block"],
    }
    result, job = send(workspace, doc)
    assert result["state"] == "prepared" and job["status"] == "failed"
    assert not any(v[1] == "/api/v3/codes" for v in workspace[4].calls)


def test_lost_codes_response_retrieves_block_without_new_codes_request(workspace):
    state, _, seller, connection, provider, *_ = workspace
    order = ready_order(workspace)
    doc = prepare_codes(workspace, order)
    provider.write_failure = "timeout"
    result, job = send(workspace, doc)
    assert result["state"] == "unknown" and job["status"] == "failed"
    assert not state.marking.codes(seller, connection["id"])["items"]
    with pytest.raises(Conflict):
        state.marking.retry_document(seller, doc["id"])
    provider.write_failure = None
    block_id = next(iter(provider.blocks))
    final = state.marking.reconcile_document(seller, doc["id"], block_id)
    assert final["state"] == "succeeded" and final["external_id"] == block_id
    assert len([v for v in provider.calls if v[1] == "/api/v3/codes"]) == 1
    retry = next(v for v in provider.calls if v[1].endswith("/codes/retry"))
    assert set(retry[2]) == {"omsId", "blockId"}
    assert (
        state.marking.code_export(seller, connection["id"], block_id)["codes"]
        == provider.blocks[block_id]["codes"]
    )


def test_ambiguous_new_blocks_require_manual_investigation(workspace):
    order = ready_order(workspace)
    doc = prepare_codes(workspace, order)
    provider = workspace[4]
    provider.write_failure = "timeout"
    send(workspace, doc)
    block_id = next(iter(provider.blocks))
    provider.blocks[str(uuid4())] = copy.deepcopy(provider.blocks[block_id])
    with pytest.raises(Conflict):
        workspace[0].marking.reconcile_document(workspace[2], doc["id"], block_id)
    assert workspace[0].marking.get_document(workspace[2], doc["id"])["state"] == "unknown"
    assert not any(v[1].endswith("/codes/retry") for v in provider.calls)


@pytest.mark.parametrize("defect", ["gtin", "size", "duplicate", "oms", "crypto"])
def test_invalid_block_acknowledgement_is_not_saved(workspace, defect):
    order = ready_order(workspace)
    doc = prepare_codes(workspace, order)
    provider = workspace[4]
    original = provider.request

    def changed(method, url, **kwargs):
        value = original(method, url, **kwargs)
        if url.endswith("/api/v3/codes"):
            body = copy.deepcopy(value.data)
            if defect == "gtin":
                body["codes"][0] = body["codes"][0].replace("00000000000001", "00000000000002")
            elif defect == "size":
                body["codes"].pop()
            elif defect == "duplicate":
                body["codes"][1] = body["codes"][0]
            elif defect == "crypto":
                body["codes"][0] = body["codes"][0].split("\x1d", 1)[0]
            else:
                body["omsId"] = str(uuid4())
            return Reply(200, body, {})
        return value

    provider.request = changed
    result, job = send(workspace, doc)
    assert result["state"] == "unknown" and job["status"] == "failed"
    assert not workspace[0].marking.codes(workspace[2], workspace[3]["id"])["items"]


def test_application_preflight_must_be_rechecked_before_signature(workspace):
    issued_codes(workspace)
    doc = prepare_application(workspace)
    next(iter(workspace[4].cises.values()))["cisInfo"]["status"] = "WRITTEN_OFF"
    detached_before = len([v for v in workspace[6].calls if v[2]])
    result, job = send(workspace, doc)
    assert result["state"] == "prepared" and job["status"] == "failed"
    assert not workspace[4].reports
    assert len([v for v in workspace[6].calls if v[2]]) == detached_before


def test_suz_settings_cannot_change_with_pending_document(workspace):
    configure_suz(workspace)
    prepare_order(workspace)
    state, client, seller, connection, provider, *_ = workspace
    response = client.put(
        f"/api/sellers/{seller}/marking/{connection['id']}/suz",
        json={"oms_id": str(uuid4()), "oms_connection": provider.oms_connection},
    )
    assert response.status_code == 409


def test_receipt_can_be_read_as_account_scoped_operation(workspace):
    doc = ready_order(workspace)
    state, _, seller, connection, provider, *_ = workspace
    receipt = provider.receipts[doc["external_id"]]
    job = state.operations.enqueue(
        seller, connection["id"], "suz.receipt", {"receipt_id": receipt["resultDocId"]}
    )
    assert state.operations.run_once()
    assert state.operations.get(seller, job["id"])["status"] == "succeeded"
    assert state.marking.overview(seller, connection["id"])["snapshots"]["suz_receipt"]["value"][
        "results"
    ] == [receipt]


def test_foreign_code_selection_and_export_do_not_cross_sellers(workspace):
    _, doc = issued_codes(workspace)
    state, client, seller, connection, provider, vault, *_ = workspace
    other = state.sellers.create("Other")["id"]
    ref = vault.put(other, {"true_token": "private-token"})
    other_connection = state.connections.create(
        other,
        "chz",
        "Other account",
        {"inn": provider.inn, "credential_ref": ref, "certificate": "A" * 40},
    )
    ids = [v["id"] for v in state.marking.codes(seller, connection["id"])["items"]]
    with pytest.raises(NotFound):
        state.marking.validate_code_selection(other, other_connection["id"], ids, full=True)
    url = (
        f"/api/sellers/{other}/marking/{other_connection['id']}/blocks/{doc['external_id']}/export"
    )
    assert client.get(url).status_code == 404


def test_legacy_connection_capabilities_upgrade_without_remote_request(workspace):
    state, _, seller, connection, provider, *_ = workspace
    with state.database.connection() as db:
        db.execute("UPDATE connections SET operations_json='[]' WHERE id=?", (connection["id"],))
    before = len(provider.calls)
    state.marking.refresh_capabilities()
    assert len(provider.calls) == before
    assert "document.submit" in {
        v["key"] for v in state.connections.get(seller, connection["id"])["operations"]
    }


@pytest.mark.parametrize(
    "buffers,state",
    [
        (["ACTIVE", "REJECTED"], "partial"),
        (["REJECTED", "REJECTED"], "rejected"),
        (["ACTIVE", "PENDING"], "processing"),
    ],
)
def test_ready_order_does_not_hide_rejected_or_unfinished_positions(workspace, buffers, state):
    from .chz_fixtures import card

    configure_suz(workspace)
    provider = workspace[4]
    provider.cards[2] = card(2)
    payload = order_payload(workspace)
    payload["document"]["products"].append(
        {**payload["document"]["products"][0], "gtin": "00000000000002"}
    )
    doc = workspace[0].marking.prepare(workspace[2], workspace[3]["id"], "suz_order", payload)
    accepted, _ = send(workspace, doc)
    for buffer, status in zip(
        provider.orders[accepted["external_id"]]["buffers"], buffers, strict=True
    ):
        buffer["bufferStatus"] = status
    final, _ = send(workspace, accepted, poll=True)
    assert final["state"] == state
