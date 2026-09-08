from __future__ import annotations

import base64
import email.utils
import time
import threading
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass
from typing import Any

import requests
from redact import redact

BASE_URL = "https://marketplace-api.wildberries.ru"
STATISTICS_BASE_URL = "https://statistics-api.wildberries.ru"
COMMON_BASE_URL = "https://common-api.wildberries.ru"


class WBApiError(RuntimeError):
    pass


@dataclass
class WBSticker:
    order_id: int
    part_a: str | None
    part_b: str | None
    barcode: str | None
    file: str


class WBClient:
    def __init__(self, token: str, mock_mode: bool = False):
        self.token = token
        self.mock_mode = mock_mode
        # requests.Session is not designed to be mutated concurrently. FBE runs
        # cache refreshes and user jobs in parallel, so every worker thread gets
        # its own connection pool instead of sharing/resetting one global session.
        self._session_local = threading.local()

    def _get_session(self) -> requests.Session:
        session = getattr(self._session_local, "session", None)
        if session is None:
            session = requests.Session()
            self._session_local.session = session
        return session

    @property
    def session(self) -> requests.Session:
        """Compatibility accessor for the current thread's HTTP session."""
        return self._get_session()

    def _reset_session(self) -> None:
        session = getattr(self._session_local, "session", None)
        if session is not None:
            try:
                session.close()
            except Exception:
                pass
        self._session_local.session = requests.Session()

    @staticmethod
    def _retry_delay(response: requests.Response, attempt: int) -> float:
        raw = str(response.headers.get("Retry-After") or "").strip()
        if raw:
            try:
                return min(5.0, max(0.0, float(raw)))
            except ValueError:
                try:
                    parsed = email.utils.parsedate_to_datetime(raw)
                    if parsed.tzinfo is None:
                        parsed = parsed.replace(tzinfo=timezone.utc)
                    return min(
                        5.0,
                        max(0.0, (parsed - datetime.now(timezone.utc)).total_seconds()),
                    )
                except (TypeError, ValueError, OverflowError):
                    pass
        return min(2.0, 0.25 * (2 ** attempt))

    def _request(self, method: str, path: str, base_url: str = BASE_URL, **kwargs) -> Any:
        if self.mock_mode:
            return self._mock(method, path, **kwargs)
        if not str(self.token or "").strip():
            raise WBApiError("Wildberries не подключен: добавьте API-токен в профиле FBE.")

        url = f"{base_url}{path}"
        method_upper = str(method).upper()
        safe_retry = bool(kwargs.pop("safe_retry", method_upper == "GET"))
        attempts = 3 if safe_retry else 1
        response = None
        last_exc = None
        headers = dict(kwargs.pop("headers", {}) or {})
        headers["Authorization"] = self.token
        for attempt in range(attempts):
            try:
                response = self._get_session().request(
                    method, url, headers=headers, timeout=(4, 20), **kwargs
                )
                if (
                    safe_retry
                    and response.status_code in {429, 500, 502, 503, 504}
                    and attempt + 1 < attempts
                ):
                    delay = self._retry_delay(response, attempt)
                    response.close()
                    time.sleep(delay)
                    continue
                break
            except requests.RequestException as exc:
                last_exc = exc
                if attempt + 1 < attempts:
                    self._reset_session()
                    time.sleep(0.15)
                    continue
                raise WBApiError(f"WB API connection error {method} {path}: {redact(exc, self.token)}") from None
        if response is None:
            raise WBApiError(f"WB API connection error {method} {path}: {redact(last_exc, self.token)}")

        if response.status_code == 204:
            return None

        if not response.ok:
            body = redact(response.text, self.token)[:1000]
            raise WBApiError(f"WB API error {response.status_code} {method} {path}: {body}")

        if not response.text:
            return None

        try:
            return response.json()
        except ValueError as exc:
            raise WBApiError(
                f"WB API returned invalid JSON {method_upper} {path}: "
                f"{redact(response.text, self.token)[:300]}"
            ) from None

    def get_seller_info(self) -> dict[str, Any]:
        data = self._request("GET", "/api/v1/seller-info", base_url=COMMON_BASE_URL, safe_retry=False)
        if not isinstance(data, dict):
            raise WBApiError("WB API вернул некорректный профиль продавца.")
        return {
            "name": str(data.get("name") or "").strip(),
            "sid": str(data.get("sid") or "").strip(),
            "tin": str(data.get("tin") or "").strip(),
            "tradeMark": str(data.get("tradeMark") or "").strip(),
        }

    def get_new_orders(self) -> list[dict[str, Any]]:
        data = self._request("GET", "/api/v3/orders/new")
        # An empty orders list is a valid WB answer. An empty/malformed payload is
        # not: treating it as [] would incorrectly wipe the visible new-order list.
        if not isinstance(data, dict) or "orders" not in data:
            raise WBApiError("WB API returned invalid payload for GET /api/v3/orders/new")
        orders = data.get("orders")
        if not isinstance(orders, list):
            raise WBApiError("WB API returned invalid orders field for GET /api/v3/orders/new")
        return [dict(item) for item in orders if isinstance(item, dict)]

    def get_orders(
        self,
        limit: int = 1000,
        next_value: int = 0,
        *,
        date_from: int | None = None,
        date_to: int | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"limit": limit, "next": next_value}
        if date_from is not None:
            params["dateFrom"] = int(date_from)
        if date_to is not None:
            params["dateTo"] = int(date_to)
        return self._request("GET", "/api/v3/orders", params=params)

    def get_all_orders_recent(self, max_pages: int = 5) -> list[dict[str, Any]]:
        orders: list[dict[str, Any]] = []
        next_value = 0
        for _ in range(max_pages):
            data = self.get_orders(limit=1000, next_value=next_value)
            batch = data.get("orders", []) if data else []
            orders.extend(batch)
            new_next = data.get("next") if data else None
            if not new_next or new_next == next_value or not batch:
                break
            next_value = int(new_next)
            time.sleep(0.25)
        return orders

    def get_orders_period(self, date_from: datetime, date_to: datetime, *, max_pages: int = 20) -> list[dict[str, Any]]:
        """Return FBS assembly orders created in one <=30 day window.

        WB documents dateFrom/dateTo as Unix timestamps and limits one request
        period to 30 calendar days. Pagination is still respected.
        """
        if date_from.tzinfo is None:
            date_from = date_from.replace(tzinfo=timezone.utc)
        if date_to.tzinfo is None:
            date_to = date_to.replace(tzinfo=timezone.utc)
        if date_to <= date_from:
            return []
        if date_to - date_from > timedelta(days=30, seconds=1):
            raise ValueError("WB allows a maximum 30-day FBS order period per request")
        result: list[dict[str, Any]] = []
        next_value = 0
        for _ in range(max_pages):
            data = self.get_orders(
                limit=1000,
                next_value=next_value,
                date_from=int(date_from.timestamp()),
                date_to=int(date_to.timestamp()),
            )
            batch = data.get("orders", []) if data else []
            result.extend(batch)
            new_next = data.get("next") if data else None
            if not new_next or new_next == next_value or not batch:
                break
            next_value = int(new_next)
            time.sleep(0.22)
        return result

    def get_orders_last_days(self, days: int = 92) -> list[dict[str, Any]]:
        """Load up to the WB online FBS history horizon in 30-day chunks."""
        days = max(1, min(int(days), 92))
        end = datetime.now(timezone.utc) + timedelta(minutes=1)
        start = end - timedelta(days=days)
        cursor = start
        rows: dict[int, dict[str, Any]] = {}
        while cursor < end:
            chunk_end = min(cursor + timedelta(days=30), end)
            for item in self.get_orders_period(cursor, chunk_end):
                try:
                    oid = int(item.get("id") or item.get("orderId") or 0)
                except (TypeError, ValueError):
                    oid = 0
                if oid:
                    rows[oid] = dict(item)
            cursor = chunk_end
            if cursor < end:
                time.sleep(0.22)
        return list(rows.values())

    def create_supply(self, name: str) -> str:
        data = self._request("POST", "/api/v3/supplies", json={"name": name})
        return data["id"]

    def get_seller_warehouses(self) -> list[dict[str, Any]]:
        """Return seller FBS virtual warehouses.

        New FBS assembly orders contain warehouseId. WB exposes the seller
        warehouse directory separately, so FBE joins that ID with the human
        warehouse name for the operational UI and automatic supply split.
        """
        data = self._request("GET", "/api/v3/warehouses")
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
        if isinstance(data, dict):
            for key in ("warehouses", "data", "items"):
                value = data.get(key)
                if isinstance(value, list):
                    return [item for item in value if isinstance(item, dict)]
        return []

    def get_supplies_page(self, limit: int = 1000, next_value: int = 0) -> dict[str, Any]:
        return self._request(
            "GET",
            "/api/v3/supplies",
            params={"limit": limit, "next": next_value},
        ) or {"supplies": [], "next": 0}

    def get_supplies(self, limit: int = 1000, next_value: int = 0) -> list[dict[str, Any]]:
        data = self.get_supplies_page(limit=limit, next_value=next_value)
        return data.get("supplies", []) if data else []

    def get_all_supplies(self, max_pages: int = 3) -> list[dict[str, Any]]:
        supplies: list[dict[str, Any]] = []
        next_value = 0

        for _ in range(max_pages):
            data = self.get_supplies_page(limit=1000, next_value=next_value)
            batch = data.get("supplies", []) if data else []
            supplies.extend(batch)

            new_next = data.get("next") if data else None
            if not new_next or new_next == next_value or not batch:
                break

            next_value = int(new_next)
            time.sleep(0.25)

        return supplies

    def get_supply_details(self, supply_id: str) -> dict[str, Any]:
        return self._request("GET", f"/api/v3/supplies/{supply_id}") or {}

    def get_supply_barcode(
        self, supply_id: str, barcode_type: str = "png", width: int = 58, height: int = 40
    ) -> dict[str, Any]:
        return self._request(
            "GET",
            f"/api/v3/supplies/{supply_id}/barcode",
            params={"type": barcode_type, "width": int(width), "height": int(height)},
        ) or {}

    @staticmethod
    def _extract_trbx_ids(data: Any) -> list[str]:
        """Normalize current/legacy WB shipping-unit response shapes to trbx IDs.

        WB currently documents POST /trbx as returning trbxIds. GET responses have
        changed shape historically, so never stringify whole dict objects here.
        """
        result: list[str] = []

        def add(value: Any) -> None:
            if value is None:
                return
            if isinstance(value, (str, int)):
                text = str(value).strip()
                if text and text not in result:
                    result.append(text)
                return
            if isinstance(value, list):
                for item in value:
                    add(item)
                return
            if isinstance(value, dict):
                # A single shipping-unit object.
                for key in ("trbxId", "trbxID", "id", "boxId", "shippingUnitId"):
                    raw = value.get(key)
                    if raw not in (None, ""):
                        add(raw)
                        return
                # Wrapper objects used by different WB revisions.
                for key in ("trbxIds", "trbx", "boxes", "shippingUnits", "ids", "items", "data", "result"):
                    if key in value:
                        add(value.get(key))

        add(data)
        return result

    def get_supply_shipping_units(self, supply_id: str) -> list[str]:
        data = self._request("GET", f"/api/v3/supplies/{supply_id}/trbx")
        return self._extract_trbx_ids(data)

    def add_supply_shipping_units(self, supply_id: str, amount: int) -> list[str]:
        amount = int(amount)
        if amount < 1 or amount > 1000:
            raise ValueError("Количество грузомест должно быть от 1 до 1000")
        data = self._request(
            "POST", f"/api/v3/supplies/{supply_id}/trbx", json={"amount": amount}
        )
        # Current WB API returns trbxIds here. Preserve them: they are the most
        # reliable identity of the units just created and can be used for stickers
        # immediately, without depending on GET /trbx read-after-write consistency.
        return self._extract_trbx_ids(data)

    def get_supply_shipping_unit_stickers(
        self,
        supply_id: str,
        trbx_ids: list[str],
        *,
        sticker_type: str = "png",
        width: int = 58,
        height: int = 40,
    ) -> list[dict[str, Any]]:
        clean = [str(x).strip() for x in trbx_ids if str(x).strip()]
        if not clean:
            return []
        data = self._request(
            "POST",
            f"/api/v3/supplies/{supply_id}/trbx/stickers",
            params={"type": sticker_type, "width": int(width), "height": int(height)},
            json={"trbxIds": clean},
            # Sticker retrieval is read-only.  It is safe to replay on a
            # transient 429/5xx/connection failure, unlike supply mutations.
            safe_retry=True,
        )
        if isinstance(data, list):
            return [dict(x) for x in data if isinstance(x, dict)]
        if isinstance(data, dict):
            # Current schema uses stickers; tolerate wrappers without confusing a
            # single sticker object with a list of shipping-unit IDs.
            for key in ("stickers", "trbxStickers", "items"):
                value = data.get(key)
                if isinstance(value, list):
                    return [dict(x) for x in value if isinstance(x, dict)]
            nested = data.get("data")
            if isinstance(nested, list):
                return [dict(x) for x in nested if isinstance(x, dict)]
            if isinstance(nested, dict):
                for key in ("stickers", "trbxStickers", "items"):
                    value = nested.get(key)
                    if isinstance(value, list):
                        return [dict(x) for x in value if isinstance(x, dict)]
            if str(data.get("file") or ""):
                return [dict(data)]
        return []

    def deliver_supply(self, supply_id: str) -> None:
        self._request("PATCH", f"/api/v3/supplies/{supply_id}/deliver")

    def add_orders_to_supply(self, supply_id: str, order_ids: list[int]) -> None:
        if not order_ids:
            return
        if len(order_ids) > 100:
            raise ValueError("WB allows max 100 assembly order IDs per request")

        self._request(
            "PATCH",
            f"/api/marketplace/v3/supplies/{supply_id}/orders",
            json={"orders": order_ids},
        )

    def get_supply_order_ids(self, supply_id: str) -> list[int]:
        data = self._request("GET", f"/api/marketplace/v3/supplies/{supply_id}/order-ids")
        return data.get("orderIds", []) if data else []


    def get_order_statuses(self, order_ids: list[int]) -> list[dict[str, Any]]:
        """Get current WB/supplier statuses for FBS assembly orders."""
        ids = [int(x) for x in order_ids if int(x)]
        if not ids:
            return []
        result: list[dict[str, Any]] = []
        for i in range(0, len(ids), 1000):
            batch = ids[i:i+1000]
            data = self._request(
                "POST", "/api/v3/orders/status", json={"orders": batch}, safe_retry=True
            )
            rows = data.get("orders", []) if isinstance(data, dict) else []
            result.extend([dict(x) for x in rows if isinstance(x, dict)])
            if i + 1000 < len(ids):
                time.sleep(0.2)
        return result

    def get_order_stickers(
        self,
        order_ids: list[int],
        sticker_type: str = "zplv",
        width: int = 58,
        height: int = 40,
    ) -> list[WBSticker]:
        if not order_ids:
            return []
        if len(order_ids) > 100:
            raise ValueError("WB allows max 100 stickers per request")

        data = self._request(
            "POST",
            "/api/v3/orders/stickers",
            params={"type": sticker_type, "width": width, "height": height},
            json={"orders": order_ids},
            safe_retry=True,
        )
        stickers = data.get("stickers", []) if data else []
        return [
            WBSticker(
                order_id=int(item["orderId"]),
                part_a=item.get("partA"),
                part_b=item.get("partB"),
                barcode=item.get("barcode"),
                file=item.get("file", ""),
            )
            for item in stickers
        ]


    def get_supplier_orders_report(self, date_from: str, flag: int = 1) -> list[dict[str, Any]]:
        """Operational orders report from WB Statistics API.

        `flag=1` means WB returns rows whose order date equals `dateFrom`
        (time part is ignored by WB for this mode).
        """
        data = self._request(
            "GET",
            "/api/v1/supplier/orders",
            base_url=STATISTICS_BASE_URL,
            params={"dateFrom": date_from, "flag": int(flag)},
        )
        return data if isinstance(data, list) else []

    def get_supplier_sales_report(self, date_from: str, flag: int = 1) -> list[dict[str, Any]]:
        """Operational sales/returns report from WB Statistics API."""
        data = self._request(
            "GET",
            "/api/v1/supplier/sales",
            base_url=STATISTICS_BASE_URL,
            params={"dateFrom": date_from, "flag": int(flag)},
        )
        return data if isinstance(data, list) else []

    def set_order_expiration(self, order_id: int, expiration: str) -> None:
        """Set product expiration date for a FBS assembly order.

        WB expects dd.mm.yyyy in JSON: {"expiration": "14.06.2028"}.
        """
        self._request(
            "PUT",
            f"/api/v3/orders/{int(order_id)}/meta/expiration",
            json={"expiration": expiration},
        )

    def get_orders_meta(self, order_ids: list[int]) -> list[dict[str, Any]]:
        """Return FBS assembly-order metadata for up to 100 order IDs."""
        ids = [int(x) for x in order_ids]
        if not ids:
            return []
        if len(ids) > 100:
            raise ValueError("WB allows max 100 order IDs per metadata request")
        data = self._request(
            "POST",
            "/api/marketplace/v3/orders/meta",
            json={"orders": ids},
            safe_retry=True,
        )
        if isinstance(data, list):
            return [dict(item) for item in data if isinstance(item, dict)]
        if isinstance(data, dict):
            for key in ("orders", "data", "items"):
                rows = data.get(key)
                if isinstance(rows, list):
                    return [dict(item) for item in rows if isinstance(item, dict)]
        return []

    def set_order_sgtin(self, order_id: int, codes: list[str]) -> None:
        """Attach one or more Chestny ZNAK Data Matrix codes to an FBS order."""
        clean_codes = [str(code) for code in codes if str(code)]
        if not clean_codes:
            raise ValueError("At least one marking code is required")
        if len(clean_codes) > 100:
            raise ValueError("WB allows max 100 marking codes per order request")
        self._request(
            "PUT",
            f"/api/v3/orders/{int(order_id)}/meta/sgtin",
            json={"sgtins": clean_codes},
        )

    @staticmethod
    def decode_sticker_file(raw_file: str, sticker_type: str) -> bytes:
        """WB may return sticker file as base64 for image formats; ZPL may be raw or base64."""
        if sticker_type.startswith("zpl") and raw_file.strip().startswith("^XA"):
            return raw_file.encode("utf-8")
        try:
            return base64.b64decode(raw_file, validate=True)
        except Exception:
            return raw_file.encode("utf-8")

    def _mock(self, method: str, path: str, **kwargs) -> Any:
        if path == "/api/v1/seller-info" and method == "GET":
            return {"name": "FBE Demo Company", "sid": "00000000-0000-4000-8000-000000000017", "tin": "000000000000", "tradeMark": "DEMO"}
        if path == "/api/v3/warehouses":
            return [
                {"id": 1, "name": "FBS Внуково", "officeId": 101},
                {"id": 2, "name": "FBS Ростов", "officeId": 102},
            ]
        if path == "/api/v3/orders/new":
            return {
                "orders": [
                    {
                        "id": 910000001,
                        "article": "PRF-R0301",
                        "skus": ["460000000001"],
                        "createdAt": "2026-08-13T10:00:00Z",
                        "warehouseId": 1,
                        "cargoType": 1,
                        "crossBorderType": 0,
                        "requiredMeta": [],
                        "optionalMeta": [],
                    },
                    {
                        "id": 910000002,
                        "article": "SPA-DEOD07",
                        "skus": ["460000000002"],
                        "createdAt": "2026-08-13T10:05:00Z",
                        "warehouseId": 2,
                        "cargoType": 1,
                        "crossBorderType": 0,
                        "requiredMeta": ["sgtin"],
                        "optionalMeta": [],
                    },
                ]
            }

        if path == "/api/v3/supplies" and method == "POST":
            return {"id": "WB-GI-MOCK-001"}

        if path == "/api/v3/supplies" and method == "GET":
            return {
                "supplies": [
                    {"id": "WB-GI-DEMO-ASSEMBLY", "done": False, "createdAt": "2026-09-07T08:10:00Z", "closedAt": None, "scanDt": None, "name": "Demo · На сборке", "cargoType": 1, "crossBorderType": 0},
                    {"id": "WB-GI-DEMO-READY", "done": True, "createdAt": "2026-09-06T11:20:00Z", "closedAt": "2026-09-07T07:45:00Z", "scanDt": None, "name": "Demo · Ждет отгрузки", "cargoType": 1, "crossBorderType": 0},
                    {"id": "WB-GI-DEMO-DONE", "done": True, "createdAt": "2026-09-03T09:00:00Z", "closedAt": "2026-09-03T14:00:00Z", "scanDt": "2026-09-04T10:30:00Z", "name": "Demo · Доставлено", "cargoType": 1, "crossBorderType": 0},
                ],
                "next": 0,
            }

        if path.endswith("/orders") and method == "PATCH":
            return None

        if path.endswith("/order-ids") and method == "GET":
            supply_id = path.split("/")[4] if len(path.split("/")) > 4 else ""
            mapping = {
                "WB-GI-DEMO-ASSEMBLY": [910000001, 910000002],
                "WB-GI-DEMO-READY": [910000003],
                "WB-GI-DEMO-DONE": [910000004],
            }
            return {"orderIds": mapping.get(supply_id, [])}

        if path.endswith("/trbx") and method == "GET":
            return {"trbxIds": ["WB-TRBX-MOCK-001"]}

        if path.endswith("/trbx") and method == "POST":
            amount = int(kwargs.get("json", {}).get("amount", 1) or 1)
            return {"trbxIds": [f"WB-TRBX-MOCK-{i+1:03d}" for i in range(amount)]}

        if path.endswith("/trbx/stickers") and method == "POST":
            # 1x1 transparent PNG, enough for route/integration tests.
            tiny = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
            ids = kwargs.get("json", {}).get("trbxIds", [])
            return {"stickers": [{"trbxId": str(x), "file": tiny} for x in ids]}

        if path.endswith("/deliver") and method == "PATCH":
            return None

        if path.endswith("/barcode") and method == "GET":
            tiny = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
            return {"barcode": "WB-GI-MOCK", "file": tiny}

        if path == "/api/v3/orders" and method == "GET":
            return {
                "next": 0,
                "orders": [
                    {"id": 910000001, "supplyId": "WB-GI-DEMO-ASSEMBLY", "article": "PRF-R0301", "skus": ["460000000001"], "createdAt": "2026-09-07T08:12:00Z", "requiredMeta": [], "optionalMeta": []},
                    {"id": 910000002, "supplyId": "WB-GI-DEMO-ASSEMBLY", "article": "SPA-DEOD07", "skus": ["460000000002"], "createdAt": "2026-09-07T08:14:00Z", "requiredMeta": ["sgtin"], "optionalMeta": []},
                    {"id": 910000003, "supplyId": "WB-GI-DEMO-READY", "article": "PRF-R0301", "skus": ["460000000001"], "createdAt": "2026-09-06T11:25:00Z", "requiredMeta": [], "optionalMeta": []},
                    {"id": 910000004, "supplyId": "WB-GI-DEMO-DONE", "article": "DEMO-SAMPLE", "skus": ["460000000003"], "createdAt": "2026-09-03T09:05:00Z", "requiredMeta": [], "optionalMeta": []},
                ],
            }

        if path == "/api/v3/orders/status" and method == "POST":
            order_ids = kwargs.get("json", {}).get("orders", [])
            states = {
                910000001: ("confirm", "waiting"),
                910000002: ("confirm", "waiting"),
                910000003: ("complete", "sorted"),
                910000004: ("complete", "sold"),
            }
            return {"orders": [
                {"id": int(oid), "supplierStatus": states.get(int(oid), ("complete", "sorted"))[0], "wbStatus": states.get(int(oid), ("complete", "sorted"))[1]}
                for oid in order_ids
            ]}

        if path == "/api/marketplace/v3/orders/meta" and method == "POST":
            order_ids = kwargs.get("json", {}).get("orders", [])
            rows = []
            for oid in order_ids:
                oid_int = int(oid)
                if oid_int % 2:
                    # Current FBS metadata may return concrete SGTIN identifiers
                    # separately from their validation decision. Keep this mock
                    # shape intentionally different from the legacy meta.sgtin
                    # model to test backward-compatible parsing.
                    serial = f"WB{oid_int}"
                    raw = f"010460000000000121{serial}\x1d91ABCD\x1d92MOCK"
                    raw = raw.replace("\\x1d", "\x1d")
                    rows.append({
                        "id": oid_int,
                        "sgtins": [{"sgtin": raw, "decision": "sgtinIntroduced"}],
                        "metaDetails": [{"key": "sgtin", "value": None, "decision": "filled"}],
                    })
                else:
                    rows.append({
                        "id": oid_int,
                        "sgtins": [],
                        "metaDetails": [{"key": "sgtin", "value": None, "decision": "required"}],
                    })
            return {"orders": rows}

        if path.startswith("/api/v3/orders/") and path.endswith("/meta/sgtin") and method == "PUT":
            return None

        if path.startswith("/api/v3/orders/") and path.endswith("/meta/expiration") and method == "PUT":
            return None

        if path == "/api/v3/orders/stickers" and method == "POST":
            order_ids = kwargs.get("json", {}).get("orders", [])
            sticker_type = kwargs.get("params", {}).get("type", "zplv")
            if sticker_type == "png":
                from PIL import Image, ImageDraw
                from io import BytesIO
                import base64
                stickers = []
                for oid in order_ids:
                    img = Image.new("RGB", (580, 400), "white")
                    d = ImageDraw.Draw(img)
                    d.rectangle([8, 8, 572, 392], outline="black", width=4)
                    d.text((40, 50), "WB MOCK STICKER", fill="black")
                    d.text((40, 130), str(oid), fill="black")
                    buf = BytesIO()
                    img.save(buf, format="PNG")
                    stickers.append({
                        "orderId": oid,
                        "partA": "123456",
                        "partB": "7890",
                        "barcode": f"MOCK-{oid}",
                        "file": base64.b64encode(buf.getvalue()).decode("ascii"),
                    })
                return {"stickers": stickers}

            if sticker_type == "svg":
                import base64
                stickers = []
                for oid in order_ids:
                    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" width="580" height="400" viewBox="0 0 580 400">
  <rect x="8" y="8" width="564" height="384" fill="white" stroke="black" stroke-width="4"/>
  <text x="40" y="70" font-family="Arial" font-size="36" fill="black">WB MOCK SVG STICKER</text>
  <text x="40" y="135" font-family="Arial" font-size="32" fill="black">{oid}</text>
  <rect x="40" y="180" width="360" height="90" fill="none" stroke="black" stroke-width="3"/>
  <text x="55" y="235" font-family="Arial" font-size="24" fill="black">MOCK-{oid}</text>
