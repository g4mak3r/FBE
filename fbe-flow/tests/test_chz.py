import json
import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest

from fbe_flow.core.credentials import WindowsVault, credential_owner
from fbe_flow.core.errors import Conflict, InvalidInput, NotFound
from fbe_flow.core.models import ConnectionContext
from fbe_flow.integrations.chz.adapter import ChzConfig
from fbe_flow.integrations.chz.http import RemoteError, redact
from fbe_flow.modules.connections import connection_context

from .chz_fixtures import card


def sync(state, seller, connection, force=False):
    state.marking.start_sync(seller, connection["id"], force)
    while state.operations.run_once():
        pass
    return state.marking.products(seller, connection["id"], limit=200)["items"]


def test_sync_pagination_is_durable_and_etags_preserve_record_ids(workspace):
    state, _, seller, connection, provider, _, _ = workspace
    provider.cards = {v: card(v) for v in range(1, 103)}
    state.marking.start_sync(seller, connection["id"])
    assert state.operations.run_once()
    assert len(state.marking.products(seller, connection["id"])["items"]) == 100
    assert sum(v["status"] == "queued" for v in state.operations.list(seller)) == 1
    state.operations.recover_interrupted()
    assert state.operations.run_once()
    first = state.marking.products(seller, connection["id"], limit=200)
    assert first["total"] == 102
    count = len([v for v in provider.calls if v[1].endswith("feed-product")])
    second = sync(state, seller, connection)
    assert [r["id"] for r in first["items"]] == [r["id"] for r in second]
    assert len([v for v in provider.calls if v[1].endswith("feed-product")]) == count
    assert state.marking.overview(seller, connection["id"])["snapshots"]["sync"]["value"][
        "complete"
    ]
    assert all(
        c[2]["owner_inn"] == provider.inn for c in provider.calls if c[1].endswith("etagslist")
    )
    assert all(c[0] == "GET" or c[1].endswith("auth/simpleSignIn") for c in provider.calls)


def test_changed_cards_update_and_force_refresh_bypasses_etags(workspace):
    state, _, seller, connection, provider, _, _ = workspace
    before = sync(state, seller, connection)[0]
    provider.cards[1]["good_name"] = "Changed title"
    after = sync(state, seller, connection)[0]
    assert after["title"] == "Changed title" and after["id"] == before["id"]
    count = len(provider.calls)
    sync(state, seller, connection, force=True)
    assert any(c[1].endswith("feed-product") for c in provider.calls[count:])


def test_duplicate_sync_is_rejected_under_concurrency(workspace):
    state, _, seller, connection, _, _, _ = workspace

    def start():
        try:
            state.marking.start_sync(seller, connection["id"])
            return "queued"
        except Conflict:
            return "duplicate"

    with ThreadPoolExecutor(max_workers=2) as executor:
        assert sorted(executor.map(lambda _: start(), range(2))) == ["duplicate", "queued"]


def test_empty_catalog_and_disappearing_card_keep_local_copy(workspace):
    state, _, seller, connection, provider, _, _ = workspace
    before = sync(state, seller, connection)[0]
    provider.cards.clear()
    after = sync(state, seller, connection)[0]
    assert after["id"] == before["id"] and after["present"] is False
    provider.cards[1] = card()
    assert sync(state, seller, connection)[0]["present"] is True


def test_temporarily_unreadable_card_is_visible_and_retried(workspace):
    state, _, seller, connection, provider, _, _ = workspace
    provider.unavailable.add(1)
    item = sync(state, seller, connection)[0]
    assert item["id"] is None and item["detail_available"] is False and item["present"] is True
    provider.unavailable.clear()
    assert sync(state, seller, connection)[0]["detail_available"] is True
    provider.cards[1]["good_name"] = "Not yet readable"
    provider.unavailable.add(1)
    assert sync(state, seller, connection)[0]["title"] == "External product 1"


@pytest.mark.parametrize(
    "invalid",
    [
        {"total": True},
        {"goods_count": 9},
        {"offset": 7},
        {"last_product_number": 0},
        {"goods": []},
        {"goods": [{"good_id": True, "etag": "x"}]},
        {"goods": [{"good_id": 1, "etag": "x"}, {"good_id": 1, "etag": "x"}]},
    ],
)
def test_malformed_page_fails_without_partial_records(workspace, invalid):
    state, _, seller, connection, provider, _, _ = workspace
    provider.page_override = invalid
    sync(state, seller, connection)
    assert state.marking.products(seller, connection["id"])["total"] == 0
    assert state.operations.list(seller)[0]["status"] == "failed"
    assert "last_sync" not in state.marking.overview(seller, connection["id"])["snapshots"]


