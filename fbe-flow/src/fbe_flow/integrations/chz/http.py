"""Bounded HTTPS transport with no redirects, credential logging or implicit retries."""

import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from urllib.parse import urlencode

from fbe_flow.core.errors import InvalidInput


class RemoteError(Exception):
    def __init__(self, status: int | None, code: str, details=None):
        super().__init__(code)
        self.status = status
        self.code = code
        self.details = details


@dataclass(frozen=True)
class Reply:
    status: int
    data: object
    headers: dict[str, str]


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class HttpTransport:
    def __init__(self):
        self.opener = urllib.request.build_opener(NoRedirect())

    def request(self, method, url, *, params=None, headers=None, body=None) -> Reply:
        if not url.startswith("https://"):
            raise InvalidInput("API ЧЗ требует HTTPS")
        if params:
            url += "?" + urlencode(params, doseq=True)
        data = (
            None
            if body is None
            else json.dumps(
                body, ensure_ascii=False, allow_nan=False, separators=(",", ":")
            ).encode("utf-8")
        )
        request = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json; charset=utf-8",
                **(headers or {}),
            },
        )
        try:
            with self.opener.open(request, timeout=20) as response:
                raw = response.read(30 * 1024 * 1024 + 1)
                if len(raw) > 30 * 1024 * 1024:
                    raise RemoteError(None, "response_too_large")
                try:
                    value = json.loads(raw) if raw else None
                except (ValueError, UnicodeError) as exc:
                    raise RemoteError(None, "invalid_response") from exc
                return Reply(response.status, value, dict(response.headers))
        except urllib.error.HTTPError as exc:
            if exc.code == 304:
                return Reply(304, None, dict(exc.headers))
            # Never return URLs or arbitrary request-bearing upstream exception text.
            try:
                details = json.loads(exc.read(64 * 1024))
            except (ValueError, UnicodeError):
                details = None
            raise RemoteError(exc.code, f"http_{exc.code}", details) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise RemoteError(None, "transport_error") from exc


def redact(value, secrets):
    if isinstance(value, dict):
        return {
            str(k): redact(v, secrets)
            for k, v in value.items()
            if not re.search(r"token|apikey|secret|password|authorization", str(k), re.I)
        }
    if isinstance(value, list):
        return [redact(item, secrets) for item in value]
    if isinstance(value, str):
        for secret in secrets:
            if secret:
                value = value.replace(secret, "[скрыто]")
        return value
    return value