</svg>'''
                    stickers.append({
                        "orderId": oid,
                        "partA": "123456",
                        "partB": "7890",
                        "barcode": f"MOCK-{oid}",
                        "file": base64.b64encode(svg.encode("utf-8")).decode("ascii"),
                    })
                return {"stickers": stickers}

            return {
                "stickers": [
                    {
                        "orderId": oid,
                        "partA": "123456",
                        "partB": "7890",
                        "barcode": f"MOCK-{oid}",
                        "file": "^XA^FO40,40^A0N,40,40^FDWB MOCK STICKER^FS^FO40,100^BY2^BCN,100,Y,N,N^FD" + str(oid) + "^FS^XZ",
                    }
                    for oid in order_ids
                ]
            }

        if path.startswith("/api/v3/supplies/") and path.endswith("/barcode") and method == "GET":
            from PIL import Image, ImageDraw
            from io import BytesIO
            import base64
            supply_id = path.split("/")[4]
            img = Image.new("RGB", (580, 400), "white")
            d = ImageDraw.Draw(img)
            d.rectangle([8, 8, 572, 392], outline="black", width=4)
            d.text((40, 50), "WB SUPPLY QR MOCK", fill="black")
            d.text((40, 130), supply_id, fill="black")
            buf = BytesIO()
            img.save(buf, format="PNG")
            return {"barcode": supply_id, "file": base64.b64encode(buf.getvalue()).decode("ascii")}

        if path.startswith("/api/v3/supplies/") and method == "GET":
            supply_id = path.split("/")[-1]
            rows = {
                "WB-GI-DEMO-ASSEMBLY": {"done": False, "createdAt": "2026-09-07T08:10:00Z", "closedAt": None, "scanDt": None, "name": "Demo · На сборке"},
                "WB-GI-DEMO-READY": {"done": True, "createdAt": "2026-09-06T11:20:00Z", "closedAt": "2026-09-07T07:45:00Z", "scanDt": None, "name": "Demo · Ждет отгрузки"},
                "WB-GI-DEMO-DONE": {"done": True, "createdAt": "2026-09-03T09:00:00Z", "closedAt": "2026-09-03T14:00:00Z", "scanDt": "2026-09-04T10:30:00Z", "name": "Demo · Доставлено"},
            }
            base = rows.get(supply_id, {"done": False, "createdAt": "2026-09-07T08:10:00Z", "closedAt": None, "scanDt": None, "name": "Demo supply"})
            return {"id": supply_id, **base, "cargoType": 1, "crossBorderType": 0}


        if path == "/api/v1/supplier/orders" and method == "GET":
            return [
                {"date": "2026-06-15T09:15:00", "supplierArticle": "PRF-R0302P0101", "priceWithDisc": 1290, "finishedPrice": 1290, "totalPrice": 1690, "isCancel": False, "srid": "mock-order-1"},
                {"date": "2026-06-15T10:40:00", "supplierArticle": "SPA-PDEO11", "priceWithDisc": 690, "finishedPrice": 690, "totalPrice": 990, "isCancel": False, "srid": "mock-order-2"},
                {"date": "2026-06-15T11:05:00", "supplierArticle": "PRW-R0302P0101", "priceWithDisc": 1290, "finishedPrice": 1290, "totalPrice": 1690, "isCancel": False, "srid": "mock-order-3"},
            ]

        if path == "/api/v1/supplier/sales" and method == "GET":
            return [
                {"date": "2026-06-15T12:00:00", "supplierArticle": "PRF-R0302P0101", "saleID": "S123", "priceWithDisc": 1290, "finishedPrice": 1290, "forPay": 930, "srid": "mock-sale-1"},
                {"date": "2026-06-15T12:10:00", "supplierArticle": "SPA-PDEO11", "saleID": "S124", "priceWithDisc": 690, "finishedPrice": 690, "forPay": 480, "srid": "mock-sale-2"},
            ]

        raise WBApiError(f"Mock endpoint not implemented: {method} {path}")