def test_catalog_change_between_pages_does_not_claim_complete(workspace):
    state, _, seller, connection, provider, _, _ = workspace
    provider.cards = {v: card(v) for v in range(1, 103)}
    state.marking.start_sync(seller, connection["id"])
    state.operations.run_once()
    provider.cards[103] = card(103)
    state.operations.run_once()
    value = state.marking.overview(seller, connection["id"])["snapshots"]["sync"]["value"]
    assert value["state"] == "failed" and value["error"] == "nk_catalog_changed"
    assert all(p["present"] for p in state.marking.products(seller, connection["id"])["items"])


def test_lookup_does_not_overwrite_own_card_or_infer_identity(workspace):
    state, _, seller, connection, _, _, _ = workspace
    original = sync(state, seller, connection)[0]
    job = state.operations.enqueue(
        seller, connection["id"], "nk.lookup", {"gtin": "00000000000001"}
    )
    state.operations.run_once()
    assert state.operations.get(seller, job["id"])["result"]["counts"] is None
    assert state.marking.products(seller, connection["id"])["items"][0] == original
    assert (
        state.marking.overview(seller, connection["id"])["snapshots"]["lookup"]["value"][0][
            "good_id"
        ]
        == 1
    )


def test_two_sellers_have_independent_records_snapshots_and_credentials(workspace):
    state, client, seller, connection, provider, vault, _ = workspace
    sync(state, seller, connection)
    other = state.sellers.create("Other")["id"]
    reference = vault.put(other, {"true_token": "second-secret"})
    second = state.connections.create(
        other, "chz", "Other", {"inn": provider.inn, "credential_ref": reference}
    )
    copies = sync(state, other, second)
    assert copies[0]["id"] != state.marking.products(seller, connection["id"])["items"][0]["id"]
    for path in ("overview", "products"):
        assert (
            client.get(f"/api/sellers/{other}/marking/{connection['id']}/{path}").status_code == 404
        )
    with pytest.raises(NotFound):
        state.marking.start_sync(other, connection["id"])
    with pytest.raises(InvalidInput):
        state.connections.create(other, "chz", "Forged", connection["config"])
    with pytest.raises(InvalidInput):
        state.registry.get("chz").execute(
            ConnectionContext(
                seller_id=other,
                connection_id=connection["id"],
                external_account_id=provider.inn,
                config=connection["config"],
            ),
            "nk.references",
            {},
        )
    with state.database.connection() as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO marking_snapshots VALUES (?,?,?,?,?)",
                (other, connection["id"], "x", "{}", "2026-10-01"),
            )


def test_statuses_categories_and_identifiers_come_from_provider(workspace):
    state, _, seller, connection, provider, _, _ = workspace
    provider.cards[1]["good_status"] = "new-external-status"
    provider.cards[1]["good_detailed_status"] = ["new-external-status"]
    provider.cards[1]["identified_by"].append({"type": "external-key", "value": "data-key"})
    item = sync(state, seller, connection)[0]
    assert item["identifiers"]["external-key"] == ["data-key"]
    assert item["category"]["categories"] == provider.cards[1]["categories"]
    assert item["attributes"]["source"]["good_status"] == "new-external-status"


def test_account_and_references_are_persisted_without_secret_echo(workspace):
    state, _, seller, connection, provider, _, _ = workspace
    state.operations.enqueue(seller, connection["id"], "nk.references", {})
    state.operations.run_once()
    data = state.marking.overview(seller, connection["id"])["snapshots"]
    assert data["account"]["value"]["productGroups"] == [provider.group]
    assert data["categories"]["value"][0]["cat_id"] == 123456
    provider.fail_path = "categories"
    state.operations.enqueue(seller, connection["id"], "nk.references", {})
    state.operations.run_once()
    assert state.operations.list(seller)[0]["error_code"] == "http_429"
    assert "private-token" not in json.dumps(state.operations.list(seller))


def test_public_participant_information_cannot_verify_connection(workspace):
    state, _, _, connection, provider, _, _ = workspace
    provider.private_account = False
    with pytest.raises(InvalidInput):
        state.registry.get("chz").describe(connection["config"])


