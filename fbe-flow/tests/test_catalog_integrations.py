"""Read-only protocol acceptance for assortment, source schemas and ChZ."""

import copy
from urllib.parse import urlparse
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from fbe_flow.app import create_app
from fbe_flow.config import AppConfig
from fbe_flow.core.errors import InvalidInput
from fbe_flow.core.integrations import AdapterRegistry
from fbe_flow.core.models import ConnectionContext, NormalizedBatch
from fbe_flow.integrations.catalog import variants
from fbe_flow.integrations.chz.adapter import ChzAdapter
from fbe_flow.integrations.chz.http import Reply
from fbe_flow.integrations.ozon.adapter import OzonAdapter
from fbe_flow.integrations.wb.adapter import WbAdapter
from fbe_flow.modules.catalog import Catalog
from fbe_flow.modules.connections import Connections
from fbe_flow.modules.records import Records
from fbe_flow.modules.sellers import Sellers

from .chz_fixtures import FixtureSigner, MemoryVault
from .chz_workflow_fixtures import WorkflowProvider
from .test_catalog import data, gtin, rule
from .wb_fixtures import token


def fixture_reply(data):
    return Reply(200, data, {})


class CatalogProvider(WorkflowProvider):
    def __init__(self):
        super().__init__()
        self.cards[1]["identified_by"] = [{"type": "gtin", "value": gtin()}]
        self.cards[1]["good_attrs"] = [
            {"attr_id": 1, "attr_name": "Код ТН ВЭД", "attr_value": "3303001000"},
            {"attr_id": 2, "attr_name": "Код ОКПД2", "attr_value": "20.42.11"},
        ]
        self.registry_group = self.group
        self.ready_group = self.group
        self.ready = True

    def request(self, method, url, *, params=None, headers=None, body=None):
        path = urlparse(url).path
        if path.endswith("/tn-ved/search"):
            self.calls.append((method, path, copy.deepcopy(params), copy.deepcopy(body)))
            return fixture_reply(
                {
                    "tnveds": [{"tnved": "3303001000", "pg": self.registry_group}],
                    "total": 1,
                    "last": True,
                },
            )
        if path.endswith("/product/info"):
            self.calls.append((method, path, copy.deepcopy(params), copy.deepcopy(body)))
            return fixture_reply(
                {
                    "results": [{"gtin": gtin(), "productGroup": self.ready_group, "permits": {}}]
                    if self.ready
                    else []
                },
            )
        return super().request(method, url, params=params, headers=headers, body=body)


@pytest.fixture
def checked_catalog(tmp_path):
    vault, signer, provider = MemoryVault(), FixtureSigner(), CatalogProvider()
    adapter = ChzAdapter(vault, signer, provider)
    app = create_app(
        AppConfig(tmp_path, worker_enabled=False), [adapter], vault=vault, signer=signer
    )
    with TestClient(app, base_url="http://localhost", headers={"X-FBE-Flow": "1"}) as client:
        seller = app.state.sellers.create("Проверка")["id"]
        reference = vault.put(seller, {"true_token": "catalog-fixture-token"})
        connection = app.state.connections.create(
            seller,
            "chz",
            "ЧЗ",
            {"inn": provider.inn, "credential_ref": reference, "certificate": "A" * 40},
        )
        product = app.state.catalog.save_product(
            seller, data(okpd2="20.42.11", product_group=provider.group)
        )
        app.state.catalog.save_rule(seller, rule(product_group=provider.group))
        yield app.state, client, seller, connection, product, provider


def test_check_reads_private_account_registry_and_exact_gtin_without_external_mutation(
    checked_catalog,
):
    state, _, seller, connection, product, provider = checked_catalog
    start = len(provider.calls)
    result = state.catalog.check(seller, product["id"], connection["id"])
    assert result["state"] == "matched" and result["automatic_changes"] is False
    calls = provider.calls[start:]
    assert {v[1].rsplit("/", 1)[-1] for v in calls} >= {"participants", "search", "info", "product"}
    assert not any(
        v[1].endswith(("/feed", "/order", "/utilisation", "/lk/documents/create")) for v in calls
    )
    assert state.catalog.product(seller, product["id"])["revision"] == product["revision"]
    assert not state.catalog.detail(seller, product["id"])["check"]["stale"]
    state.catalog.save_product(
        seller,
        data(title="Изменение", okpd2="20.42.11", product_group=provider.group),
        product["id"],
        product["revision"],
    )
    assert state.catalog.detail(seller, product["id"])["check"]["stale"]


@pytest.mark.parametrize(
    "case,issue",
    [
        ("group", "nk_group_mismatch"),
        ("registry", "tnved_group_mismatch"),
        ("tnved", "nk_tnved_mismatch"),
        ("missing", "nk_tnved_unconfirmed"),
        ("ready", "nk_group_unconfirmed"),
    ],
)
def test_live_mismatches_require_review_and_preserve_product(checked_catalog, case, issue):
    state, _, seller, connection, product, provider = checked_catalog
    if case == "group":
        provider.ready_group = "other"
    elif case == "registry":
        provider.registry_group = "other"
    elif case == "tnved":
        provider.cards[1]["good_attrs"][0]["attr_value"] = "3303009000"
    elif case == "missing":
        provider.cards[1]["good_attrs"] = []
    else:
        provider.ready = False
    before = state.catalog.product(seller, product["id"])
    result = state.catalog.check(seller, product["id"], connection["id"])
    assert result["state"] == "needs_review"
    assert issue in {i["code"] for i in result["issues"]}
    assert state.catalog.product(seller, product["id"]) == before


