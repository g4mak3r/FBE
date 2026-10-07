"""Loopback QA app with isolated protocol doubles and temporary data."""

import copy
import json
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlparse
from uuid import uuid4

import uvicorn

from fbe_flow.app import create_app
from fbe_flow.config import AppConfig
from fbe_flow.core.models import AccountInfo, NormalizedBatch, Order, Product, Supply, Warehouse
from fbe_flow.integrations.catalog import variants
from fbe_flow.integrations.chz.adapter import ChzAdapter
from fbe_flow.integrations.kit import KitAdapter
from fbe_flow.integrations.ozon.adapter import OzonAdapter
from fbe_flow.integrations.wb.adapter import WbAdapter
from tests.chz_fixtures import FixtureSigner, MemoryVault
from tests.commerce_fixtures import KitProvider, uid
from tests.conftest import FixtureAdapter
from tests.test_catalog import data, gtin
from tests.test_catalog_integrations import CatalogProvider, SchemaTransport, fixture_reply


class BrowserProvider(CatalogProvider):
    def __init__(self):
        super().__init__()
        self.cards[3] = copy.deepcopy(self.cards[1])
        self.cards[3]["good_id"] = 3
        self.cards[3]["identified_by"] = [{"type": "gtin", "value": gtin(3)}]

    def request(self, method, url, *, params=None, headers=None, body=None):
        if urlparse(url).path.endswith("/product/info"):
            self.calls.append((method, urlparse(url).path, params, body))
            return fixture_reply(
                {
                    "results": [
                        {"gtin": g, "productGroup": self.group, "permits": {}}
                        for g in body["gtins"]
                    ]
                }
            )
        return super().request(method, url, params=params, headers=headers, body=body)


class BrowserWb(FixtureAdapter):
    key, label = "wb", "WB · изолированная проверка"
    supported_operations = staticmethod(WbAdapter.supported_operations)

    def describe(self, config):
        return AccountInfo(external_account_id=config["account_id"])

    @staticmethod
    def catalog_variants(record):
        return variants(record, "wb")


class BrowserOzonTransport(SchemaTransport):
    def request(self, method, url, *, params=None, headers=None, body=None):
        path = urlparse(url).path
        if path == "/v1/roles":
            return fixture_reply({"roles": [{"name": "Admin"}]})
        if path == "/v1/seller/info":
            return fixture_reply({"company": {"inn": "123456789012", "name": "QA seller"}})
        return super().request(method, url, params=params, headers=headers, body=body)


