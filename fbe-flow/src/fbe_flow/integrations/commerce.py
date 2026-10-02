"""Protected outbound API boundary shared by commerce adapters."""

import time
from threading import Lock

from fbe_flow.core.credentials import credential_owner
from fbe_flow.core.errors import InvalidInput
from fbe_flow.core.models import AccountInfo, Contract, OperationSpec
from fbe_flow.integrations.chz.http import HttpTransport, RemoteError, redact


class CommerceConfig(Contract):
    credential_ref: str
    account_id: str
    tin: str
    read_only: bool = False


def objects(value, key):
    values = value.get(key) if isinstance(value, dict) else None
    if not isinstance(values, list) or any(not isinstance(v, dict) for v in values):
        raise RemoteError(None, "commerce_response_invalid")
    return values


def positive(value):
    if isinstance(value, str) and value.isascii() and value.isdigit():
        value = int(value)
    if type(value) is not int or value <= 0:
        raise RemoteError(None, "commerce_identifier_invalid")
    return value


class ProtectedAdapter:
    def __init__(self, vault, transport=None, *, clock=time.monotonic, pause=time.sleep):
        self.vault, self.http = vault, transport or HttpTransport()
        self.clock, self.pause = clock, pause
        self._lock, self._identity_lock = Lock(), Lock()
        self._next, self._identities = {}, {}

    def secret(self, config):
        value = self.vault.get(credential_owner(config.credential_ref), config.credential_ref)
        token = value.get(self.key + "_token")
        if not isinstance(token, str) or not token.strip():
            raise InvalidInput("Ключ подключения недоступен")
        return token

    def validate_binding(self, seller, config):
        parsed = CommerceConfig.model_validate(config)
        if credential_owner(parsed.credential_ref) != seller:
            raise InvalidInput("Ключ принадлежит другому продавцу")
        self.secret(parsed)

    def config(self, context):
        self.validate_binding(context.seller_id, context.config)
        parsed = CommerceConfig.model_validate(context.config)
        if context.external_account_id != parsed.account_id:
            raise InvalidInput("Неверный аккаунт интеграции")
        return parsed

    @staticmethod
    def supported_operations(read_only=False):
        return [
            OperationSpec(key="commerce.sync", label="Обновить данные").model_dump(),
            *(
                []
                if read_only
                else [
                    OperationSpec(key="commerce.command", label="Выполнить действие").model_dump()
                ]
            ),
            OperationSpec(key="commerce.reconcile", label="Сверить результат").model_dump(),
        ]

    def _call(self, config, method, path, *, body=None, params=None, write=False):
        if write and config.read_only:
            raise InvalidInput("Подключение разрешает только чтение")
        token = self.secret(config)
        headers = self.headers(config, token)
        account = config.account_id or config.credential_ref
        with self._lock:
            delay = self._next.get(account, 0) - self.clock()
            if delay > 0:
                self.pause(delay)
            self._next[account] = self.clock() + self.interval
        try:
            reply = self.http.request(
                method, self.host + path, headers=headers, params=params, body=body
            )
        except RemoteError as exc:
            if exc.status == 429:
                with self._lock:
                    self._next[account] = max(self._next[account], self.clock() + 10)
            raise RemoteError(exc.status, exc.code, redact(exc.details, [token])) from exc
        if reply.status not in {200, 201, 204}:
            raise RemoteError(reply.status, "commerce_unexpected_status")
        return redact(reply.data, [token])

    def account(self, config):
        with self._identity_lock:
            saved = self._identities.get(config.credential_ref)
            if saved and self.clock() - saved[1] < 60:
                value = saved[0]
            else:
                value = self.identity(config)
                self._identities[config.credential_ref] = (value, self.clock())
        if value["account_id"] != config.account_id or value["tin"] != config.tin:
            raise InvalidInput("Ключ относится к другому аккаунту или ИНН")
        return value

    def describe(self, config):
        parsed = CommerceConfig.model_validate(config)
        self.account(parsed)
        return AccountInfo(
            external_account_id=parsed.account_id,
            operations=tuple(
                OperationSpec.model_validate(v) for v in self.supported_operations(parsed.read_only)
            ),
        )

    def execute(self, context, operation, payload):
        raise InvalidInput("Действия интеграции выполняются через журнал FBE")
