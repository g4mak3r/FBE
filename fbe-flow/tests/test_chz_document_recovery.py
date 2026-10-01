import base64
import copy
import json
import sqlite3
from uuid import uuid4

import pytest

from fbe_flow.core.errors import Conflict, InvalidInput
from fbe_flow.modules.connections import connection_context

from .test_chz_suz import (
    configure_suz,
    issued_codes,
    prepare_application,
    prepare_codes,
    prepare_order,
    ready_order,
)
from .test_chz_workflows import edit, send, true_payload


@pytest.mark.parametrize("failure", ["timeout", "corrupt", 500, 503])
def test_uncertain_acknowledgement_blocks_replay_and_scheduling(workspace, failure):
    configure_suz(workspace)
    doc = prepare_order(workspace)
    workspace[4].write_failure = failure
    result, job = send(workspace, doc)
    assert result["state"] == "unknown" and job["status"] == "failed"
    assert result["wire_saved"] and not result["retryable"]
    with pytest.raises(Conflict):
        workspace[0].marking.enqueue_document(workspace[2], doc["id"])
    with pytest.raises(Conflict):
        workspace[0].marking.retry_document(workspace[2], doc["id"])
    before = len(workspace[4].calls)
    assert not workspace[0].operations.run_once()
    assert len(workspace[4].calls) == before


@pytest.mark.parametrize("status", [400, 401, 403, 422, 429])
def test_definite_rejection_requires_explicit_fresh_preparation(workspace, status):
    configure_suz(workspace)
    doc = prepare_order(workspace)
    workspace[4].write_failure = status
    result, job = send(workspace, doc)
    assert result["state"] == "rejected" and result["retryable"]
    assert not workspace[4].orders
    assert not workspace[0].operations.run_once()
    retry = workspace[0].marking.retry_document(workspace[2], doc["id"])
    assert retry["state"] == "prepared"
    workspace[4].write_failure = None
    workspace[4].cards[1]["good_mark_flag"] = False
    result, job = send(workspace, retry)
    assert result["state"] == "prepared" and job["status"] == "failed"
    assert not workspace[4].orders


@pytest.mark.parametrize("kind", ["suz_order", "suz_utilisation", "suz_close"])
def test_unknown_suz_write_is_reconciled_by_original_receipt_content(workspace, kind):
    if kind == "suz_order":
        configure_suz(workspace)
        doc = prepare_order(workspace)
    elif kind == "suz_utilisation":
        issued_codes(workspace)
        doc = prepare_application(workspace)
    else:
        order = ready_order(workspace)
        doc = workspace[0].marking.prepare(
            workspace[2],
            workspace[3]["id"],
            kind,
            {
                "product_group": workspace[4].group,
                "order_id": order["external_id"],
                "gtin": "00000000000001",
            },
        )
    provider = workspace[4]
    provider.write_failure = "timeout"
    result, job = send(workspace, doc)
    assert result["state"] == "unknown" and job["status"] == "failed"
    provider.write_failure = None
    external_id = next(
        k
        for k, v in provider.receipts.items()
        if v["workflow"]
        == {
            "suz_order": "CREATE_ORDER",
            "suz_utilisation": "REPORT_UTILIZE",
            "suz_close": "CLOSE_ORDER",
        }[kind]
    )
    path = {
        "suz_order": "/api/v3/order",
        "suz_utilisation": "/api/v3/utilisation",
        "suz_close": "/api/v3/order/close",
    }[kind]
    writes_before = len([v for v in provider.calls if v[1] == path])
    expected_id = doc["body"]["document"]["orderId"] if kind == "suz_close" else external_id
    final = workspace[0].marking.reconcile_document(workspace[2], doc["id"], expected_id)
    assert final["state"] == "succeeded" and final["external_id"] == expected_id
    assert final["result"]["receipt"]["sourceDocId"] == external_id
    assert len([v for v in provider.calls if v[1] == path]) == writes_before
    assert any(v[1].endswith("/receipts/document") for v in provider.calls)