def main():
    target = Path(sys.argv[1])
    with TemporaryDirectory(prefix="fbe-catalog-qa-") as directory:
        vault, signer, provider = MemoryVault(), FixtureSigner(), BrowserProvider()
        ozon = OzonAdapter(vault, BrowserOzonTransport(), pause=lambda _: None)
        app = create_app(
            AppConfig(Path(directory), worker_enabled=False),
            [
                ChzAdapter(vault, signer, provider),
                BrowserWb(),
                ozon,
                KitAdapter(vault, KitProvider(), pause=lambda _: None),
            ],
            vault=vault,
            signer=signer,
        )
        state = app.state
        state.database.initialize()
        seller = state.sellers.create("Проверка ассортимента")["id"]
        other = state.sellers.create("Другой продавец")["id"]
        chz = state.connections.create(
            seller,
            "chz",
            "ЧЗ — проверка",
            {
                "inn": provider.inn,
                "credential_ref": vault.put(seller, {"true_token": "qa-token"}),
                "certificate": "A" * 40,
            },
        )
        wb = state.connections.create(
            seller,
            "wb",
            "WB — проверка",
            {"account_id": "wb-100", "tin": provider.inn, "read_only": True},
        )
        ozon_connection = state.connections.create(
            seller,
            "ozon",
            "Ozon — проверка",
            {
                "account_id": "100",
                "tin": provider.inn,
                "read_only": True,
                "credential_ref": vault.put(seller, {"ozon_token": "qa-token"}),
            },
        )
        state.records.apply(
            seller,
            wb["id"],
            NormalizedBatch(
                products=(
                    Product(
                        external_id="100",
                        title="WB источник",
                        sku="WB001",
                        attributes={
                            "source": {
                                "kizMarked": True,
                                "dimensions": {
                                    "length": 12.5,
                                    "width": 4,
                                    "height": 2,
                                    "weightBrutto": 0.25,
                                },
                                "sizes": [
                                    {"chrtID": 501, "techSize": "S", "skus": [gtin(10)]},
                                    {"chrtID": 502, "techSize": "M", "skus": [gtin(11)]},
                                ],
                            }
                        },
                    ),
                )
            ),
        )
        state.records.apply(
            seller,
            ozon_connection["id"],
            NormalizedBatch(
                products=(
                    ozon.product(
                        {
                            "id": 1,
                            "name": "Ozon источник",
                            "offer_id": "OZ001",
                            "sku": 10001,
                            "barcodes": [gtin(20)],
                            "description_category_id": 12,
                        }
                    ),
                )
            ),
        )
        kit = state.connections.create(
            seller,
            "kit",
            "Магазин — проверка",
            {
                "account_id": uid(8000),
                "tin": provider.inn,
                "read_only": True,
                "credential_ref": vault.put(seller, {"kit_token": "qa-token"}),
            },
        )
        state.records.apply(
            seller,
            wb["id"],
            NormalizedBatch(
                warehouses=(Warehouse(external_id="wb-main", name="Основной WB"),),
                supplies=(
                    Supply(
                        external_id="WB-QA-1",
                        status="open",
                        warehouse_external_id="wb-main",
                        attributes={"source": {"name": "Поставка QA"}},
                    ),
                ),
                orders=tuple(
                    Order(
                        external_id=str(101 + n),
                        status=status,
                        warehouse_external_id="wb-main",
                        attributes={
                            "source": {
                                "deliveryType": "fbs",
                                "article": "WB001",
                                "nmId": 100,
                                "chrtId": 501,
                                "warehouseId": "wb-main",
                            }
                        },
                    )
                    for n, status in enumerate(("new", "confirm", "cancel"))
                ),
            ),
        )
        state.records.apply(
            seller,
            ozon_connection["id"],
            NormalizedBatch(
                warehouses=(Warehouse(external_id="ozon-main", name="Основной Ozon"),),
                orders=tuple(
                    Order(
                        external_id="QA-100-" + str(n),
                        status=status,
                        warehouse_external_id="ozon-main",
                        attributes={"items": [], "source": {"posting_number": "QA-100-" + str(n)}},
                    )
                    for n, status in enumerate(("awaiting_packaging", "awaiting_deliver"))
                ),
            ),
        )
        state.records.apply(
            seller,
            kit["id"],
            NormalizedBatch(
                orders=(
                    Order(
                        external_id=uid(2001),
                        status="NEW",
                        attributes={"items": [], "source": {"order_number": "KIT-2001"}},
                    ),
                ),
            ),
        )
        with state.database.connection() as conn:
            state.fulfillment.snapshot(conn, seller, wb["id"], "last_sync", {"counts": {}})
            state.fulfillment.snapshot(conn, seller, wb["id"], "sync", {"state": "complete"})
            state.commerce.snapshot(
                conn, seller, ozon_connection["id"], "last_sync", {"counts": {}}
            )
            state.commerce.snapshot(conn, seller, kit["id"], "last_sync", {"counts": {}})
            for connection in (ozon_connection, kit):
                state.commerce.snapshot(
                    conn, seller, connection["id"], "sync", {"state": "complete"}
                )
        initial = state.catalog.save_product(seller, data())
        code_id = str(uuid4())
        with state.database.connection() as conn:
            conn.execute(
                "INSERT INTO marking_codes(id,seller_id,connection_id,code,full_code,gtin,"
                "product_group,external_status) VALUES(?,?,?,?,?,?,?,?)",
                (
                    code_id,
                    seller,
                    chz["id"],
                    "01" + gtin(3) + "21QA",
                    "01" + gtin(3) + "21QA\x1d91KEY\x1d92CRYPTO",
                    gtin(3),
                    provider.group,
                    "EMITTED",
                ),
            )
        target.write_text(
            json.dumps(
                {
                    "seller": seller,
                    "other": other,
                    "chz": chz["id"],
                    "ozon": ozon_connection["id"],
                    "wb": wb["id"],
                    "kit": kit["id"],
                    "initial": initial["id"],
                    "gtin": gtin(3),
                    "group": provider.group,
                    "code": code_id,
                }
            ),
            encoding="utf-8",
        )
        uvicorn.run(app, host="127.0.0.1", port=8767, log_level="warning")


if __name__ == "__main__":
    main()
