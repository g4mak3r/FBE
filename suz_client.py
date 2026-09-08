from __future__ import annotations

import base64
import json
import datetime as _dt
import email.utils
import os
import platform
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlencode

import requests
from redact import redact


class SuzApiError(RuntimeError):
    """Structured API error retaining the upstream True API response."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        response_body: Any = None,
        request_id: str = "",
        url: str = "",
        stage: str = "",
    ):
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body
        self.request_id = request_id
        self.url = url
        self.stage = stage


@dataclass
class SuzToken:
    value: str
    expires_at: float


class SuzClient:
    """Minimal OMS Cloud / True API client for the production environment.

    Signing is delegated to Windows PowerShell and the certificate already installed
    in the Windows certificate store. The private key and token PIN are never read by FBE.
    """

    def __init__(self, *, base_dir: str | Path, settings_loader):
        self.base_dir = Path(base_dir).resolve()
        self.settings_loader = settings_loader
        # requests.Session is not designed to be mutated/shared concurrently.
        # FBE can run several long True API/SUZ jobs at once, therefore each
        # worker thread owns its own connection pool.
        self._http_local = threading.local()
        self._token: SuzToken | None = None
        self._true_token: SuzToken | None = None
        self._token_lock = threading.Lock()

    def settings(self) -> dict[str, Any]:
        return dict(self.settings_loader() or {})

    def reset_token(self) -> None:
        with self._token_lock:
            self._token = None
            self._true_token = None

    def _diagnostic_log(self, stage: str, message: str) -> None:
        """Write non-secret SUZ diagnostics to a local log file."""
        try:
            log_dir = Path(self.settings().get("workspace_root") or self.base_dir / "data") / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            timestamp = _dt.datetime.now().isoformat(timespec="seconds")
            safe = redact(message, *(str(v) for k, v in self.settings().items() if any(part in k for part in ("token", "inn", "oms_id", "connection_id", "thumbprint")) and v)).replace("\r", " ").replace("\n", " ")
            with (log_dir / "suz.log").open("a", encoding="utf-8") as handle:
                handle.write(f"{timestamp} [{stage}] {safe}\n")
        except Exception:
            pass

    @staticmethod
    def _clean_thumbprint(value: Any) -> str:
        return str(value or "").replace(" ", "").strip().upper()

    def _require_windows(self) -> None:
        if platform.system().lower() != "windows":
            raise SuzApiError("Подписание УКЭП доступно только в установленном FBE на Windows.")

    def _powershell(self) -> str:
        return str(self.settings().get("powershell_exe") or "powershell.exe")

    def _run_powershell(self, script: Path, args: list[str], timeout: int = 120) -> str:
        if bool(self.settings().get("mock_mode", False)):
            raise SuzApiError("DEMO MODE: certificate operations are disabled")
        self._require_windows()
        cmd = [
            self._powershell(),
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
            *args,
        ]
        try:
            completed = subprocess.run(
                cmd,
                cwd=str(self.base_dir),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                check=False,
            )
        except FileNotFoundError as exc:
            raise SuzApiError("PowerShell не найден. Проверьте powershell.exe в Windows.") from exc
        except subprocess.TimeoutExpired as exc:
            raise SuzApiError("Истекло время ожидания подписи УКЭП.") from exc

        stdout = (completed.stdout or "").strip()
        stderr = (completed.stderr or "").strip()
        if completed.returncode != 0:
            message = stderr or stdout or f"PowerShell завершился с кодом {completed.returncode}"
            raise SuzApiError(message)
        return stdout

    def list_certificates(self) -> list[dict[str, Any]]:
        if bool(self.settings().get("mock_mode", False)):
            return [{
                "thumbprint": "DEMO000000000000000000000000000000000000",
                "subject": "CN=FBE DEMO CERTIFICATE",
                "issuer": "CN=FBE DEMO",
            }]
        script = self.base_dir / "tools" / "list_signing_certificates_json.ps1"
        raw = self._run_powershell(script, [], timeout=60)
        if not raw:
            return []
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise SuzApiError(f"Не удалось разобрать список сертификатов: {raw[:300]}") from exc
        if isinstance(data, dict):
            data = [data]
        return [dict(item) for item in data if isinstance(item, dict)]

    def _sign_bytes(self, content: bytes, *, detached: bool) -> str:
        settings = self.settings()
        thumbprint = self._clean_thumbprint(settings.get("suz_cert_thumbprint"))
        if not thumbprint:
            raise SuzApiError("Сначала выберите сертификат УКЭП в окне Честного Знака.")
        script = self.base_dir / "tools" / "sign_cms.ps1"
        tmp_dir = Path(self.settings().get("workspace_root") or self.base_dir / "data") / "runtime" / "suz"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        fd, temp_path = tempfile.mkstemp(prefix="sign_", suffix=".bin", dir=str(tmp_dir))
        os.close(fd)
        path = Path(temp_path)
        try:
            path.write_bytes(content)
            args = ["-Thumbprint", thumbprint, "-InputFile", str(path)]
            if detached:
                args.append("-Detached")
            signature = self._run_powershell(script, args, timeout=180).strip()
            try:
                base64.b64decode(signature, validate=True)
            except Exception as exc:
                raise SuzApiError("PowerShell вернул некорректную Base64-подпись.") from exc
            return signature
        finally:
            try:
                path.unlink(missing_ok=True)
            except Exception:
                pass

    @staticmethod
    def _json_bytes(payload: dict[str, Any]) -> bytes:
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

    @staticmethod
    def _response_error_parts(response: requests.Response) -> tuple[str, Any, str]:
        request_id = (
            response.headers.get("X-RequestId")
            or response.headers.get("X-Request-ID")
            or response.headers.get("Request-Id")
            or ""
        )
        try:
            body: Any = response.json()
        except Exception:
            body = (response.text or "").strip() or response.reason
        if isinstance(body, dict):
            message = str(
                body.get("error_message")
                or body.get("message")
                or body.get("error")
                or body.get("description")
                or json.dumps(body, ensure_ascii=False)
            )
        elif isinstance(body, list):
            message = json.dumps(body, ensure_ascii=False)
        else:
            message = str(body or response.reason)
        return message, body, str(request_id or "")

    @staticmethod
    def _find_string(payload: Any, keys: tuple[str, ...]) -> str:
        """Find a scalar in common API wrappers without depending on one revision."""
        if isinstance(payload, dict):
            for key in keys:
                value = payload.get(key)
                if isinstance(value, (str, int)) and str(value).strip():
                    return str(value).strip()
            for key in ("result", "data", "document", "body", "response"):
                found = SuzClient._find_string(payload.get(key), keys)
                if found:
                    return found
        elif isinstance(payload, list):
            for item in payload:
                found = SuzClient._find_string(item, keys)
                if found:
                    return found
        return ""

    def _http_session(self) -> requests.Session:
        session = getattr(self._http_local, "session", None)
        if session is None:
            session = requests.Session()
            self._http_local.session = session
        return session

    def _reset_http_session(self) -> None:
        session = getattr(self._http_local, "session", None)
        if session is not None:
            try:
                session.close()
            except Exception:
                pass
        self._http_local.session = requests.Session()

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
                        parsed = parsed.replace(tzinfo=_dt.timezone.utc)
                    return min(
                        5.0,
                        max(0.0, (parsed - _dt.datetime.now(_dt.timezone.utc)).total_seconds()),
                    )
                except (TypeError, ValueError, OverflowError):
                    pass
        return min(2.0, 0.3 * (2 ** attempt))

    def _request(self, method: str, url: str, *, stage: str = "HTTP", retry_connection: bool = False, **kwargs) -> requests.Response:
        settings = self.settings()
        if bool(settings.get("mock_mode", False)):
            raise SuzApiError(
                "DEMO MODE: внешние запросы Честного Знака отключены.",
                url=url,
                stage=stage,
            )
        connect_timeout = float(settings.get("suz_connect_timeout_seconds") or 4.0)
        read_timeout = float(settings.get("suz_timeout_seconds") or 20.0)
        if retry_connection:
            read_timeout = min(read_timeout, float(settings.get("suz_status_timeout_seconds") or 15.0))
        timeout = (max(1.0, connect_timeout), max(3.0, read_timeout))
        self._diagnostic_log(stage, f"{method} {url}")
        # Only callers that explicitly mark an operation as safe/read-only get
        # transport and rate-limit retries. Legal document/order POSTs remain a
        # single attempt so an ambiguous disconnect cannot duplicate a mutation.
        attempts = 3 if retry_connection else 1
        response = None
        last_exc: requests.RequestException | None = None
        for attempt in range(attempts):
            try:
                response = self._http_session().request(method, url, timeout=timeout, **kwargs)
                if (
                    retry_connection
                    and response.status_code in {429, 500, 502, 503, 504}
                    and attempt + 1 < attempts
                ):
                    delay = self._retry_delay(response, attempt)
                    self._diagnostic_log(
                        stage,
                        f"HTTP {response.status_code} attempt {attempt + 1}/{attempts}; retry in {delay:.2f}s",
                    )
                    response.close()
                    time.sleep(delay)
                    continue
                break
            except requests.RequestException as exc:
                last_exc = exc
                self._diagnostic_log(stage, f"connection error attempt {attempt + 1}/{attempts}: {exc}")
                if attempt + 1 < attempts:
                    # CRPT occasionally closes an idle keep-alive socket without an HTTP
                    # response. Re-open the pool and retry status/read operations once.
                    self._reset_http_session()
                    time.sleep(0.18)
                    continue
                raise SuzApiError(
                    f"{stage}: ошибка соединения: {exc}",
                    url=url,
                    stage=stage,
                ) from exc
        if response is None:
            raise SuzApiError(f"{stage}: ошибка соединения: {last_exc}", url=url, stage=stage)
        if response.status_code < 200 or response.status_code >= 300:
            message, body, request_id = self._response_error_parts(response)
            suffix = f" · requestId {request_id}" if request_id else ""
            rendered = f"HTTP {response.status_code}: {message}{suffix}"
            self._diagnostic_log(stage, rendered)
            raise SuzApiError(
                f"{stage}: {rendered}",
                status_code=response.status_code,
                response_body=body,
                request_id=request_id,
                url=url,
                stage=stage,
            )
        self._diagnostic_log(stage, f"HTTP {response.status_code}")
        return response

    def _true_api_url(self, path: str, *, version: int = 3) -> str:
        settings = self.settings()
        if int(version) == 4:
            base = str(
                settings.get("suz_true_api_v4_base_url")
                or "https://markirovka.crpt.ru/api/v4/true-api"
            ).rstrip("/")
        else:
            base = str(
                settings.get("suz_true_api_base_url")
                or "https://markirovka.crpt.ru/api/v3/true-api"
            ).rstrip("/")
        return base + "/" + path.lstrip("/")

    def _oms_url(self, path: str) -> str:
        base = str(self.settings().get("suz_base_url") or "https://suzgrid.crpt.ru").rstrip("/")
        return base + "/" + path.lstrip("/")

    def get_client_token(self, *, force: bool = False) -> str:
        with self._token_lock:
            now = time.time()
            if not force and self._token and self._token.expires_at > now + 120:
                return self._token.value

            settings = self.settings()
            connection = str(settings.get("suz_connection_id") or "").strip()
            if not connection:
                raise SuzApiError("Настройки: не заполнен идентификатор подключения omsConnection.")

            try:
                challenge_response = self._request(
                    "GET",
                    self._true_api_url("auth/key"),
                    stage="1/4 Получение challenge True API",
                    retry_connection=True,
                    headers={"Accept": "application/json"},
                )
                challenge = challenge_response.json()
            except ValueError as exc:
                raise SuzApiError("1/4 Получение challenge True API: сервер вернул не JSON") from exc

            uuid = str(challenge.get("uuid") or "").strip()
            data = str(challenge.get("data") or "")
            if not uuid or not data:
                self._diagnostic_log("1/4 Получение challenge True API", f"unexpected body keys: {list(challenge) if isinstance(challenge, dict) else type(challenge)}")
                raise SuzApiError("1/4 Получение challenge True API: ответ не содержит uuid и data.")

            try:
                # True API requires an attached CAdES-BES/CMS signature of the exact
                # random data bytes. tools/sign_cms.ps1 uses CryptoPro CAdESCOM first.
                signature = self._sign_bytes(data.encode("utf-8"), detached=False)
                self._diagnostic_log("2/4 Подпись challenge", f"signature generated, base64 chars={len(signature)}")
            except SuzApiError as exc:
                self._diagnostic_log("2/4 Подпись challenge", str(exc))
                raise SuzApiError(f"2/4 Подпись challenge УКЭП: {exc}") from exc

            payload: dict[str, Any] = {"uuid": uuid, "data": signature}
            auth_inn = str(settings.get("suz_auth_inn") or "").strip()
            if auth_inn:
                payload["inn"] = auth_inn

            try:
                auth_response = self._request(
                    "POST",
                    self._true_api_url(f"auth/simpleSignIn/{connection}"),
                    stage="3/4 Получение clientToken",
                    headers={"Accept": "application/json", "Content-Type": "application/json"},
                    data=self._json_bytes(payload),
                )
                response = auth_response.json()
            except ValueError as exc:
                raise SuzApiError("3/4 Получение clientToken: сервер вернул не JSON") from exc

            token = str(response.get("token") or response.get("uuidToken") or "").strip()
            if not token:
                message = str(response.get("error_message") or response.get("description") or response or "True API не вернул токен")
                self._diagnostic_log("3/4 Получение clientToken", message)
                raise SuzApiError(f"3/4 Получение clientToken: {message}")

            expires_at = now + 9.5 * 60 * 60
            self._token = SuzToken(token, expires_at)
            self._diagnostic_log("3/4 Получение clientToken", "token received and kept only in RAM")
            return token


    def get_true_api_token(self, *, force: bool = False) -> str:
        """Return a bearer token for True API itself.

        This is deliberately separate from the OMS clientToken. True API authentication
        ends at /auth/simpleSignIn, while the OMS token uses
        /auth/simpleSignIn/{omsConnection}. Both tokens are kept only in RAM.
        """
        with self._token_lock:
            now = time.time()
            if not force and self._true_token and self._true_token.expires_at > now + 120:
                return self._true_token.value

            try:
                challenge_response = self._request(
                    "GET",
                    self._true_api_url("auth/key"),
                    stage="True API: получение challenge",
                    retry_connection=True,
                    headers={"Accept": "application/json"},
                )
                challenge = challenge_response.json()
            except ValueError as exc:
                raise SuzApiError("True API: challenge получен не в формате JSON") from exc

            uuid = str(challenge.get("uuid") or "").strip() if isinstance(challenge, dict) else ""
            data = str(challenge.get("data") or "") if isinstance(challenge, dict) else ""
            if not uuid or not data:
                raise SuzApiError("True API: ответ challenge не содержит uuid и data")

            signature = self._sign_bytes(data.encode("utf-8"), detached=False)
            # True API unified authentication accepts the signed challenge only.
            # The participant INN belongs to business documents, not to simpleSignIn.
            payload: dict[str, Any] = {"uuid": uuid, "data": signature}

            try:
                auth_response = self._request(
                    "POST",
                    self._true_api_url("auth/simpleSignIn"),
                    stage="True API: получение bearer token",
                    headers={"Accept": "application/json", "Content-Type": "application/json"},
                    data=self._json_bytes(payload),
                )
                response = auth_response.json()
            except ValueError as exc:
                raise SuzApiError("True API: сервер вернул не JSON при авторизации") from exc

            token = ""
            life_time_minutes = 30.0
            if isinstance(response, dict):
                token = str(response.get("token") or response.get("uuidToken") or "").strip()
                try:
                    life_time_minutes = float(response.get("life_time") or response.get("lifeTime") or 30)
                except Exception:
                    life_time_minutes = 30.0
            if not token:
                raise SuzApiError(f"True API не вернул bearer token: {response}")

            # Use the server-provided lifetime and keep a safety margin.
            expires_at = now + max(5.0, life_time_minutes - 1.0) * 60
            self._true_token = SuzToken(token, expires_at)
            self._diagnostic_log("True API auth", "bearer token received and kept only in RAM")
            return token

    @staticmethod
    def identification_code(raw_code: str) -> str:
        """Strip the crypto verification part from a full marking code.

        True API documents and /cises/info use the identification code (01+GTIN,
        21+serial) without the verification key/code. The first ASCII 29 separator
        terminates that identification part for perfumery and chemistry codes.
        """
        value = str(raw_code or "")
        if not value:
            raise SuzApiError("Пустой КИЗ")
        return value.split("\x1d", 1)[0]

    def get_cises_info(self, raw_codes: Iterable[str], *, product_group: str) -> list[dict[str, Any]]:
        codes = [self.identification_code(code) for code in raw_codes if str(code or "")]
        if not codes:
            return []
        token = self.get_true_api_token()
        query = urlencode({"pg": str(product_group or "").strip()})
        response = self._request(
            "POST",
            self._true_api_url(f"cises/info?{query}"),
            stage="True API: проверка статусов КИ",
            retry_connection=True,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Authorization": f"Bearer {token}",
            },
            data=json.dumps(codes, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
        )
        try:
            data = response.json()
        except ValueError as exc:
            raise SuzApiError("True API /cises/info вернул не JSON") from exc
        if isinstance(data, dict):
            # True API deployments have used several equivalent wrappers.
            # Preserve an empty list (it is meaningful) and unwrap nested data.
            for key in ("results", "items", "data", "result", "cises"):
                value = data.get(key)
                if isinstance(value, list):
                    data = value
                    break
                if isinstance(value, dict):
                    nested = next(
                        (
                            value.get(nested_key)
                            for nested_key in ("results", "items", "data", "cises")
                            if isinstance(value.get(nested_key), list)
                        ),
                        None,
                    )
                    if nested is not None:
                        data = nested
                        break
            else:
                data = [data]
        return [dict(item) for item in (data or []) if isinstance(item, dict)]

    def create_true_document(
        self,
        *,
        product_group: str,
        document_type: str,
        document_payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Sign and submit a MANUAL JSON document through the unified True API method."""
        product_group = str(product_group or "").strip()
        document_type = str(document_type or "").strip()
        if not product_group or not document_type:
            raise SuzApiError("Не указаны товарная группа или тип документа True API")

        inner_body = self._json_bytes(document_payload)
        signature = self._sign_bytes(inner_body, detached=True)
        outer_payload = {
            "document_format": "MANUAL",
            "product_document": base64.b64encode(inner_body).decode("ascii"),
            "type": document_type,
            "signature": signature,
        }
        token = self.get_true_api_token()
        query = urlencode({"pg": product_group})
        response = self._request(
            "POST",
            self._true_api_url(f"lk/documents/create?{query}"),
            stage=f"True API: отправка документа {document_type}",
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Authorization": f"Bearer {token}",
            },
            data=self._json_bytes(outer_payload),
        )
        try:
            result: Any = response.json()
        except ValueError:
            result = (response.text or "").strip().strip('"')
        if isinstance(result, str):
            document_id = result.strip()
            result = {"uuid": document_id}
        else:
            document_id = self._find_string(
                result, ("uuid", "id", "value", "document_id", "documentId")
            )
        if not document_id:
            raise SuzApiError(f"True API не вернул идентификатор документа: {result}")
        result = dict(result) if isinstance(result, dict) else {"response": result}
        result["uuid"] = document_id
        result["requestDocument"] = document_payload
        return result

    def get_true_document_info(self, document_id: str) -> dict[str, Any]:
        """Get document content/status using the supported v4 method."""
        document_id = str(document_id or "").strip()
        if not document_id:
            raise SuzApiError("Не указан UUID документа True API")
        token = self.get_true_api_token()
        response = self._request(
            "GET",
            self._true_api_url(f"doc/{document_id}/info", version=4),
            stage="True API: проверка документа",
            retry_connection=True,
            headers={"Accept": "application/json", "Authorization": f"Bearer {token}"},
        )
        try:
            data = response.json()
        except ValueError as exc:
            raise SuzApiError("True API /doc/{id}/info вернул не JSON") from exc
        return dict(data) if isinstance(data, dict) else {"result": data}

    def ping(self, *, force_token: bool = False) -> dict[str, Any]:
        settings = self.settings()
        oms_id = str(settings.get("suz_oms_id") or "").strip()
        if not oms_id:
            raise SuzApiError("Настройки: не заполнен OMS ID.")
        token = self.get_client_token(force=force_token)
        query = urlencode({"omsId": oms_id})
        response = self._request(
            "GET",
            self._oms_url(f"api/v3/ping?{query}"),
            stage="4/4 Проверка OMS",
            retry_connection=True,
            headers={"Accept": "application/json", "clientToken": token},
        )
        try:
            return dict(response.json())
        except ValueError as exc:
            raise SuzApiError("4/4 Проверка OMS: сервер вернул не JSON") from exc

    def create_order(
        self,
        products: Iterable[dict[str, Any]],
        *,
        attributes: dict[str, Any] | None = None,
        product_group: str,
    ) -> dict[str, Any]:
        settings = self.settings()
        oms_id = str(settings.get("suz_oms_id") or "").strip()
        if not oms_id:
            raise SuzApiError("Не заполнен OMS ID.")
        token = self.get_client_token()
        product_group = str(product_group or "").strip()
        if not product_group:
            raise SuzApiError("Не указана товарная группа СУЗ для заказа.")
        payload = {
            "productGroup": product_group,
            "products": [dict(item) for item in products],
            "attributes": dict(attributes or {}),
        }
        body = self._json_bytes(payload)
        stage = "Создание заказа кодов СУЗ"
        # Log only non-secret request metadata. The clientToken, CMS signature and
        # certificate details are intentionally never written to disk.
        self._diagnostic_log(
            stage,
            "request=" + json.dumps(
                {
                    "productGroup": payload.get("productGroup"),
                    "products": payload.get("products"),
                    "attributes": payload.get("attributes"),
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        )
        signature = self._sign_bytes(body, detached=True)
        self._diagnostic_log(stage, f"detached signature generated, base64 chars={len(signature)}")
        query = urlencode({"omsId": oms_id})
        try:
            response = self._request(
                "POST",
                self._oms_url(f"api/v3/order?{query}"),
                stage=stage,
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/json;charset=UTF-8",
                    "clientToken": token,
                    "X-Signature": signature,
                },
                data=body,
            )
        except SuzApiError as exc:
            # HTTP 401 is an explicit rejection before order creation, therefore a
            # token refresh + one replay is safe. Transport/5xx failures are NOT
            # replayed because POST /order is non-idempotent and could duplicate KMs.
            if exc.status_code != 401:
                raise
            token = self.get_client_token(force=True)
            response = self._request(
                "POST",
                self._oms_url(f"api/v3/order?{query}"),
                stage=stage + " после обновления токена",
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/json;charset=UTF-8",
                    "clientToken": token,
                    "X-Signature": signature,
                },
                data=body,
            )
        try:
            response_payload: Any = response.json()
        except ValueError as exc:
            raise SuzApiError(f"{stage}: сервер вернул некорректный JSON") from exc
        order_id = self._find_string(
            response_payload,
            ("orderId", "order_id", "omsOrderId", "id"),
        )
        if not order_id:
            raise SuzApiError(f"{stage}: ответ не содержит orderId: {response_payload}")
        result = (
            dict(response_payload)
            if isinstance(response_payload, dict)
            else {"response": response_payload}
        )
        result["orderId"] = order_id
        result["requestPayload"] = payload
        return result

    def send_utilisation_report(
        self,
        raw_codes: Iterable[str],
        *,
        product_group: str,
        attributes: dict[str, Any] | None = None,
        utilisation_type: str = "UTILISATION",
    ) -> dict[str, Any]:
        """Submit an OMS application/utilisation report using full marking codes.

        Unlike True API /cises/info, OMS /utilisation requires the complete code
        with its verification/crypto tail exactly as received from SUZ.
        """
        settings = self.settings()
        oms_id = str(settings.get("suz_oms_id") or "").strip()
        if not oms_id:
            raise SuzApiError("Не заполнен OMS ID.")
        codes = [str(code or "") for code in raw_codes if str(code or "")]
        if not codes:
            raise SuzApiError("Нет КИЗов для отчета о нанесении")
        token = self.get_client_token()
        payload: dict[str, Any] = {
            "productGroup": str(product_group or "").strip(),
            "sntins": codes,
            "attributes": dict(attributes or {}),
        }
        if utilisation_type:
            payload["utilisationType"] = str(utilisation_type)
        body = self._json_bytes(payload)
        signature = self._sign_bytes(body, detached=True)
        query = urlencode({"omsId": oms_id})
        response = self._request(
            "POST",
            self._oms_url(f"api/v3/utilisation?{query}"),
            stage="СУЗ: отчет о нанесении",
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json;charset=UTF-8",
                "clientToken": token,
                "X-Signature": signature,
            },
            data=body,
        )
        try:
            payload_response: Any = response.json()
        except ValueError as exc:
            raise SuzApiError("СУЗ: отчет о нанесении вернул некорректный JSON") from exc
        result = (
            dict(payload_response)
            if isinstance(payload_response, dict)
            else {"response": payload_response}
        )
        report_id = self._find_string(payload_response, ("reportId", "report_id", "id"))
        if not report_id:
            raise SuzApiError(f"СУЗ не вернул reportId отчета о нанесении: {result}")
        result["reportId"] = report_id
        result["requestPayload"] = payload
        return result

    def get_report_info(self, report_id: str) -> dict[str, Any]:
        settings = self.settings()
        oms_id = str(settings.get("suz_oms_id") or "").strip()
        if not oms_id:
            raise SuzApiError("Не заполнен OMS ID.")
        report_id = str(report_id or "").strip()
        if not report_id:
            raise SuzApiError("Не указан reportId")
        token = self.get_client_token()
        query = urlencode({"omsId": oms_id, "reportId": report_id})
        response = self._request(
            "GET",
            self._oms_url(f"api/v3/report/info?{query}"),
            stage="СУЗ: статус отчета о нанесении",
            retry_connection=True,
            headers={"Accept": "application/json", "clientToken": token},
        )
        try:
            data = response.json()
        except ValueError as exc:
            raise SuzApiError("СУЗ /report/info вернул не JSON") from exc
        return dict(data) if isinstance(data, dict) else {"result": data}

    def order_status(self, order_id: str, gtin: str | None = None) -> list[dict[str, Any]]:
        settings = self.settings()
        oms_id = str(settings.get("suz_oms_id") or "").strip()
        token = self.get_client_token()
        query_data = {"omsId": oms_id, "orderId": order_id}
        if gtin:
            query_data["gtin"] = gtin
        query = urlencode(query_data)
        response = self._request(
            "GET",
            self._oms_url(f"api/v3/order/status?{query}"),
            retry_connection=True,
            headers={"Accept": "application/json", "clientToken": token},
        )
        try:
            data = response.json()
        except ValueError as exc:
            raise SuzApiError("СУЗ /order/status вернул не JSON") from exc
        if isinstance(data, dict):
            data = [data]
        if not isinstance(data, list):
            raise SuzApiError("СУЗ /order/status вернул неожиданный формат ответа")
        return [dict(item) for item in data if isinstance(item, dict)]

    def get_codes(self, *, order_id: str, gtin: str, quantity: int) -> dict[str, Any]:
        settings = self.settings()
        oms_id = str(settings.get("suz_oms_id") or "").strip()
        token = self.get_client_token()
        query = urlencode({"omsId": oms_id, "orderId": order_id, "quantity": int(quantity), "gtin": gtin})
        response = self._request(
            "GET",
            self._oms_url(f"api/v3/codes?{query}"),
            stage="Получение кодов КМ",
            retry_connection=True,
            headers={"Accept": "application/json", "clientToken": token},
        )
        try:
            data = response.json()
        except ValueError as exc:
            raise SuzApiError("СУЗ /codes вернул не JSON") from exc
        if not isinstance(data, dict):
            raise SuzApiError("СУЗ /codes вернул неожиданный формат ответа")
        return dict(data)

    def get_code_blocks(self, *, order_id: str, gtin: str) -> list[dict[str, Any]]:
        """Return identifiers of code blocks previously issued through OMS API."""
        settings = self.settings()
        oms_id = str(settings.get("suz_oms_id") or "").strip()
        token = self.get_client_token()
        query = urlencode({"omsId": oms_id, "orderId": order_id, "gtin": gtin})
        response = self._request(
            "GET",
            self._oms_url(f"api/v3/order/codes/blocks?{query}"),
            stage="Получение списка блоков КМ",
            retry_connection=True,
            headers={"Accept": "application/json", "clientToken": token},
        )
        try:
            data = response.json()
        except ValueError as exc:
            raise SuzApiError("СУЗ /order/codes/blocks вернул не JSON") from exc
        if not isinstance(data, dict):
            raise SuzApiError("СУЗ /order/codes/blocks вернул неожиданный формат ответа")
        blocks = data.get("blocks")
        return [dict(item) for item in (blocks or []) if isinstance(item, dict)]

    def retry_codes(self, *, block_id: str) -> dict[str, Any]:
        """Re-obtain a previously issued OMS API code block."""
        settings = self.settings()
        oms_id = str(settings.get("suz_oms_id") or "").strip()
        token = self.get_client_token()
        query = urlencode({"omsId": oms_id, "blockId": block_id})
        response = self._request(
            "GET",
            self._oms_url(f"api/v3/order/codes/retry?{query}"),
            stage="Повторное получение блока КМ",
            retry_connection=True,
            headers={"Accept": "application/json", "clientToken": token},
        )
        try:
            data = response.json()
        except ValueError as exc:
            raise SuzApiError("СУЗ /order/codes/retry вернул не JSON") from exc
        if not isinstance(data, dict):
            raise SuzApiError("СУЗ /order/codes/retry вернул неожиданный формат ответа")
        return dict(data)

    def close_order(self, order_id: str) -> dict[str, Any]:
        settings = self.settings()
        oms_id = str(settings.get("suz_oms_id") or "").strip()
        token = self.get_client_token()
        body = self._json_bytes({"orderId": order_id})
        signature = self._sign_bytes(body, detached=True)
        query = urlencode({"omsId": oms_id})
        response = self._request(
            "POST",
            self._oms_url(f"api/v3/order/close?{query}"),
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "clientToken": token,
                "X-Signature": signature,
            },
            data=body,
        )
        try:
            return dict(response.json())
        except Exception:
            return {"success": True}