@pytest.mark.parametrize("defect", ["owner", "oms", "workflow", "time", "content", "event"])
def test_receipt_does_not_attach_wrong_or_old_external_document(workspace, defect):
    configure_suz(workspace)
    doc = prepare_order(workspace)
    provider = workspace[4]
    provider.write_failure = "timeout"
    send(workspace, doc)
    external_id, receipt = next(iter(provider.receipts.items()))
    if defect == "owner":
        receipt["details"]["participantInn"] = "999999999999"
    elif defect == "oms":
        receipt["operations"][0]["details"]["omsId"] = str(uuid4())
    elif defect == "workflow":
        receipt["workflow"] = "REPORT_UTILIZE"
    elif defect == "time":
        receipt["sourceDocDate"] = 1
    elif defect == "event":
        receipt["operations"] = []
    else:
        provider.originals[receipt["operations"][0]["docId"]]["products"][0]["quantity"] = 10
    with pytest.raises(Conflict):
        workspace[0].marking.reconcile_document(workspace[2], doc["id"], external_id)
    assert workspace[0].marking.get_document(workspace[2], doc["id"])["state"] == "unknown"


def test_restart_after_write_boundary_retains_bytes_and_target_locks(workspace):
    state, _, seller, connection, provider, *_ = workspace
    adapter = configure_suz(workspace)
    doc = prepare_order(workspace)
    job = state.marking.enqueue_document(seller, doc["id"])
    assert state.operations.claim()["id"] == job["id"]
    context = connection_context(state.connections.get(seller, connection["id"]))
    wire = adapter.prepare_wire(context, doc)
    state.marking._state(
        seller, doc["id"], {"state": "submitting"}, expected={"prepared"}, wire=wire
    )
    before = len(provider.calls)
    state.operations.recover_interrupted()
    recovered = state.marking.get_document(seller, doc["id"], internal=True)
    assert recovered["state"] == "unknown" and recovered["wire"] == wire
    assert state.operations.get(seller, job["id"])["status"] == "interrupted"
    assert len(provider.calls) == before
    with state.database.connection() as db:
        assert (
            db.execute(
                "SELECT count(*) FROM marking_targets WHERE document_id=?", (doc["id"],)
            ).fetchone()[0]
            == 1
        )
    another = prepare_order(workspace, key="different-task")
    with pytest.raises(Conflict):
        state.marking.enqueue_document(seller, another["id"])
    assert not state.operations.run_once()


def test_restart_before_boundary_releases_targets_and_preserves_reviewable_draft(workspace):
    state, _, seller, *_ = workspace
    doc = edit(workspace)
    job = state.marking.enqueue_document(seller, doc["id"])
    assert state.operations.claim()["id"] == job["id"]
    state.operations.recover_interrupted()
    assert state.marking.get_document(seller, doc["id"])["state"] == "prepared"
    with state.database.connection() as db:
        assert (
            db.execute(
                "SELECT count(*) FROM marking_targets WHERE document_id=?", (doc["id"],)
            ).fetchone()[0]
            == 0
        )
    assert state.marking.enqueue_document(seller, doc["id"])["status"] == "queued"


def test_accepted_requests_poll_after_restart_without_replaying_submission(workspace):
    state, _, seller, *_ = workspace
    configure_suz(workspace)
    accepted, _ = send(workspace, prepare_order(workspace))
    state.operations.recover_interrupted()
    with state.database.connection() as db:
        db.execute(
            "UPDATE marking_documents SET next_check_at='2000-01-01T00:00:00Z' WHERE id=?",
            (accepted["id"],),
        )
    state.marking.queue_due()
    state.marking.queue_due()
    jobs = state.operations.list(seller)
    polls = [v for v in jobs if v["operation_key"] == "document.poll" and v["status"] == "queued"]
    assert len(polls) == 1
    assert state.operations.run_once()
    assert state.marking.get_document(seller, accepted["id"])["state"] == "succeeded"
    assert len([v for v in workspace[4].calls if v[1] == "/api/v3/order"]) == 1