def test_registry_group_does_not_claim_statutory_marking_obligation(checked_catalog):
    state, _, seller, connection, product, _ = checked_catalog
    saved = state.catalog.rules(seller)[0]
    state.catalog.save_rule(
        seller,
        rule(product_group=saved["product_group"], enabled=False),
        saved["id"],
        saved["revision"],
    )
    result = state.catalog.check(seller, product["id"], connection["id"])
    assert result["suggested_group"] == saved["product_group"]
    assert result["classification"]["marking_required"] is None
    assert result["state"] == "needs_review"


def test_changed_rules_or_check_date_invalidate_previous_result(checked_catalog):
    state, _, seller, connection, product, provider = checked_catalog
    state.catalog.check(seller, product["id"], connection["id"])
    saved = state.catalog.rules(seller)[0]
    state.catalog.save_rule(
        seller,
        rule(product_group=provider.group, source_note="Уточнение"),
        saved["id"],
        saved["revision"],
    )
    assert state.catalog.detail(seller, product["id"])["check"]["stale"]
    state.catalog.check(seller, product["id"], connection["id"])
    with state.database.connection() as conn:
        conn.execute("UPDATE catalog_checks SET created_at='2020-01-01T00:00:00.000Z'")
    assert state.catalog.detail(seller, product["id"])["check"]["stale"]


def test_wb_sizes_keep_distinct_identity_and_convert_dimensions_without_guessing_status():
    record = {
        "title": "Источник",
        "sku": "parent",
        "external_id": "1",
        "identifiers": {},
        "attributes": {
            "source": {
                "kizMarked": True,
                "dimensions": {"length": 12.5, "width": 5, "height": 2, "weightBrutto": 0.25},
                "sizes": [
                    {"chrtID": 10, "techSize": "S", "skus": [gtin()]},
                    {"chrtID": 20, "techSize": "M", "skus": [gtin(2)]},
                ],
            }
        },
    }
    result = variants(record, "wb")
    assert [v["key"] for v in result] == ["10", "20"]
    assert result[0]["fields"]["sku"] != result[1]["fields"]["sku"]
    assert result[0]["fields"]["length_mm"] == "125.0"
    assert result[0]["fields"]["gross_weight_g"] == "250.00"
    assert result[0]["fields"]["marking_attestation"] is True
    assert "external_status" not in result[0]["fields"]


class SchemaTransport:
    def __init__(self):
        self.calls = []

    def request(self, method, url, *, params=None, headers=None, body=None):
        path = urlparse(url).path
        self.calls.append((method, path, copy.deepcopy(params), copy.deepcopy(body)))
        if path.endswith("/attribute"):
            result = {
                "result": [{"id": 7, "name": "Цвет", "is_required": True, "dictionary_id": 12}]
            }
        elif path.endswith("/attribute/values"):
            assert body["limit"] == 100
            result = {
                "result": [{"id": body["last_value_id"] + 1, "value": "Синий"}],
                "has_next": True,
            }
        elif "/object/charcs/" in path:
            result = {"data": [{"charcID": 7, "name": "Цвет", "required": True}]}
        elif path.endswith("/directory/tnved"):
            result = {"data": [{"tnved": "3303001000", "isKiz": True}]}
        elif path.endswith("/product/info/attributes"):
            result = {
                "result": [
                    {
                        "id": 1,
                        "name": "После чтения",
                        "depth": 125,
                        "width": 40,
                        "height": 20,
                        "dimension_unit": "mm",
                        "weight": 250,
                        "weight_unit": "g",
                        "attributes": [
                            {"id": 7, "values": [{"dictionary_value_id": 1, "value": "Синий"}]}
                        ],
                    }
                ]
            }
        else:
            raise AssertionError(path)
        return fixture_reply(result)


def schema_adapter(channel, monkeypatch):
    vault, transport = MemoryVault(), SchemaTransport()
    seller = str(uuid4())
    ref = vault.put(seller, {channel + "_token": token(mask=18 | (1 << 30), sid="100") if channel == "wb" else "fixture"})
    adapter = (WbAdapter if channel == "wb" else OzonAdapter)(
        vault, transport, pause=lambda _: None
    )
    config = {"credential_ref": ref, "account_id": "100", "tin": "123456789012", "read_only": True}
    monkeypatch.setattr(adapter, "account", lambda _: {"account_id": "100", "tin": config["tin"]})
    context = ConnectionContext(
        seller_id=seller, connection_id="schema", external_account_id="100", config=config
    )
    return adapter, context, transport


