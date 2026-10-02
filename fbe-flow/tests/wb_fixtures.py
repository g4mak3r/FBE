"""Offline WB responses, including the current metaDetails format without legacy meta."""

import base64
import copy
import json
import time
from urllib.parse import urlparse

from fbe_flow.app import create_app
from fbe_flow.config import AppConfig
from fbe_flow.integrations.chz.adapter import ChzAdapter
from fbe_flow.integrations.chz.http import RemoteError, Reply
from fbe_flow.integrations.wb import WbAdapter

from .chz_fixtures import FixtureSigner, MemoryVault
from .chz_workflow_fixtures import WorkflowProvider


def token(*, mask=18, exp=None, **claims):
    payload = {"s": mask, "exp": exp if exp is not None else int(time.time()) + 3600, **claims}
    part = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return "eyJhbGciOiJ0ZXN0In0." + part + ".offline-signature"


class WbProvider:
    def __init__(self):
        self.account = {"sid": "seller-account", "tin": "123456789012", "name": "WB fixture"}
        self.cards = {
            n: {
                "nmID": n,
                "vendorCode": f"article-{n}",
                "title": f"WB product {n}",
                "subjectID": 765432,
                "subjectName": "Source category",
                "sizes": [{"chrtID": n * 10 + 1, "techSize": "0", "skus": [f"{n:013d}"]}],
            }
            for n in range(1, 103)
        }
        self.warehouses = [{"id": 999, "name": "Source warehouse"}]
        self.supplies = {
            "WB-GI-1": {
                "id": "WB-GI-1",
                "name": "Existing supply",
                "done": False,
                "cargoType": 1,
                "crossBorderType": 1,
                "isB2b": False,
                "scanDt": None,
            }
        }
        self.orders = {
            n: {
                "id": n,
                "warehouseId": 999,
                "nmId": 1,
                "chrtId": 11,
                "deliveryType": "fbs",
                "article": "article-1",
                "cargoType": 1,
                "crossBorderType": 1,
                "options": {"isB2B": False},
                "supplyId": "WB-GI-1" if n == 1001 else None,
            }
            for n in (1001, 1002)
        }
        self.statuses = {1001: "confirm", 1002: "new"}
        self.meta = {
            n: [{"key": "sgtin", "value": None, "decision": "required"}] for n in self.orders
        }
        self.calls = []
        self.failure = None
        self.failure_path = None
        self.decision_after_send = "filled"
        self.partial_add = False
        self.bad_meta_ids = False
        self.product_stall = False
        self.supply_cursor = None
        self.fail_read_after_send = False

    def members(self, supply):
        return [n for n, v in self.orders.items() if v.get("supplyId") == supply]

    def request(self, method, url, *, params=None, headers=None, body=None):
        path = urlparse(url).path
        self.calls.append((method, path, copy.deepcopy(params), copy.deepcopy(body)))
        writing = method in {"PATCH", "PUT", "DELETE"} or (
            method == "POST" and path == "/api/v3/supplies"
        )
        if writing and type(self.failure) is int:
            raise RemoteError(
                self.failure, f"http_{self.failure}", {"token": headers["Authorization"]}
            )
        if path == "/api/v1/seller-info":
            claims = json.loads(
                base64.urlsafe_b64decode(headers["Authorization"].split(".")[1] + "==")
            )
            value = dict(self.account)
            if claims.get("account"):
                value["sid"] = claims["account"]
        elif path == "/api/v3/warehouses":
            value = self.warehouses
        elif path == "/content/v2/get/cards/list":
            cursor = body["settings"]["cursor"]
            count, start = cursor["limit"], cursor.get("nmID", 0)
            values = [v for n, v in self.cards.items() if n > start][:count]
            returned = {
                "total": len(values),
                "nmID": values[-1]["nmID"] if values else start,
                "updatedAt": "2026-10-02T00:00:00Z",
            }
            if self.product_stall and start:
                returned.update(cursor, total=100)
            value = {"cards": values, "cursor": returned}
        elif path == "/api/v3/supplies" and method == "GET":
            value = {"supplies": list(self.supplies.values()), "next": self.supply_cursor or 0}
        elif path == "/api/v3/orders":
            value = {"orders": list(self.orders.values()), "next": 0}
        elif path == "/api/v3/orders/new":
            value = {"orders": [v for n, v in self.orders.items() if self.statuses[n] == "new"]}
        elif path == "/api/v3/orders/status":
            value = {
                "orders": [
                    {"id": n, "supplierStatus": self.statuses[n], "wbStatus": "waiting"}
                    for n in body["orders"]
                ]
            }
        elif path == "/api/marketplace/v3/orders/meta":
            if self.fail_read_after_send and any(v[0] == "PUT" for v in self.calls):
                raise RemoteError(None, "transport_error")
            value = {
                "orders": [
                    {"id": n + 1 if self.bad_meta_ids else n, "metaDetails": self.meta[n]}
                    for n in body["orders"]
                ]
            }
        elif path.endswith("/meta/sgtin"):
            n = int(path.split("/")[-3])
            self.meta[n] = [
                {"key": "sgtin", "value": body["sgtins"], "decision": self.decision_after_send}
            ]
            value = None
        elif path == "/api/v3/supplies" and method == "POST":
            identifier = f"WB-GI-{len(self.supplies) + 1}"
            self.supplies[identifier] = {
                "id": identifier,
                "name": body["name"],
                "done": False,
                "cargoType": 0,
                "scanDt": None,
            }
            value = {"id": identifier}
        elif path.endswith("/order-ids"):
            identifier = path.split("/")[-2]
            value = {"orderIds": self.members(identifier)}
        elif path.startswith("/api/marketplace/v3/supplies/") and path.endswith("/orders"):
            identifier = path.split("/")[-2]
            selected = body["orders"][:1] if self.partial_add else body["orders"]
            for n in selected:
                self.orders[n]["supplyId"] = identifier
                self.statuses[n] = "confirm"
            value = None
        elif path.endswith("/deliver"):
            identifier = path.split("/")[-2]
            self.supplies[identifier]["done"] = True
            for n in self.members(identifier):
                self.statuses[n] = "complete"
            value = None
        elif path.startswith("/api/v3/supplies/"):
            identifier = path.split("/")[-1]
            if identifier not in self.supplies:
                raise RemoteError(404, "http_404")
            if method == "DELETE":
                del self.supplies[identifier]
                value = None
            else:
                value = self.supplies[identifier]
        else:
            raise AssertionError((method, path, params, body))
        if writing and self.failure in {"timeout", "invalid_json"}:
            raise RemoteError(
                None, "transport_error" if self.failure == "timeout" else "invalid_response"
            )
        return Reply(
            201 if writing and path == "/api/v3/supplies" else (204 if writing else 200),
            copy.deepcopy(value),
            {},
        )


