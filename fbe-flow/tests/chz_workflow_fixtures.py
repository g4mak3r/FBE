"""Offline protocol responses derived from NK, OMS 3.0.33 and True API specifications."""

import copy
import json
from datetime import UTC, datetime
from urllib.parse import urlparse
from uuid import uuid4

from fbe_flow.integrations.chz.http import RemoteError, Reply

from .chz_fixtures import Provider


class WorkflowProvider(Provider):
    def __init__(self):
        super().__init__()
        self.oms_id, self.oms_connection = str(uuid4()), str(uuid4())
        self.raw_requests = []
        self.feeds, self.orders, self.blocks, self.reports, self.documents = {}, {}, {}, {}, {}
        self.cises = {}
        self.receipts, self.originals = {}, {}
        self.write_failure = None
        self.signature_errors = set()
        self.feed_errors = None
        self.model = [
            {
                "attr_id": 987654,
                "attr_name": "External attribute",
                "attr_type": "o",
                "attr_preset_only": False,
                "attr_multiplicity": False,
                "attr_value_type": [],
                "attr_field_type": "text",
            }
        ]

    def add_receipt(self, source, workflow, body):
        receipt_id, doc_id = str(uuid4()), source
        self.originals[doc_id] = copy.deepcopy(body)
        self.receipts[source] = {
            "resultDocId": receipt_id,
            "sourceDocId": source,
            "sourceDocDate": int(datetime.now(UTC).timestamp() * 1000),
            "workflow": workflow,
            "state": "SUCCESS",
            "details": {"participantInn": self.inn},
            "operations": [
                {
                    "docId": doc_id,
                    "operationType": "DOC_RECEIVED",
                    "details": {"omsId": self.oms_id},
                }
            ],
        }

    def request(self, method, url, *, params=None, headers=None, body=None):
        path = urlparse(url).path
        raw = body if isinstance(body, bytes) else None
        if raw is not None:
            self.raw_requests.append((path, raw, copy.deepcopy(headers)))
            body = json.loads(raw)
        write_paths = {
            "/api/v3/order",
            "/api/v3/codes",
            "/api/v3/utilisation",
            "/api/v3/order/close",
        }
        if isinstance(self.write_failure, int) and (
            path in write_paths
            or path.endswith(("/feed", "/feed-product-sign-pkcs", "/lk/documents/create"))
        ):
            self.calls.append((method, path, copy.deepcopy(params), copy.deepcopy(body)))
            self.headers.append(copy.deepcopy(headers))
            raise RemoteError(
                self.write_failure,
                f"http_{self.write_failure}",
                {"message": "suz-client-secret renewed-token"},
            )
        value = None
        handled = True
        mutation = False
        if "/auth/simpleSignIn/" in path:
            assert path.endswith(self.oms_connection)
            assert body["inn"] == self.inn
            value = {"token": "suz-client-secret"}
        elif path.endswith("/attributes"):
            value = {"apiversion": 3, "result": copy.deepcopy(self.model)}
        elif path.endswith("/feed-product") and (params or {}).get("gtin"):
            value = {
                "result": [
                    copy.deepcopy(v)
                    for v in self.cards.values()
                    if any(i["value"] == params["gtin"] for i in v["identified_by"])
                ]
            }
        elif path.endswith("/feed-product-document"):
            value = {
                "result": {
                    "xmls": [
                        {
                            "goodId": v,
                            "xml": "<?xml version='1.0' encoding='UTF-8'?><good>"
                            + json.dumps(self.cards[v], ensure_ascii=False, sort_keys=True)
                            + "</good>",
                        }
                        for v in body["goodIds"]
                        if v in self.cards and v not in self.unavailable
                    ],
                    "errors": [],
                }
            }
        elif path.endswith("/feed-product-sign-pkcs"):
            mutation = True
            signed, errors = [], []
            for item in body:
                key = item["goodId"]
                if key in self.signature_errors:
                    errors.append({"goodId": key, "message": "Invalid card"})
                else:
                    self.cards[key].update(
                        good_signed=True,
                        good_status="published",
                        good_detailed_status=["published"],
                    )
                    signed.append(key)
            value = {"result": {"signed": signed, "errors": errors}}
        elif path.endswith("/feed"):
            mutation = True
            feed_id = len(self.feeds) + 1
            self.feeds[str(feed_id)] = {
                "feed_id": feed_id,
                "status": "Moderated",
                **({"item": self.feed_errors} if self.feed_errors else {}),
            }
            for entry in body:
                card = self.cards[entry["good_id"]]
                for change in entry["good_attrs"]:
                    remaining = [
                        v for v in card["good_attrs"] if str(v["attr_id"]) != str(change["attr_id"])
                    ]
                    if not change.get("delete"):
                        remaining.append(copy.deepcopy(change))
                    card["good_attrs"] = remaining
            value = {"result": {"feed_id": feed_id}}
        elif path.endswith("/feed-status"):
            value = {"result": copy.deepcopy(self.feeds[str(params["feed_id"])])}
        elif path.endswith("/cises/info"):
            value = [
                copy.deepcopy(
                    self.cises.get(
                        code,
                        {
                            "cisInfo": {"requestedCis": code},
                            "errorCode": "404",
                            "errorMessage": "Code not found",
                        },
                    )
                )
                for code in body
            ]
        elif path.endswith("/lk/documents/create"):
            import base64

            mutation = True
            identifier = str(uuid4())
            self.documents[identifier] = {
                "number": identifier,
                "type": body["type"],
                "senderInn": self.inn,
                "status": "CHECKED_OK",
                "productGroup": [params["pg"]],
                "content": base64.b64decode(body["product_document"]).decode("utf-8"),
            }
            document = json.loads(self.documents[identifier]["content"])
            for item in document["products"]:
                code = item.get("uit_code") or item.get("cis")
                if code in self.cises:
                    self.cises[code]["cisInfo"]["status"] = (
                        "INTRODUCED" if body["type"] == "LP_INTRODUCE_GOODS" else "WRITTEN_OFF"
                    )
            value = identifier
        elif "/doc/" in path and path.endswith("/info"):
            value = [copy.deepcopy(self.documents[path.split("/")[-2]])]
        elif path.endswith("/api/v3/ping"):
            value = {"omsId": self.oms_id, "apiVersion": "3.0.33", "omsVersion": "4.61"}
        elif path.endswith("/api/v3/order"):
            mutation = True
            identifier = str(uuid4())
            self.orders[identifier] = {
                "orderId": identifier,
                "orderStatus": "READY",
                "productGroup": body["productGroup"],
                "buffers": [
                    {
                        "gtin": v["gtin"],
                        "templateId": v["templateId"],
                        "bufferStatus": "ACTIVE",
                        "totalCodes": v["quantity"],
                        "leftInBuffer": v["quantity"],
                    }
                    for v in body["products"]
                ],
            }
            value = {"omsId": self.oms_id, "orderId": identifier}
            self.add_receipt(identifier, "CREATE_ORDER", body)
        elif path.endswith("/api/v3/order/list"):
            value = {"omsId": self.oms_id, "orderInfos": copy.deepcopy(list(self.orders.values()))}
        elif path.endswith("/api/v3/order/status"):
            order = self.orders[params["orderId"]]
            value = [
                {
                    "omsId": self.oms_id,
                    "orderId": order["orderId"],
                    "availableCodes": v["leftInBuffer"],
                    **copy.deepcopy(v),
                }
                for v in order["buffers"]
                if not params.get("gtin") or params["gtin"] == v["gtin"]
            ]
        elif path.endswith("/api/v3/order/codes/blocks"):
            value = {
                "omsId": self.oms_id,
                "orderId": params["orderId"],
                "gtin": params["gtin"],
                "blocks": [
                    {"blockId": key, "blockDateTime": 1790872471, "quantity": len(v["codes"])}
                    for key, v in self.blocks.items()
                    if v["order_id"] == params["orderId"] and v["gtin"] == params["gtin"]
                ],
            }
        elif path.endswith("/api/v3/codes"):
            mutation = True
            identifier = str(uuid4())
            gtin = params["gtin"]
            codes = [
                f"01{gtin}21{len(self.blocks):04d}{v:09d}\x1d93ABCD"
                for v in range(params["quantity"])
            ]
            self.blocks[identifier] = {"order_id": params["orderId"], "gtin": gtin, "codes": codes}
            for code in codes:
                cis = code.split("\x1d", 1)[0]
                self.cises[cis] = {
                    "cisInfo": {
                        "cis": cis,
                        "requestedCis": cis,
                        "gtin": gtin,
                        "ownerInn": self.inn,
                        "status": "EMITTED",
                        "productGroup": self.group,
                        "packageType": "UNIT",
                    }
                }
            for buffer in self.orders[params["orderId"]]["buffers"]:
                if buffer["gtin"] == gtin:
                    buffer["leftInBuffer"] -= len(codes)
            value = {"omsId": self.oms_id, "blockId": identifier, "codes": codes}
        elif path.endswith("/api/v3/order/codes/retry"):
            value = {
                "omsId": self.oms_id,
                "blockId": params["blockId"],
                "codes": copy.deepcopy(self.blocks[params["blockId"]]["codes"]),
            }
        elif path.endswith("/api/v3/utilisation"):
            mutation = True
            identifier = str(uuid4())
            self.reports[identifier] = {
                "omsId": self.oms_id,
                "reportId": identifier,
                "reportStatus": "SUCCESS",
            }
            value = {"omsId": self.oms_id, "reportId": identifier}
            self.add_receipt(identifier, "REPORT_UTILIZE", body)
            for code in body["sntins"]:
                self.cises[code.split("\x1d", 1)[0]]["cisInfo"]["status"] = "APPLIED"
        elif path.endswith("/api/v3/report/info"):
            value = copy.deepcopy(self.reports[params["reportId"]])
        elif path.endswith("/api/v3/order/close"):
            mutation = True
            order = self.orders[body["orderId"]]
            for buffer in order["buffers"]:
                if not body.get("gtin") or body["gtin"] == buffer["gtin"]:
                    buffer["bufferStatus"] = "CLOSED"
            if all(v["bufferStatus"] == "CLOSED" for v in order["buffers"]):
                order["orderStatus"] = "CLOSED"
            value = {"omsId": self.oms_id}
            self.add_receipt(str(uuid4()), "CLOSE_ORDER", body)
        elif path.endswith("/api/v3/receipts/receipt/search"):
            assert body["filter"] and body["skip"] == 1
            value = {
                "results": [
                    copy.deepcopy(v)
                    for k, v in self.receipts.items()
                    if k in body["filter"].get("sourceDocIds", [])
                    or k in body["filter"].get("orderIds", [])
                    or self.originals[v["operations"][0]["docId"]].get("orderId")
                    in body["filter"].get("orderIds", [])
                ]
            }
        elif path.endswith("/api/v3/receipts/receipt"):
            value = {
                "results": [
                    copy.deepcopy(v)
                    for v in self.receipts.values()
                    if v["resultDocId"] == params["resultDocId"]
                ]
            }
        elif path.endswith("/api/v3/receipts/document"):
            assert any(
                v["resultDocId"] == params["resultDocId"]
                and v["operations"][0]["docId"] == params["docId"]
                for v in self.receipts.values()
            )
            value = {"content": json.dumps(self.originals[params["docId"]], ensure_ascii=False)}
        else:
            handled = False
        if not handled:
            return super().request(method, url, params=params, headers=headers, body=body)
        self.calls.append((method, path, copy.deepcopy(params), copy.deepcopy(body)))
        self.headers.append(copy.deepcopy(headers))
        if path.startswith("/api/v3/") and "/true-api/" not in path:
            assert params["omsId"] == self.oms_id
            assert headers["clientToken"] == "suz-client-secret"
        if mutation and self.write_failure:
            failure = self.write_failure
            if failure == "corrupt":
                return Reply(200, {"unexpected": "ack"}, {})
            if isinstance(failure, int):
                raise RemoteError(
                    failure,
                    f"http_{failure}",
                    {"message": "upstream error suz-client-secret renewed-token"},
                )
            raise RemoteError(None, "transport_error")
        return Reply(200, value, {})