@pytest.mark.parametrize("channel,category", [("wb", "12"), ("ozon", "12:34")])
def test_live_schema_uses_exact_category_and_read_only_protocol(monkeypatch, channel, category):
    adapter, context, transport = schema_adapter(channel, monkeypatch)
    result = adapter.catalog_schema(context, category)
    assert result["attributes"]
    assert result["category"] == category
    if channel == "wb":
        assert transport.calls[-1][2] == {"subjectID": 12}
    else:
        assert transport.calls[-1][3] == {
            "description_category_id": 12,
            "type_id": 34,
            "language": "DEFAULT",
        }
        response = adapter.catalog_dictionary(context, category, 7, 9)
        assert response["result"][0]["id"] == 10
        assert transport.calls[-1][3]["last_value_id"] == 9
        with pytest.raises(InvalidInput):
            adapter.catalog_dictionary(context, category, 999)
    assert not any(v[1].endswith(("/create", "/update", "/import")) for v in transport.calls)


def test_ozon_full_attribute_refresh_preserves_variant_and_canonical_trade_item(db, monkeypatch):
    adapter, context, transport = schema_adapter("ozon", monkeypatch)
    registry = AdapterRegistry([adapter])
    seller = Sellers(db).create("Ozon")["id"]
    reference = adapter.vault.put(seller, {"ozon_token": "fixture"})
    config = {**context.config, "credential_ref": reference}
    connections = Connections(db, registry)
    connection = connections.create(seller, "ozon", "Ozon", config)
    catalog = Catalog(db, connections, registry, None)
    original = adapter.product(
        {
            "id": 1,
            "name": "Исходный",
            "offer_id": "offer-1",
            "sku": 10001,
            "barcodes": [gtin()],
            "description_category_id": 12,
        }
    )
    records = Records(db)
    records.apply(seller, connection["id"], NormalizedBatch(products=(original,)))
    source = records.list(seller, "products")[0]
    p = catalog.adopt(seller, data(), source["id"], "10001")
    refreshed = catalog.refresh_source(seller, source["id"])
    assert refreshed["id"] == source["id"] and refreshed["attributes"]["variant"] == "10001"
    assert catalog.product(seller, p["id"])["title"] == "Товар 1"
    source_fields = catalog.sources(seller)["items"][0]["variants"][0]["fields"]
    assert source_fields["attributes"]["ozon:7"][0]["value"] == "Синий"
    assert source_fields["length_mm"] == "125"
    assert transport.calls[-1][3]["filter"] == {"product_id": ["1"], "visibility": "ALL"}


def test_existing_wb_and_ozon_workflows_enforce_active_canonical_binding(tmp_path):
    from fbe_flow.core.errors import Conflict
    from fbe_flow.modules.catalog import clean_product
    from .commerce_fixtures import application, seed_all

    app, chz, wb_api, _, _, _ = application(tmp_path)
    chz.cards[1]["identified_by"] = [{"type": "gtin", "value": gtin()}]
    wb_api.cards[1]["sizes"][0]["skus"] = [gtin()]
    with TestClient(app, base_url="http://localhost", headers={"X-FBE-Flow": "1"}) as client:
        seller, chz_connection, wb, ozon, _ = seed_all(app.state, client, chz, wb_api)
        state = app.state
        wb_product = next(p for p in state.fulfillment.records(seller, wb["id"], "products")["items"]
                          if p["external_id"] == "1")
        nk = next(p for p in state.marking.products(seller, chz_connection["id"])["items"]
                  if p["external_id"] == "1")
        canonical = state.catalog.adopt(seller, data(product_group=chz.group), wb_product["id"], "11")
        state.fulfillment.link(seller, wb["id"], {"product_id": wb_product["id"], "chrt_id": "11",
                              "chz_connection_id": chz_connection["id"], "chz_product_id": nk["id"],
                              "gtin": gtin(), "product_group": chz.group})
        wb_order = next(p for p in state.fulfillment.records(seller, wb["id"], "orders")["items"]
                        if p["external_id"] == "1001")
        assert state.fulfillment.order_link(seller, wb["id"], wb_order["id"])[1]["gtin"] == gtin()
        ozon_product = next(p for p in state.commerce.records(seller, ozon["id"], "products",
                                                             limit=200)["items"]
                            if p["external_id"] == "1")
        state.catalog.link(seller, canonical["id"], ozon_product["id"], "10001")
        state.commerce.link(seller, ozon["id"], {"product_id": ozon_product["id"],
                            "chz_connection_id": chz_connection["id"], "chz_product_id": nk["id"],
                            "gtin": gtin(), "product_group": chz.group})
        ozon_order = state.commerce.records(seller, ozon["id"], "orders")["items"][0]
        assert state.commerce.order_item(seller, ozon["id"], ozon_order["id"], "10001")[2]["gtin"] == gtin()
        state.catalog.save_product(seller, {**clean_product(canonical), "archived": True},
                                   canonical["id"], canonical["revision"])
        before = len(chz.calls)
        with pytest.raises(Conflict):
            state.fulfillment.order_link(seller, wb["id"], wb_order["id"])
        with pytest.raises(Conflict):
            state.commerce.order_item(seller, ozon["id"], ozon_order["id"], "10001")
        assert len(chz.calls) == before
