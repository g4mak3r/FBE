import base64
import json
from datetime import date

import pytest

from fbe_flow.core.errors import Conflict, InvalidInput, NotFound
from fbe_flow.modules.connections import connection_context

from .chz_fixtures import card


def sync_ids(workspace):
    state, _, seller, connection, *_ = workspace
    state.marking.start_sync(seller, connection["id"])
    while state.operations.run_once():
        pass
    return [v["id"] for v in state.marking.products(seller, connection["id"])["items"]]


def edit(workspace, *, patch=None, ids=None, moderation=False):
    state, _, seller, connection, *_ = workspace
    return state.marking.prepare(
        seller,
        connection["id"],
        "edit",
        {
            "product_ids": ids or sync_ids(workspace),
            "attributes": patch or [{"attr_id": "987654", "attr_value": "После изменения"}],
            "moderation": moderation,
        },
    )


def send(workspace, doc, *, poll=False):
    state, _, seller, *_ = workspace
    job = state.marking.enqueue_document(seller, doc["id"], poll=poll)
    assert state.operations.run_once()
    return state.marking.get_document(seller, doc["id"]), state.operations.get(seller, job["id"])


def cis_info(provider, code, status="APPLIED", **fields):
    provider.cises[code] = {
        "cisInfo": {
            "cis": code,
            "requestedCis": code,
            "gtin": code[2:16],
            "ownerInn": provider.inn,
            "status": status,
            "productGroup": provider.group,
            "packageType": "UNIT",
            **fields,
        }
    }


def true_payload(workspace, *, withdraw=False):
    provider = workspace[4]
    code = "010000000000000121SERIAL123"
    cis_info(provider, code, "INTRODUCED" if withdraw else "APPLIED")
    document = (
        {
            "inn": provider.inn,
            "action": "OTHER",
            "action_date": date.today().isoformat(),
            "withdrawal_type_other": "Использование",
            "document_type": "OTHER",
            "document_number": "1",
            "document_date": date.today().isoformat(),
            "products": [{"cis": code}],
        }
        if withdraw
        else {
            "participant_inn": provider.inn,
            "producer_inn": provider.inn,
            "owner_inn": provider.inn,
            "production_type": "OWN_PRODUCTION",
            "production_date": date.today().isoformat(),
            "products": [{"uit_code": code}],
        }
    )
    return {
        "type": "LK_RECEIPT" if withdraw else "LP_INTRODUCE_GOODS",
        "product_group": provider.group,
        "document": document,
    }


def test_edit_preview_does_not_write_and_completed_feed_refreshes_source(workspace):
    state, client, seller, connection, provider, *_ = workspace
    original_ids = sync_ids(workspace)
    doc = edit(workspace, ids=original_ids)
    assert doc["body"]["preview"][0]["changes"][0]["before"] == ["Before"]
    assert not provider.feeds
    response = client.get(f"/api/sellers/{seller}/marking/documents/{doc['id']}")
    assert response.status_code == 200 and "wire" not in response.json()
    accepted, job = send(workspace, doc)
    assert accepted["state"] == "accepted" and job["status"] == "succeeded"
    assert accepted["external_id"] == "1" and accepted["wire_saved"]
    final, job = send(workspace, doc, poll=True)
    assert final["state"] == "succeeded" and job["status"] == "succeeded"
    source = state.marking.products(seller, connection["id"])["items"][0]
    assert source["attributes"]["source"]["good_attrs"][0]["attr_value"] == "После изменения"
    assert source["id"] == original_ids[0]
    with pytest.raises(Conflict):
        state.marking.enqueue_document(seller, doc["id"])


def test_bulk_model_comes_from_categories_and_is_cached_per_category(workspace):
    state, client, seller, connection, provider, *_ = workspace
    provider.cards = {v: card(v) for v in range(1, 103)}
    ids = sync_ids(workspace)
    before = len(provider.calls)
    result = client.post(
        f"/api/sellers/{seller}/marking/{connection['id']}/edit-model",
        json={"product_ids": ids[:100]},
    )
    assert result.status_code == 200 and result.json()["attributes"][0]["attr_id"] == 987654
    assert sum(v[1].endswith("/attributes") for v in provider.calls[before:]) == 1
    doc = state.marking.prepare(
        seller,
        connection["id"],
        "edit",
        {"product_ids": ids[:100], "attributes": [{"attr_id": "987654", "attr_value": "After"}]},
    )
    assert len(doc["body"]["entries"]) == 100


@pytest.mark.parametrize(
    "change",
    [
        {"attr_id": "missing", "attr_value": "x"},
        {"attr_id": "888", "attr_value": "x"},
        {"attr_id": "987654", "attr_value": 1},
        {"attr_id": "987654", "attr_value": "x", "delete": "yes"},
        {"attr_id": "987654", "attr_value": ""},
        {"attr_id": "987654", "attr_value": "x", "unknown": True},
    ],
)
def test_invalid_changes_never_create_feed(workspace, change):
    with pytest.raises(InvalidInput):
        edit(workspace, patch=[change])
    assert not workspace[4].feeds


