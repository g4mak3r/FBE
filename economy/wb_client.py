from __future__ import annotations

import math
import time
from datetime import date, datetime, timedelta
from typing import Any

import requests
from redact import redact

from .token_inspector import normalize_token

FINANCE_BASE_URL = "https://finance-api.wildberries.ru"
STATISTICS_BASE_URL = "https://statistics-api.wildberries.ru"
ADVERT_BASE_URL = "https://advert-api.wildberries.ru"


class EconomyWBError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        method: str = "",
        url: str = "",
        detail: str = "",
        request_id: str = "",
        api_code: str = "",
        origin: str = "",
        response_body: str = "",
        rate_limit: dict[str, Any] | None = None,
        attempts: int = 1,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.method = method
        self.url = url
        self.detail = detail
        self.request_id = request_id
        self.api_code = api_code
        self.origin = origin
        self.response_body = response_body
        self.rate_limit = dict(rate_limit or {})
        self.attempts = int(attempts)

    def diagnostic(self) -> dict[str, Any]:
        return {
            "status_code": self.status_code,
            "method": self.method,
            "url": self.url,
            "detail": self.detail,
            "request_id": self.request_id,
            "api_code": self.api_code,
            "origin": self.origin,
            "response_body": self.response_body[:4000],
            "rate_limit": self.rate_limit,
            "attempts": self.attempts,
        }


