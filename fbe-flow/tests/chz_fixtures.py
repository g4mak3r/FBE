"""Offline doubles with official read-only response shapes; never shipped as accounts."""

import base64
import copy
import hashlib
import json
from urllib.parse import urlparse
from uuid import uuid4

from fbe_flow.core.errors import InvalidInput
from fbe_flow.integrations.chz.http import RemoteError, Reply


class MemoryVault:
    def __init__(self):
        self.values = {}

    def put(self, seller, values):
        reference = f"{seller}:{uuid4()}"
        self.values[reference] = dict(values)
        return reference

    def get(self, seller, reference):
        if not reference.startswith(seller + ":") or reference not in self.values:
            raise InvalidInput("Ключи другого продавца")
        return dict(self.values[reference])

    def delete(self, seller, reference):
        self.get(seller, reference)
        del self.values[reference]


class FixtureSigner:
    def __init__(self):
        self.calls = []

    def sign(self, raw, thumbprint, *, detached):
        self.calls.append((raw, thumbprint, detached))
        return base64.b64encode(b"test-signature").decode()

    def certificates(self):
        return [{"thumbprint": "A" * 40, "subject": "Fixture certificate", "expires": "2099-01-01"}]


def card(number=1, status="draft"):
    return {
        "good_id": number,
        "good_name": f"External product {number}",
        "good_status": status,
        "good_detailed_status": [status],
        "good_signed": False,
        "good_mark_flag": True,
        "good_turn_flag": False,
        "identified_by": [{"type": "gtin", "value": f"{number:014d}"}],
        "categories": [{"cat_id": 123456, "cat_name": "Account category"}],
        "good_attrs": [
            {"attr_id": 987654, "attr_name": "External attribute", "attr_value": "Before"}
        ],
    }


class Provider:
    def __init__(self):
        self.inn = "123456789012"
        self.group = "account-group"
        self.cards = {1: card()}
        self.calls, self.headers = [], []
        self.unavailable = set()
        self.fail_path = None
        self.private_account = True
        self.page_override = {}
        self.auth_response = {"uuidToken": "renewed-token", "expireDate": "2099-01-01T00:00:00Z"}

    def request(self, method, url, *, params=None, headers=None, body=None):
        path = urlparse(url).path
        self.calls.append((method, path, copy.deepcopy(params), copy.deepcopy(body)))
        self.headers.append(copy.deepcopy(headers))
        if self.fail_path and path.endswith(self.fail_path):
            raise RemoteError(429, "http_429", {"message": "renewed-token private-token"})
        if path.endswith("/auth/key"):
            value = {"uuid": str(uuid4()), "data": "CHALLENGE-UTF8"}
        elif path.endswith("/auth/simpleSignIn"):
            value = self.auth_response
        elif path.endswith("/participants"):
            value = {
                "inn": self.inn,
                "role": ["PRODUCER"] if self.private_account else [],
                "is_registered": True,
                "productGroups": [self.group],
            }
        elif path.endswith("/categories"):
            value = {
                "apiversion": 3,
                "result": [{"cat_id": 123456, "cat_name": "Account category"}],
            }
        elif path.endswith("/etagslist"):
            ids = sorted(self.cards)[params["offset"] : params["offset"] + 100]
            value = {
                "apiversion": 3,
                "result": {
                    "goods_count": len(ids),
                    "offset": params["offset"],
                    "last_product_number": params["offset"] + len(ids),
                    "total": len(self.cards),
                    "goods": [
                        {
                            "good_id": key,
                            "etag": hashlib.sha256(
                                json.dumps(self.cards[key], sort_keys=True).encode()
                            ).hexdigest(),
                        }
                        for key in ids
                    ],
                    **self.page_override,
                },
            }
        elif path.endswith("/feed-product"):
            ids = [int(v) for v in params["good_ids"].split(";")]
            assert len(ids) <= 25
            value = {
                "apiversion": 3,
                "result": [
                    copy.deepcopy(self.cards[v])
                    for v in ids
                    if v in self.cards and v not in self.unavailable
                ],
            }
        elif path.endswith("/product"):
            value = {
                "apiversion": 3,
                "result": [
                    copy.deepcopy(v)
                    for v in self.cards.values()
                    if params["gtin"] in [i["value"] for i in v.get("identified_by", [])]
                ],
            }
        else:
            raise AssertionError(f"Out of stage 1: {method} {path}")
        return Reply(200, value, {})
