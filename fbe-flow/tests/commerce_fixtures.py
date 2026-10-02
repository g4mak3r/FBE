"""Transport fixtures following Ozon Seller and Yandex KIT response contracts."""

import copy
from urllib.parse import urlparse
from uuid import UUID

from fbe_flow.app import create_app
from fbe_flow.config import AppConfig
from fbe_flow.integrations.chz import ChzAdapter
from fbe_flow.integrations.chz.http import RemoteError, Reply
from fbe_flow.integrations.kit import KitAdapter
from fbe_flow.integrations.ozon import OzonAdapter
from fbe_flow.integrations.wb import WbAdapter

from .chz_fixtures import FixtureSigner, MemoryVault
from .chz_workflow_fixtures import WorkflowProvider
from .wb_fixtures import WbProvider, drain, seed


def uid(number):
    return str(UUID(int=number))


class OzonProvider:
    def __init__(self):
        self.calls, self.failure, self.read_failure = [], None, False
        self.inn, self.passed, self.mark_errors = "123456789012", False, []
        self.products = {
            n: {"id": n, "sku": n + 10000, "name": f"Ozon {n}", "offer_id": f"offer-{n}"}
            for n in range(1, 103)
        }
        self.order = {
            "posting_number": "001-100-1",
            "status": "awaiting_packaging",
            "available_actions": ["ship", "cancel"],
            "requirements": {"products_requiring_mandatory_mark": [10001]},
            "products": [
                {"sku": 10001, "name": "Marked", "quantity": 2, "price": "100.00"},
                {"sku": 10002, "name": "Other", "quantity": 1, "price": "50.00"},
            ],
        }
        self.exemplars = {
            "posting_number": self.order["posting_number"],
            "multi_box_qty": 1,
            "products": [
                {"product_id": 10001, "exemplars": [{"exemplar_id": 71}, {"exemplar_id": 72}]},
                {
                    "product_id": 10002,
                    "exemplars": [{"exemplar_id": 73, "gtd": "original-gtd", "marks": []}],
                },
            ],
        }
        self.product_stall, self.has_next_empty = False, False
        self.ship_pending = False

    def request(self, method, url, *, params=None, headers=None, body=None):
        assert url.startswith("https://api-seller.ozon.ru/")
        assert headers["Client-Id"] == "777" and headers["Api-Key"]
        path = urlparse(url).path
        self.calls.append((method, path, copy.deepcopy(params), copy.deepcopy(body)))
        writes = {
            "/v6/fbs/posting/product/exemplar/set",
            "/v4/posting/fbs/ship",
            "/v2/posting/fbs/cancel",
        }
        if path in writes and type(self.failure) is int:
            raise RemoteError(self.failure, f"http_{self.failure}", {"key": headers["Api-Key"]})
        if path == "/v1/roles":
            value = {"roles": [{"name": "Admin", "methods": []}]}
        elif path == "/v1/seller/info":
            value = {"company": {"inn": self.inn, "name": "Fixture seller"}}
        elif path == "/v2/warehouse/list":
            value = {"warehouses": [{"warehouse_id": 123, "name": "Ozon warehouse"}]}
        elif path == "/v3/product/list":
            start = int(body["last_id"] or 0)
            if self.product_stall and start:
                start = 0
            ids = [n for n in self.products if n > start][:100]
            value = {
                "result": {
                    "items": [{"product_id": n, "offer_id": f"offer-{n}"} for n in ids],
                    "last_id": str(ids[-1]) if ids else "",
                    "total": len(self.products),
                }
            }
        elif path == "/v3/product/info/list":
            value = {"items": [self.products[n] for n in body["product_id"]]}
        elif path == "/v3/posting/fbs/list":
            value = {
                "result": {
                    "postings": [] if self.has_next_empty else [self.order],
                    "has_next": self.has_next_empty,
                }
            }
        elif path == "/v3/posting/fbs/get":
            value = {"result": self.order}
        elif path == "/v2/carriage/delivery/list":
            value = {"methods": [{"carriages": [{"id": 0}, {"id": 12, "status": "new"}]}]}
        elif path == "/v1/returns/list":
            value = {"returns": [{"id": 44, "posting_number": "001-100-1"}], "has_next": False}
        elif path == "/v6/fbs/posting/product/exemplar/create-or-get":
            value = self.exemplars
        elif path == "/v6/fbs/posting/product/exemplar/set":
            self.exemplars = copy.deepcopy(body)
            value = {}
        elif path == "/v5/fbs/posting/product/exemplar/status":
            if self.read_failure:
                raise RemoteError(None, "transport_error")
            value = copy.deepcopy(self.exemplars)
            value["status"] = "ship_available" if self.passed else "validation_in_process"
            for product in value["products"]:
                for exemplar in product["exemplars"]:
                    for mark in exemplar.get("marks", []):
                        mark.update(
                            check_status="passed" if self.passed else "processing",
                            error_codes=self.mark_errors,
                        )
        elif path == "/v4/posting/fbs/ship":
            if not self.ship_pending:
                self.order["status"] = "awaiting_deliver"
            value = {"result": [self.order["posting_number"]]}
        elif path == "/v2/posting/fbs/cancel-reason/list":
            value = {
                "result": [{"id": 101, "title": "Test", "is_available_for_cancellation": True}]
            }
        elif path == "/v2/posting/fbs/cancel":
            self.order["status"] = "cancelled"
            value = {"result": True}
        else:
            raise AssertionError((method, path, params, body))
        if path in writes and self.failure in {"timeout", "invalid_json"}:
            raise RemoteError(
                None, "transport_error" if self.failure == "timeout" else "invalid_response"
            )
        return Reply(200, copy.deepcopy(value), {})