@pytest.mark.parametrize(
    "model", [{"attr_type": "b"}, {"attr_preset_only": True, "attr_preset": ["Allowed"]}]
)
def test_blocked_and_dictionary_attributes_are_validated(workspace, model):
    workspace[4].model[0].update(model)
    with pytest.raises(InvalidInput):
        edit(workspace)


def test_multiplicity_types_and_deletions_are_checked(workspace):
    provider = workspace[4]
    with pytest.raises(InvalidInput):
        edit(
            workspace,
            patch=[
                {"attr_id": "987654", "attr_value": "a"},
                {"attr_id": "987654", "attr_value": "b"},
            ],
        )
    provider.model[0].update(
        attr_multiplicity=True,
        attr_multiplicity_type="unique",
        attr_value_type=["type-a", "type-b"],
    )
    doc = edit(
        workspace,
        patch=[
            {"attr_id": "987654", "attr_value": "a", "attr_value_type": "type-a"},
            {"attr_id": "987654", "attr_value": "b", "attr_value_type": "type-b"},
        ],
    )
    assert len(doc["body"]["entries"][0]["good_attrs"]) == 2
    with pytest.raises(InvalidInput):
        edit(workspace, patch=[{"attr_id": "987654", "attr_value": "Absent", "delete": True}])


@pytest.mark.parametrize("status", ["moderation", "archived", "new-upstream-state"])
def test_unsupported_card_state_blocks_edit(workspace, status):
    workspace[4].cards[1] = card(status=status)
    with pytest.raises(InvalidInput):
        edit(workspace)


@pytest.mark.parametrize("missing", [False, True])
def test_stale_or_missing_cards_fail_before_write_boundary(workspace, missing):
    state, _, seller, _, provider, *_ = workspace
    doc = edit(workspace)
    if missing:
        provider.unavailable.add(1)
    else:
        provider.cards[1]["good_name"] = "Changed by another application"
    after, job = send(workspace, doc)
    assert after["state"] == "prepared" and not after["wire_saved"]
    assert job["status"] == "failed" and not provider.feeds
    with state.database.connection() as conn:
        assert (
            conn.execute(
                "SELECT count(*) FROM marking_targets WHERE seller_id=?", (seller,)
            ).fetchone()[0]
            == 0
        )


def test_feed_errors_and_unknown_status_are_not_reported_as_full_success(workspace):
    state, _, seller, _, provider, *_ = workspace
    provider.feed_errors = [{"id": 0, "good_id": 1, "message": "Invalid value"}]
    doc = edit(workspace)
    send(workspace, doc)
    provider.feeds["1"]["status"] = "UNKNOWN_NEW_STATUS"
    after, _ = send(workspace, doc, poll=True)
    assert after["state"] == "processing"
    provider.feeds["1"]["status"] = "Moderated"
    final, _ = send(workspace, doc, poll=True)
    assert final["state"] == "partial" and final["result"]["item"]
    with pytest.raises(Conflict):
        state.marking.retry_document(seller, doc["id"])


def test_publication_signs_exact_utf8_xml_and_preserves_partial_results(workspace):
    state, _, seller, connection, provider, _, signer = workspace
    provider.cards[1]["good_name"] = "Товар с кириллицей"
    provider.cards[2] = card(2)
    ids = sync_ids(workspace)
    doc = state.marking.prepare(seller, connection["id"], "sign", {"product_ids": ids})
    assert not any(detached for _, _, detached in signer.calls)
    provider.signature_errors = {2}
    after, job = send(workspace, doc)
    assert after["state"] == "partial" and job["status"] == "succeeded"
    assert after["result"]["signed"] == [1] and after["result"]["errors"][0]["goodId"] == 2
    wire = json.loads(
        next(
            raw
            for path, raw, _ in provider.raw_requests
            if path.endswith("/feed-product-sign-pkcs")
        )
    )
    signed = [raw for raw, _, detached in signer.calls if detached]
    assert [base64.b64decode(v["base64Xml"]) for v in wire] == signed
    assert signed[0].decode("utf-8") == doc["body"]["xmls"][0]["xml"]
    assert "wire" not in state.marking.get_document(seller, doc["id"])


def test_publication_missing_xml_and_changed_xml_block_submission(workspace):
    state, _, seller, connection, provider, *_ = workspace
    ids = sync_ids(workspace)
    doc = state.marking.prepare(seller, connection["id"], "sign", {"product_ids": ids})
    provider.cards[1]["good_attrs"][0]["attr_value"] = "Changed"
    after, job = send(workspace, doc)
    assert after["state"] == "prepared" and job["status"] == "failed"
    provider.unavailable.add(1)
    with pytest.raises(InvalidInput):
        state.marking.prepare(seller, connection["id"], "sign", {"product_ids": ids})