def application(tmp_path, *, worker=False):
    vault, signer, chz, wb = MemoryVault(), FixtureSigner(), WorkflowProvider(), WbProvider()
    app = create_app(
        AppConfig(tmp_path, worker_enabled=worker),
        [ChzAdapter(vault, signer, chz), WbAdapter(vault, wb, pause=lambda _: None)],
        vault=vault,
        signer=signer,
    )
    return app, chz, wb, vault


def drain(state, maximum=100):
    for _ in range(maximum):
        if not state.operations.run_once():
            return
    raise AssertionError("Queue did not finish")


def seed(state, client, chz, wb):
    seller = state.sellers.create("WB seller")["id"]
    ref = state.vault.put(seller, {"true_token": "private-token"})
    chz_connection = state.connections.create(
        seller,
        "chz",
        "Production ChZ",
        {
            "inn": chz.inn,
            "environment": "production",
            "credential_ref": ref,
            "certificate": "A" * 40,
        },
    )
    state.operations.enqueue(seller, chz_connection["id"], "nk.references", {})
    state.marking.start_sync(seller, chz_connection["id"])
    response = client.post(
        f"/api/sellers/{seller}/wb/connections", json={"name": "WB account", "token": token()}
    )
    assert response.status_code == 201, response.text
    connection = response.json()
    state.fulfillment.start_sync(seller, connection["id"])
    drain(state)
    return seller, chz_connection, connection


def add_link(state, seller, chz_connection, connection):
    product = next(
        v
        for v in state.fulfillment.records(seller, connection["id"], "products", limit=200)["items"]
        if v["external_id"] == "1"
    )
    nk = state.marking.products(seller, chz_connection["id"])["items"][0]
    return state.fulfillment.link(
        seller,
        connection["id"],
        {
            "product_id": product["id"],
            "chrt_id": "11",
            "chz_connection_id": chz_connection["id"],
            "chz_product_id": nk["id"],
            "gtin": "00000000000001",
            "product_group": "account-group",
        },
    )


def add_codes(state, seller, chz_connection, provider, count=3):
    rows = []
    for n in range(count):
        cis = "010000000000000121" + f"SERIAL{n:07d}"
        row = {
            "cisInfo": {
                "requestedCis": cis,
                "cis": cis,
                "gtin": "00000000000001",
                "ownerInn": provider.inn,
                "productGroup": provider.group,
                "packageType": "UNIT",
                "status": "INTRODUCED",
            }
        }
        provider.cises[cis] = row
        rows.append(row)
    with state.database.connection() as conn:
        state.marking._apply_codes(
            conn, {"seller_id": seller, "connection_id": chz_connection["id"]}, rows
        )
        for row in rows:
            cis = row["cisInfo"]["cis"]
            conn.execute(
                "UPDATE marking_codes SET full_code=? WHERE seller_id=? "
                "AND connection_id=? AND code=?",
                (cis + "\x1d91KEY1\x1d92SIGNATURE", seller, chz_connection["id"], cis),
            )
    return state.marking.codes(seller, chz_connection["id"])["items"]