class KitProvider:
    def __init__(self):
        self.calls, self.failure, self.partial = [], None, False
        self.store_id = uid(8000)
        self.products = {
            uid(n): {
                "id": uid(n),
                "name": f"KIT {n}",
                "sku": f"article-{n}",
                "product_id": uid(n + 1000),
                "status": "PUBLISHED",
                "requires_marking": n == 1,
                "pricing": {"price": "100.00", "manual_discount_price": "90.00"},
                "stocks": [{"warehouse_id": uid(9000), "quantity": 10, "reserved": 2}],
            }
            for n in range(1, 103)
        }
        self.warehouses = [{"id": uid(9000), "title": "KIT warehouse", "status": "ACTIVE"}]
        self.order = {
            "id": uid(7000),
            "order_number": 451,
            "status": "WAIT_FOR_CONFIRMATION",
            "payment": {"method": "ONLINE", "status": "PAYMENT_PAID"},
            "total_final_price": "140.00",
            "acquiring_type": "CABINET_PROVIDER",
            "delivery_chunks": [
                {
                    "id": 1,
                    "items": [
                        {
                            "id": uid(6001),
                            "product_variant_id": uid(1),
                            "quantity": 1,
                            "final_price": "90.00",
                            "truthful_label": None,
                            "refused_count": 0,
                        }
                    ],
                    "delivery_info": {
                        "method": "COURIER",
                        "courier_delivery_service_type": "MERCHANT_SHIP",
                        "tracking_number": "track-first",
                    },
                },
                {
                    "id": 2,
                    "items": [
                        {
                            "id": uid(6002),
                            "product_variant_id": uid(2),
                            "quantity": 1,
                            "final_price": "50.00",
                            "truthful_label": None,
                            "refused_count": 0,
                        }
                    ],
                    "delivery_info": {"method": "SELF_PICK_UP", "tracking_number": "track-second"},
                },
            ],
        }
        self.orders = [self.order]
        self.stall_phase = None

    def request(self, method, url, *, params=None, headers=None, body=None):
        assert url.startswith("https://api.kit.yandex.net/")
        assert headers["Authorization"].startswith("Bearer ")
        path = urlparse(url).path
        self.calls.append((method, path, copy.deepcopy(params), copy.deepcopy(body)))
        if method == "POST" and type(self.failure) is int:
            raise RemoteError(self.failure, f"http_{self.failure}")
        if path == "/v1/store":
            value = {
                "id": self.store_id,
                "slug": "fixture",
                "b2c_url": "https://fixture.kit.yandex.ru",
            }
        elif path == "/v1/users/current":
            value = {"email": "fixture@example.test", "role_name": "Owner"}
        elif path in {"/v1/warehouses", "/v1/variants", "/v1/orders"}:
            key = path.split("/")[-1]
            values = {
                "warehouses": self.warehouses,
                "variants": list(self.products.values()),
                "orders": self.orders,
            }[key]
            page = 1 if self.stall_phase == key else params["page"]
            start = (page - 1) * params["per_page"]
            value = {key: values[start : start + params["per_page"]], "total_count": len(values)}
        elif path.startswith("/v1/variants/") and method == "GET":
            value = self.products[path.split("/")[-1]]
        elif path.endswith("/bulk_update"):
            kind = path.split("/")[-2]
            for item in body["items"][:1] if self.partial else body["items"]:
                product = self.products[item["variant_id"]]
                if kind == "prices":
                    product["pricing"].update({k: v for k, v in item.items() if k != "variant_id"})
                else:
                    stock = next(
                        v for v in product["stocks"] if v["warehouse_id"] == item["warehouse_id"]
                    )
                    stock["quantity"] = item["quantity"]
            value = None
        elif path.startswith("/v1/orders/"):
            if method == "GET":
                value = self.order
            else:
                assert body is None, "KIT order methods do not accept JSON bodies"
                self.order["status"] = {
                    "confirm": "WAIT_FOR_DELIVERY",
                    "cancel": "CANCELLATION_IN_PROGRESS",
                    "complete": "CREATING_FINAL_RECEIPTS",
                }[path.split("/")[-1]]
                value = None
        else:
            raise AssertionError((method, path, params, body))
        if method == "POST" and self.failure in {"timeout", "invalid_json"}:
            raise RemoteError(
                None, "transport_error" if self.failure == "timeout" else "invalid_response"
            )
        return Reply(204 if method == "POST" else 200, copy.deepcopy(value), {})