@pytest.mark.parametrize("withdraw", [False, True])
def test_true_document_preflight_exact_signature_and_processing(workspace, withdraw):
    state, _, seller, connection, provider, _, signer = workspace
    payload = true_payload(workspace, withdraw=withdraw)
    doc = state.marking.prepare(seller, connection["id"], "true", payload)
    assert not provider.documents
    accepted, job = send(workspace, doc)
    assert accepted["state"] == "accepted" and job["status"] == "succeeded"
    body = json.loads(
        next(raw for path, raw, _ in provider.raw_requests if path.endswith("/lk/documents/create"))
    )
    actual = base64.b64decode(body["product_document"])
    assert actual == [v[0] for v in signer.calls if v[2]][-1]
    assert json.loads(actual) == payload["document"]
    final, _ = send(workspace, doc, poll=True)
    assert final["state"] == "succeeded" and final["external_status"] == "CHECKED_OK"


@pytest.mark.parametrize(
    "field,value",
    [
        ("ownerInn", "999999999999"),
        ("status", "INTRODUCED"),
        ("productGroup", "another-group"),
        ("packageType", "GROUP"),
        ("statusEx", "IN_TRANSIT"),
    ],
)
def test_wrong_owner_status_group_or_package_blocks_introduction(workspace, field, value):
    state, _, seller, connection, provider, *_ = workspace
    payload = true_payload(workspace)
    code = payload["document"]["products"][0]["uit_code"]
    provider.cises[code]["cisInfo"][field] = value
    with pytest.raises(InvalidInput):
        state.marking.prepare(seller, connection["id"], "true", payload)
    assert not provider.documents


def test_changed_code_status_before_send_keeps_reviewable_draft(workspace):
    state, _, seller, connection, provider, *_ = workspace
    payload = true_payload(workspace)
    doc = state.marking.prepare(seller, connection["id"], "true", payload)
    provider.cises[payload["document"]["products"][0]["uit_code"]]["cisInfo"]["status"] = (
        "INTRODUCED"
    )
    after, job = send(workspace, doc)
    assert after["state"] == "prepared" and job["status"] == "failed" and not provider.documents


@pytest.mark.parametrize(
    "status,expected",
    [
        ("CHECKED_NOT_OK", "rejected"),
        ("PARSE_ERROR", "rejected"),
        ("PROCESSING", "processing"),
        ("FUTURE_STATUS", "processing"),
    ],
)
def test_true_external_status_mapping_is_conservative(workspace, status, expected):
    state, _, seller, connection, provider, *_ = workspace
    doc = state.marking.prepare(seller, connection["id"], "true", true_payload(workspace))
    after, _ = send(workspace, doc)
    provider.documents[after["external_id"]]["status"] = status
    result, _ = send(workspace, doc, poll=True)
    assert result["state"] == expected


def test_raw_operation_cannot_bypass_document_or_account_binding(workspace):
    state, _, seller, connection, provider, *_ = workspace
    job = state.operations.enqueue(
        seller, connection["id"], "document.submit", {"document_id": "missing"}
    )
    state.operations.run_once()
    assert state.operations.get(seller, job["id"])["status"] == "failed"
    altered = connection_context(
        {**connection, "external_account_id": "production:" + provider.inn}
    )
    with pytest.raises(InvalidInput):
        state.registry.get("chz").config(altered)


def test_foreign_products_documents_and_codes_return_not_found(workspace):
    state, client, seller, connection, *_ = workspace
    doc = edit(workspace)
    other = state.sellers.create("Other")["id"]
    root = f"/api/sellers/{other}/marking/documents/{doc['id']}"
    assert client.get(root).status_code == 404
    assert client.post(root + "/submit", json={}).status_code == 404
    assert client.post(root + "/cancel", json={}).status_code == 404
    with pytest.raises(NotFound):
        state.marking._selected_ids(other, connection["id"], doc["body"]["targets"])


def test_document_http_flow_prepares_then_submits_explicitly(workspace):
    state, client, seller, connection, provider, *_ = workspace
    ids = sync_ids(workspace)
    root = f"/api/sellers/{seller}/marking"
    response = client.post(
        f"{root}/{connection['id']}/prepare",
        json={
            "action": "edit",
            "payload": {
                "product_ids": ids,
                "attributes": [{"attr_id": "987654", "attr_value": "HTTP value"}],
            },
        },
    )
    assert response.status_code == 201 and not provider.feeds
    doc = response.json()
    assert client.post(f"{root}/documents/{doc['id']}/submit", json={}).status_code == 202
    assert client.post(f"{root}/documents/{doc['id']}/submit", json={}).status_code == 409
    assert state.marking.get_document(seller, doc["id"])["active_operation"]
    state.operations.run_once()
    assert client.get(f"{root}/documents/{doc['id']}").json()["state"] == "accepted"
    assert client.get(f"{root}/{connection['id']}/documents").json()[0]["id"] == doc["id"]