def test_post_ack_result_failure_preserves_external_id_and_saved_code_block(workspace):
    state, _, seller, connection, provider, *_ = workspace
    order = ready_order(workspace)
    doc = prepare_codes(workspace, order)
    original = state.operations.result_handler

    def fail_result(db, job, result):
        original(db, job, result)
        raise RuntimeError("local_result_commit_failed")

    state.operations.result_handler = fail_result
    final, job = send(workspace, doc)
    assert job["status"] == "failed" and final["state"] == "succeeded"
    assert final["external_id"] in provider.blocks
    assert (
        state.marking.code_export(seller, connection["id"], final["external_id"])["codes"]
        == provider.blocks[final["external_id"]]["codes"]
    )
    assert len(state.marking.codes(seller, connection["id"])["items"]) == 2
    state.operations.recover_interrupted()
    assert state.marking.get_document(seller, doc["id"])["state"] == "succeeded"
    with pytest.raises(Conflict):
        state.marking.enqueue_document(seller, doc["id"])


def test_incomplete_application_holds_same_cis_across_different_document_types(workspace):
    state, _, seller, connection, provider, *_ = workspace
    issued_codes(workspace)
    application = prepare_application(workspace)
    provider.write_failure = "timeout"
    unknown, _ = send(workspace, application)
    assert unknown["state"] == "unknown"
    provider.write_failure = None
    codes = [v["code"] for v in state.marking.codes(seller, connection["id"])["items"]]
    payload = true_payload(workspace)
    payload["document"]["products"] = [{"uit_code": v} for v in codes]
    introduction = state.marking.prepare(seller, connection["id"], "true", payload)
    with pytest.raises(Conflict):
        state.marking.enqueue_document(seller, introduction["id"])


def test_unknown_true_document_requires_matching_body_sender_group_and_type(workspace):
    state, _, seller, connection, provider, *_ = workspace
    payload = true_payload(workspace)
    doc = state.marking.prepare(seller, connection["id"], "true", payload)
    provider.write_failure = "timeout"
    send(workspace, doc)
    external_id = next(iter(provider.documents))
    original = copy.deepcopy(provider.documents[external_id])
    for fields in [
        {"senderInn": "999999999999"},
        {"type": "LK_RECEIPT"},
        {"productGroup": ["other-group"]},
        {"productGroup": "account-group"},
        {"content": json.dumps({"products": []})},
    ]:
        provider.documents[external_id] = {**original, **fields}
        with pytest.raises(InvalidInput):
            state.marking.reconcile_document(seller, doc["id"], external_id)
        assert state.marking.get_document(seller, doc["id"])["state"] == "unknown"
    provider.documents[external_id] = original
    final = state.marking.reconcile_document(seller, doc["id"], external_id)
    assert final["state"] == "succeeded" and final["external_id"] == external_id
    assert len([v for v in provider.calls if v[1].endswith("/lk/documents/create")]) == 1


def test_signature_and_tokens_are_redacted_from_persisted_remote_errors(workspace):
    configure_suz(workspace)
    doc = prepare_order(workspace)
    provider = workspace[4]
    original = provider.request
    from fbe_flow.integrations.chz.http import RemoteError

    def echoed(method, url, **kwargs):
        if url.endswith("/api/v3/order"):
            signature = kwargs["headers"]["X-Signature"]
            raise RemoteError(
                422,
                "http_422",
                {
                    "signature": signature,
                    "token": "suz-client-secret",
                    "message": f"rejected {signature} suz-client-secret",
                },
            )
        return original(method, url, **kwargs)

    provider.request = echoed
    final, job = send(workspace, doc)
    exposed = json.dumps([final, job], ensure_ascii=False)
    assert final["state"] == "rejected"
    assert (
        "suz-client-secret" not in exposed
        and base64.b64encode(b"test-signature").decode() not in exposed
    )