def test_auth_uses_attached_challenge_signature_and_scoped_token_cache(workspace):
    state, _, _, connection, provider, _, signer = workspace
    state.registry.get("chz").describe(connection["config"])
    assert len(signer.calls) == 1 and signer.calls[0] == (b"CHALLENGE-UTF8", "A" * 40, False)
    signin = next(c for c in provider.calls if c[1].endswith("simpleSignIn"))
    assert signin[3]["unitedToken"] is True and signin[3]["inn"] == provider.inn
    assert all(h["Authorization"] == "Bearer renewed-token" for h in provider.headers if h)


@pytest.mark.parametrize(
    "response",
    [
        None,
        {},
        {"uuidToken": 1},
        {
            "uuidToken": "expired",
            "expireDate": "2001-01-01T00:00:00Z",
        },
    ],
)
def test_invalid_auth_response_is_rejected(workspace, response):
    state, _, _, connection, provider, _, _ = workspace
    adapter = state.registry.get("chz")
    adapter._tokens.clear()
    provider.auth_response = response
    with pytest.raises(RemoteError):
        adapter.account(ChzConfig.model_validate(connection["config"]))


def test_mutations_require_a_prepared_document_and_do_not_expose_credentials(workspace):
    state, client, seller, connection, _, _, _ = workspace
    assert {v["key"] for v in connection["operations"]} == {
        "nk.sync",
        "nk.lookup",
        "nk.references",
        "account.refresh",
        "codes.check",
        "suz.status",
        "suz.blocks",
        "suz.receipt",
        "document.submit",
        "document.poll",
    }
    assert (
        client.post(
            f"/api/sellers/{seller}/marking/{connection['id']}/documents", json={}
        ).status_code
        == 405
    )
    html = client.get(f"/sellers/{seller}/marking").text
    assert "Национальный каталог" in html and "Массовое изменение" in html
    assert "credential_ref" not in html and "private-token" not in html
    response = client.get(f"/api/sellers/{seller}/connections/{connection['id']}").text
    assert "credential_ref" not in response and "config" not in response
    with pytest.raises(InvalidInput):
        state.registry.get("chz").execute(connection_context(connection), "document.submit", {})


def test_search_treats_percent_and_underscore_as_literals(workspace):
    state, _, seller, connection, provider, _, _ = workspace
    provider.cards[1]["good_name"] = "Product 20%_one"
    sync(state, seller, connection)
    assert state.marking.products(seller, connection["id"], search="%_")["total"] == 1
    assert state.marking.products(seller, connection["id"], search="missing")["total"] == 0


def test_redaction_covers_nested_diagnostics_and_secret_keys():
    value = json.dumps(
        redact(
            {"message": "private-token", "nested": [{"token": "leak", "error": "private-token"}]},
            ["private-token"],
        )
    )
    assert "private-token" not in value and "leak" not in value


@pytest.mark.parametrize("reference", ["../x", "invalid", "not:a:reference", "0000:0000"])
def test_credential_reference_rejects_paths_and_invalid_owner(reference):
    with pytest.raises(InvalidInput):
        credential_owner(reference)


@pytest.mark.skipif(os.name != "nt", reason="Exercises Windows DPAPI CurrentUser")
def test_windows_vault_encrypts_and_restricts_seller(tmp_path):
    vault = WindowsVault(tmp_path)
    seller, other = str(uuid4()), str(uuid4())
    reference = vault.put(seller, {"true_token": "dpapi-secret-test"})
    assert vault.get(seller, reference) == {"true_token": "dpapi-secret-test"}
    assert b"dpapi-secret-test" not in next(tmp_path.rglob("*.dpapi")).read_bytes()
    with pytest.raises(InvalidInput):
        vault.get(other, reference)
    vault.delete(seller, reference)
    with pytest.raises(InvalidInput):
        vault.get(seller, reference)


def test_production_and_sandbox_accounts_do_not_collide(workspace):
    state, _, seller, connection, provider, vault, _ = workspace
    reference = vault.put(seller, {"true_token": "production-test-token"})
    production = state.connections.create(
        seller,
        "chz",
        "Production",
        {
            "inn": provider.inn,
            "environment": "production",
            "credential_ref": reference,
        },
    )
    assert connection["external_account_id"] == f"sandbox:{provider.inn}"
    assert production["external_account_id"] == f"production:{provider.inn}"
    first = sync(state, seller, connection)[0]
    second = sync(state, seller, production)[0]
    assert first["id"] != second["id"]