class EconomyWBClient:
    """Read-only WB API client for the Economy module.

    FBE 0.77.0 uses one shared WB token for every API category. Legacy
    category-specific parameters remain accepted for backward compatibility.
    """

    def __init__(
        self,
        *,
        default_token: str,
        finance_token: str = "",
        statistics_token: str = "",
        promotion_token: str = "",
        mock_mode: bool = False,
        max_retries: int = 4,
        max_wait_seconds: float = 180.0,
        retry_fallback_seconds: float = 5.0,
        request_timeout_seconds: float = 30.0,
    ):
        self.default_token = normalize_token(default_token)
        legacy_fallback = (
            normalize_token(statistics_token)
            or normalize_token(finance_token)
            or normalize_token(promotion_token)
        )
        shared_token = self.default_token or legacy_fallback
        self.finance_token = shared_token
        self.statistics_token = shared_token
        self.promotion_token = shared_token
        self.mock_mode = bool(mock_mode)
        self.max_retries = max(0, int(max_retries))
        self.max_wait_seconds = max(1.0, float(max_wait_seconds))
        self.retry_fallback_seconds = max(1.0, float(retry_fallback_seconds))
        self.request_timeout_seconds = max(2.0, float(request_timeout_seconds))
        self.session = requests.Session()
        self.request_events: list[dict[str, Any]] = []

    def token_for(self, source: str) -> str:
        if source == "finance":
            return self.finance_token
        if source == "statistics":
            return self.statistics_token
        if source == "promotion":
            return self.promotion_token
        raise ValueError(f"Неизвестный контур WB API: {source}")

    @staticmethod
    def _header_number(headers: Any, *names: str) -> tuple[float | None, str, str]:
        for name in names:
            value = headers.get(name) if headers is not None else None
            if value in (None, ""):
                continue
            raw = str(value).strip()
            try:
                return max(0.0, float(raw.replace(",", "."))), raw, name
            except Exception:
                continue
        return None, "", ""

    @classmethod
    def _header_delay_seconds(cls, headers: Any, *names: str) -> tuple[float | None, str, str, str]:
        value, raw, header_name = cls._header_number(headers, *names)
        if value is None:
            return None, raw, header_name, "missing"
        # WB documentation calls these values seconds, but production gateways
        # can return millisecond counters such as 39773 (= 39.773 s). Treating
        # them as seconds would freeze local FBE for eleven hours. Rate-limit
        # windows used by these methods are short, so values above ten minutes
        # are normalized from milliseconds defensively.
        if value > 600:
            return value / 1000.0, raw, header_name, "milliseconds"
        return value, raw, header_name, "seconds"

    def _rate_limit_info(self, response: requests.Response) -> dict[str, Any]:
        retry, retry_raw, retry_header, retry_unit = self._header_delay_seconds(
            response.headers,
            "X-Ratelimit-Retry",
            "X-RateLimit-Retry",
            "Retry-After",
        )
        reset, reset_raw, reset_header, reset_unit = self._header_delay_seconds(
            response.headers,
            "X-Ratelimit-Reset",
            "X-RateLimit-Reset",
        )
        remaining, remaining_raw, remaining_header = self._header_number(
            response.headers,
            "X-Ratelimit-Remaining",
            "X-RateLimit-Remaining",
        )
        limit, limit_raw, limit_header = self._header_number(
            response.headers,
            "X-Ratelimit-Limit",
            "X-RateLimit-Limit",
        )
        return {
            "retry_seconds": retry,
            "retry_raw": retry_raw,
            "retry_header": retry_header,
            "retry_interpreted_as": retry_unit,
            "reset_seconds": reset,
            "reset_raw": reset_raw,
            "reset_header": reset_header,
            "reset_interpreted_as": reset_unit,
            "remaining": remaining,
            "remaining_raw": remaining_raw,
            "remaining_header": remaining_header,
            "limit": limit,
            "limit_raw": limit_raw,
            "limit_header": limit_header,
        }

    def drain_request_events(self) -> list[dict[str, Any]]:
        events = list(self.request_events)
        self.request_events.clear()
        return events

    def _request(
        self,
        method: str,
        url: str,
        *,
        token: str,
        allow_no_content: bool = True,
        **kwargs: Any,
    ) -> Any:
        if self.mock_mode:
            return self._mock(method, url, **kwargs)
        token = normalize_token(token)
        if not token:
            raise EconomyWBError(
                "Для этого источника не задан токен WB API",
                method=method,
                url=url,
                detail="token is missing",
            )
        headers = dict(kwargs.pop("headers", {}) or {})
        headers["Authorization"] = token
        attempts = self.max_retries + 1
        total_wait = 0.0
        last_response: requests.Response | None = None
        last_exception: requests.RequestException | None = None
        retryable_statuses = {429, 500, 502, 503, 504}
        for attempt in range(1, attempts + 1):
            try:
                response = self.session.request(
                    method,
                    url,
                    headers=headers,
                    timeout=(min(5.0, self.request_timeout_seconds), self.request_timeout_seconds),
                    **kwargs,
                )
            except requests.RequestException as exc:
                last_exception = exc
                retry_seconds = min(
                    30.0,
                    self.retry_fallback_seconds * (2 ** (attempt - 1)),
                )
                will_retry = attempt < attempts and total_wait + retry_seconds <= self.max_wait_seconds
                self.request_events.append({
                    "at": datetime.now().isoformat(timespec="seconds"),
                    "method": method,
                    "url": url,
                    "status_code": None,
                    "attempt": attempt,
                    "error": redact(exc, token),
                    "wait_seconds": retry_seconds if will_retry else 0,
                    "will_retry": will_retry,
                })
                if not will_retry:
                    break
                time.sleep(retry_seconds)
                total_wait += retry_seconds
                continue

            last_response = response
            rate_limit = self._rate_limit_info(response)
            event = {
                "at": datetime.now().isoformat(timespec="seconds"),
                "method": method,
                "url": url,
                "status_code": response.status_code,
                "attempt": attempt,
                "rate_limit": rate_limit,
            }
            if response.status_code not in retryable_statuses:
                self.request_events.append(event)
                break

            retry_seconds = rate_limit.get("retry_seconds")
            if retry_seconds is None:
                retry_seconds = rate_limit.get("reset_seconds")
            if retry_seconds is None:
                retry_seconds = min(
                    30.0,
                    self.retry_fallback_seconds * (2 ** (attempt - 1)),
                )
            retry_seconds = max(1.0, float(retry_seconds)) + 0.35
            event["wait_seconds"] = retry_seconds
            event["will_retry"] = attempt < attempts and total_wait + retry_seconds <= self.max_wait_seconds
            self.request_events.append(event)
            if not event["will_retry"]:
                break
            time.sleep(retry_seconds)
            total_wait += retry_seconds

        if last_response is None:
            detail = redact(last_exception or "network request failed", token)
            raise EconomyWBError(
                f"WB API недоступен для {method} {url}: {detail}",
                method=method,
                url=url,
                detail=detail,
                attempts=attempt,
            ) from last_exception
        response = last_response
        if response.status_code == 204 and allow_no_content:
            return None
        if not response.ok:
            body = redact(response.text, token)[:4000]
            payload: dict[str, Any] = {}
            try:
                parsed = response.json()
                if isinstance(parsed, dict):
                    payload = {key: redact(value, token) for key, value in parsed.items()}
            except Exception:
                pass
            detail = str(payload.get("detail") or payload.get("title") or body or "Ошибка WB API")
            request_id = str(payload.get("requestId") or payload.get("request_id") or "")
            api_code = str(payload.get("code") or "")
            origin = str(payload.get("origin") or "")
            rate_limit = self._rate_limit_info(response)
            retry_value = rate_limit.get("retry_seconds") or rate_limit.get("reset_seconds")
            suffix = f"; повторить через {math.ceil(float(retry_value))} сек." if retry_value is not None else ""
            message = f"WB API {response.status_code} {method} {url}: {detail}{suffix}"
            if request_id:
                message += f"; requestId: {request_id}"
            raise EconomyWBError(
                message,
                status_code=response.status_code,
                method=method,
                url=url,
                detail=detail,
                request_id=request_id,
                api_code=api_code,
                origin=origin,
                response_body=body,
                rate_limit=rate_limit,
                attempts=attempt,
            )
        if not response.content:
            return None
        try:
            return response.json()
        except Exception as exc:
            raise EconomyWBError(
                f"WB API вернул не JSON для {url}: {exc}",
                status_code=response.status_code,
                method=method,
                url=url,
                detail=str(exc),
                response_body=redact(response.text, token)[:4000],
                rate_limit=self._rate_limit_info(response),
                attempts=attempt,
            ) from exc

    def check_connection(self, source: str) -> Any:
        urls = {
            "statistics": f"{STATISTICS_BASE_URL}/ping",
            "finance": f"{FINANCE_BASE_URL}/ping",
            "promotion": f"{ADVERT_BASE_URL}/ping",
        }
        return self._request(
            "GET",
            urls[source],
            token=self.token_for(source),
            allow_no_content=True,
        )

    def get_orders(self, date_from: str) -> list[dict[str, Any]]:
        data = self._request(
            "GET",
            f"{STATISTICS_BASE_URL}/api/v1/supplier/orders",
            token=self.statistics_token,
            params={"dateFrom": date_from, "flag": 0},
        )
        return data if isinstance(data, list) else []

    def get_sales(self, date_from: str) -> list[dict[str, Any]]:
        data = self._request(
            "GET",
            f"{STATISTICS_BASE_URL}/api/v1/supplier/sales",
            token=self.statistics_token,
            params={"dateFrom": date_from, "flag": 0},
        )
        return data if isinstance(data, list) else []

    def get_finance_reports(
        self,
        date_from: str,
        date_to: str,
        *,
        period: str = "daily",
        limit: int = 1000,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Return official WB sales-report summaries.

        ``period`` is explicit because FBE 0.77.0 stores daily summaries for
        arbitrary date filters and weekly summaries only for reconciliation.
        The endpoint is paginated with ``limit`` and ``offset``.
        """
        period = str(period or "daily").strip().lower()
        if period not in {"daily", "weekly"}:
            raise ValueError("period must be 'daily' or 'weekly'")
        data = self._request(
            "POST",
            f"{FINANCE_BASE_URL}/api/finance/v1/sales-reports/list",
            token=self.finance_token,
            json={
                "dateFrom": date_from,
                "dateTo": date_to,
                "limit": min(max(int(limit), 1), 1000),
                "offset": max(int(offset), 0),
                "period": period,
            },
        )
        if isinstance(data, list):
            return [row for row in data if isinstance(row, dict)]
        if isinstance(data, dict):
            for key in ("reports", "data", "items", "result"):
                value = data.get(key)
                if isinstance(value, list):
                    return [row for row in value if isinstance(row, dict)]
        return []

    def get_finance_details(
        self,
        date_from: str,
        date_to: str,
        *,
        rrd_id: int = 0,
        limit: int = 100000,
    ) -> list[dict[str, Any]]:
        data = self._request(
            "POST",
            f"{FINANCE_BASE_URL}/api/finance/v1/sales-reports/detailed",
            token=self.finance_token,
            json={
                "dateFrom": date_from,
                "dateTo": date_to,
                "limit": min(max(int(limit), 1), 100000),
                "rrdId": int(rrd_id),
                "period": "daily",
            },
        )
        return data if isinstance(data, list) else []

    def get_campaign_ids(self) -> list[int]:
        data = self._request(
            "GET",
            f"{ADVERT_BASE_URL}/adv/v1/promotion/count",
            token=self.promotion_token,
        ) or {}
        ids: list[int] = []
        for group in data.get("adverts", []) if isinstance(data, dict) else []:
            status = int(group.get("status") or 0)
            if status not in {7, 9, 11}:
                continue
            for item in group.get("advert_list", []) or []:
                try:
                    ids.append(int(item.get("advertId")))
                except Exception:
                    continue
        return sorted(set(ids))

    def get_campaign_info(self, ids: list[int]) -> list[dict[str, Any]]:
        if not ids:
            return []
        rows: list[dict[str, Any]] = []
        for offset in range(0, len(ids), 50):
            batch = ids[offset:offset + 50]
            data = self._request(
                "GET",
                f"{ADVERT_BASE_URL}/api/advert/v2/adverts",
                token=self.promotion_token,
                params={"ids": ",".join(str(int(value)) for value in batch)},
            )
            if isinstance(data, list):
                rows.extend(row for row in data if isinstance(row, dict))
            elif isinstance(data, dict):
                for key in ("adverts", "items", "data", "result"):
                    value = data.get(key)
                    if isinstance(value, list):
                        rows.extend(row for row in value if isinstance(row, dict))
                        break
        return rows

    def get_ad_expenses(self, date_from: str, date_to: str) -> list[dict[str, Any]]:
        data = self._request(
            "GET",
            f"{ADVERT_BASE_URL}/adv/v1/upd",
            token=self.promotion_token,
            params={"from": date_from, "to": date_to},
        )
        if isinstance(data, list):
            return [row for row in data if isinstance(row, dict)]
        if isinstance(data, dict):
            for key in ("expenses", "items", "data", "result"):
                value = data.get(key)
                if isinstance(value, list):
                    return [row for row in value if isinstance(row, dict)]
        return []

    def get_campaign_stats(self, ids: list[int], date_from: str, date_to: str) -> list[dict[str, Any]]:
        if not ids:
            return []
        rows: list[dict[str, Any]] = []
        for offset in range(0, len(ids), 50):
            batch = ids[offset:offset + 50]
            data = self._request(
                "GET",
                f"{ADVERT_BASE_URL}/adv/v3/fullstats",
                token=self.promotion_token,
                params={
                    "ids": ",".join(str(int(x)) for x in batch),
                    "beginDate": date_from,
                    "endDate": date_to,
                },
            )
            if isinstance(data, list):
                rows.extend(row for row in data if isinstance(row, dict))
        return rows

    def _mock(self, method: str, url: str, **kwargs: Any) -> Any:
        today = date.today()
        if url.endswith("/supplier/orders"):
            return [
                {
                    "srid": f"mock-order-{i}",
                    "date": str(today - timedelta(days=i % 8)),
                    "lastChangeDate": str(today),
                    "supplierArticle": f"MOCK-{i % 3 + 1}",
                    "nmId": 9000000 + i % 3,
                    "finishedPrice": 599 + (i % 3) * 300,
                    "isCancel": i in {7, 14},
                    "warehouseName": "Коледино",
                    "regionName": "Москва",
                }
                for i in range(18)
            ]
        if url.endswith("/supplier/sales"):
            return [
                {
                    "saleID": ("R" if i in {5, 13} else "S") + f"mock-{i}",
                    "srid": f"mock-order-{i}",
                    "date": str(today - timedelta(days=i % 8)),
                    "supplierArticle": f"MOCK-{i % 3 + 1}",
                    "nmId": 9000000 + i % 3,
                    "finishedPrice": 599 + (i % 3) * 300,
                }
                for i in range(16)
            ]
        if url.endswith("/sales-reports/list"):
            body = kwargs.get("json") or {}
            period = str(body.get("period") or "weekly")
            if period == "daily":
                rows = []
                for i in range(8):
                    report_day = today - timedelta(days=i)
                    rows.append({
                        "reportId": 2234567000 + i,
                        "dateFrom": str(report_day),
                        "dateTo": str(report_day),
                        "createDate": str(report_day + timedelta(days=1)),
                        "currency": "RUB",
                        "reportType": 1,
                        "retailAmountSum": str(1500 + i * 75),
                        "forPaySum": str(940 + i * 45),
                        "deliveryServiceSum": str(110 + i * 4),
                        "paidStorageSum": str(12 + i),
                        "paidAcceptanceSum": "0",
                        "deductionSum": str(220 + i * 3),
                        "penaltySum": "0",
                        "additionalPaymentSum": "0",
                        "bankPaymentSum": str(598 + i * 37),
                    })
                offset = int(body.get("offset") or 0)
                limit = int(body.get("limit") or 1000)
                return rows[offset:offset + limit]
            return [{
                "reportId": 1234567,
                "dateFrom": str(today - timedelta(days=7)),
                "dateTo": str(today),
                "createDate": str(today),
                "currency": "RUB",
                "reportType": 1,
                "retailAmountSum": "12480.00",
                "forPaySum": "7800.00",
                "deliveryServiceSum": "720.00",
                "paidStorageSum": "90.00",
                "paidAcceptanceSum": "0",
                "deductionSum": "1850.00",
                "penaltySum": "0",
                "additionalPaymentSum": "0",
                "bankPaymentSum": "5140.00",
            }]
        if url.endswith("/sales-reports/detailed"):
            rows = []
            for i in range(16):
                returned = i in {5, 13}
                amount = 599 + (i % 3) * 300
                rows.append({
                    "rrdId": 100000 + i,
                    "reportId": 1234567,
                    "saleDate": str(today - timedelta(days=i % 8)),
                    "rrDate": str(today - timedelta(days=i % 8)),
                    "vendorCode": f"MOCK-{i % 3 + 1}",
                    "nmId": 9000000 + i % 3,
                    "srid": f"mock-order-{i}",
                    "docTypeName": "Возврат" if returned else "Продажа",
                    "quantity": 1,
                    "retailAmount": str(-amount if returned else amount),
                    "retailPriceWithDisc": str(-amount if returned else amount),
                    "ppvzSalesCommission": str((-amount if returned else amount) * 0.24),
                    "forPay": str((-amount if returned else amount) * 0.76),
                    "acquiringFee": str(12 if not returned else -12),
                    "deliveryService": str(48 if not returned else 64),
                    "storageFee": "2.5",
                    "deduction": "0",
                    "penalty": "0",
                    "additionalPayment": "0",
                    "rebillLogisticCost": "0",
                    "acceptance": "0",
                    "title": f"Тестовый товар {i % 3 + 1}",
                })
            return rows
        if url.endswith("/promotion/count"):
            return {"adverts": [{"type": 9, "status": 9, "count": 1, "advert_list": [{"advertId": 777001, "changeTime": str(today)}]}], "all": 1}
        if url.endswith("/api/advert/v2/adverts"):
            raw_ids = str((kwargs.get("params") or {}).get("ids") or "777001")
            return [
                {
                    "advertId": int(value), "name": f"Тестовая кампания {value}",
                    "status": 9, "type": 9, "paymentType": "Баланс",
                    "dailyBudget": 1000, "changeTime": str(today),
                }
                for value in raw_ids.split(",") if value.strip().isdigit()
            ]
        if url.endswith("/adv/v1/upd"):
            return [
                {
                    "advertId": 777001, "campName": "Тестовая кампания 777001",
                    "advertType": 9, "paymentType": "Баланс",
                    "updNum": i + 1, "updTime": f"{today - timedelta(days=i)}T12:00:00+03:00",
                    "updSum": 420 + i * 5,
                }
                for i in range(7)
            ]
        if url.endswith("/fullstats"):
            return [{
                "advertId": 777001,
                "days": [
                    {
                        "date": str(today - timedelta(days=i)),
                        "views": 900 + i * 10,
                        "clicks": 45 + i,
                        "sum": 420 + i * 5,
                        "atbs": 15,
                        "orders": 7,
                        "shks": 5,
                        "sum_price": 4500,
                        "apps": [{"nm": [{"nmId": 9000000 + j, "views": 300, "clicks": 15, "sum": 140 + j * 5, "atbs": 5, "orders": 2, "shks": 1, "sum_price": 1200 + j * 200} for j in range(3)]}],
                    }
                    for i in range(7)
                ],
            }]
        return None