def test_database_foreign_keys_prevent_cross_seller_document_and_code_links(workspace):
    state, _, seller, connection, provider, vault, *_ = workspace
    _, doc = issued_codes(workspace)
    other = state.sellers.create("Other")["id"]
    ref = vault.put(other, {"true_token": "private-token"})
    foreign = state.connections.create(
        other, "chz", "Other", {"inn": provider.inn, "credential_ref": ref, "certificate": "A" * 40}
    )
    statements = [
        (
            "INSERT INTO marking_targets VALUES (?,?,?,?)",
            (other, foreign["id"], "cis:test", doc["id"]),
        ),
        (
            "INSERT INTO marking_events(seller_id,document_id,state,data_json) VALUES (?,?,?,?)",
            (other, doc["id"], "test", "{}"),
        ),
        (
            "INSERT INTO marking_code_blocks VALUES (?,?,?,?,?,?,?,?)",
            (str(uuid4()), other, foreign["id"], "order", "gtin", doc["id"], "[]", "now"),
        ),
        (
            "INSERT INTO marking_codes(id,seller_id,connection_id,code,block_id) "
            "VALUES (?,?,?,?,?)",
            (str(uuid4()), other, foreign["id"], "foreign", doc["external_id"]),
        ),
    ]
    for sql, args in statements:
        with pytest.raises(sqlite3.IntegrityError), state.database.connection() as db:
            db.execute(sql, args)


def test_unknown_nk_edit_reconciles_current_values_and_refreshes_original_uuid(workspace):
    state, _, seller, connection, provider, *_ = workspace
    doc = edit(workspace)
    before = state.marking.products(seller, connection["id"])["items"][0]["id"]
    provider.write_failure = "timeout"
    unknown, _ = send(workspace, doc)
    assert unknown["state"] == "unknown"
    final = state.marking.reconcile_document(seller, doc["id"], None)
    assert final["state"] == "succeeded" and final["external_status"] == "verified_current_state"
    card = state.marking.products(seller, connection["id"])["items"][0]
    assert card["id"] == before
    assert card["attributes"]["source"]["good_attrs"][0]["attr_value"] == "После изменения"
    assert len([v for v in provider.calls if v[1].endswith("/feed")]) == 1


def test_unknown_nk_moderation_cannot_be_proved_by_values_alone(workspace):
    doc = edit(workspace, moderation=True)
    workspace[4].write_failure = "timeout"
    send(workspace, doc)
    with pytest.raises(Conflict):
        workspace[0].marking.reconcile_document(workspace[2], doc["id"], None)
    assert workspace[0].marking.get_document(workspace[2], doc["id"])["state"] == "unknown"


def test_unknown_nk_publication_checks_signed_content_and_refreshes_source(workspace):
    state, _, seller, connection, provider, *_ = workspace
    from .test_chz_workflows import sync_ids

    ids = sync_ids(workspace)
    doc = state.marking.prepare(seller, connection["id"], "sign", {"product_ids": ids})
    provider.write_failure = "timeout"
    send(workspace, doc)
    final = state.marking.reconcile_document(seller, doc["id"], None)
    assert final["state"] == "succeeded"
    source = state.marking.products(seller, connection["id"])["items"][0]
    assert source["id"] == ids[0] and source["attributes"]["source"]["good_status"] == "published"


def test_cancel_prepared_document_allows_new_review_and_never_sends(workspace):
    doc = edit(workspace)
    final = workspace[0].marking.cancel_document(workspace[2], doc["id"])
    assert final["state"] == "cancelled"
    replacement = edit(workspace)
    assert replacement["id"] != doc["id"] and not workspace[4].feeds