def application(tmp_path, *, worker=False):
    vault, signer = MemoryVault(), FixtureSigner()
    chz, wb, ozon, kit = WorkflowProvider(), WbProvider(), OzonProvider(), KitProvider()
    app = create_app(
        AppConfig(tmp_path, worker_enabled=worker),
        [
            ChzAdapter(vault, signer, chz),
            WbAdapter(vault, wb, pause=lambda _: None),
            OzonAdapter(vault, ozon, pause=lambda _: None),
            KitAdapter(vault, kit, pause=lambda _: None),
        ],
        vault=vault,
        signer=signer,
    )
    return app, chz, wb, ozon, kit, vault


def connect(state, client, seller, key, *, read_only=False):
    payload = {
        "adapter_key": key,
        "name": key + " account",
        "token": key + "-private-key",
        "read_only": read_only,
    }
    payload.update({"client_id": "777"} if key == "ozon" else {"tin": "123456789012"})
    response = client.post(f"/api/sellers/{seller}/commerce/connections", json=payload)
    assert response.status_code == 201, response.text
    connection = response.json()
    state.commerce.start_sync(seller, connection["id"])
    drain(state)
    return connection


def seed_all(state, client, chz, wb):
    seller, chz_connection, wb_connection = seed(state, client, chz, wb)
    ozon_connection = connect(state, client, seller, "ozon")
    kit_connection = connect(state, client, seller, "kit")
    return seller, chz_connection, wb_connection, ozon_connection, kit_connection


def add_link(state, seller, chz_connection, connection):
    product = next(
        v
        for v in state.commerce.records(seller, connection["id"], "products", limit=200)["items"]
        if v["external_id"] in {"1", uid(1)}
    )
    nk = state.marking.products(seller, chz_connection["id"])["items"][0]
    return state.commerce.link(
        seller,
        connection["id"],
        {
            "product_id": product["id"],
            "chz_connection_id": chz_connection["id"],
            "chz_product_id": nk["id"],
            "gtin": "00000000000001",
            "product_group": "account-group",
        },
    )
