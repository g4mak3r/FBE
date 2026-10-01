"""Credentials are never stored in connection config, queue payloads or HTTP responses."""

import ctypes
import json
import os
from pathlib import Path
from typing import Protocol
from uuid import UUID, uuid4

from fbe_flow.core.errors import InvalidInput


class CredentialVault(Protocol):
    def put(self, seller_id: str, values: dict[str, str]) -> str: ...
    def get(self, seller_id: str, reference: str) -> dict[str, str]: ...
    def delete(self, seller_id: str, reference: str) -> None: ...


def credential_owner(reference: str) -> str:
    try:
        owner, key = reference.split(":")
        return f"{UUID(owner)}" if str(UUID(key)) == key else ""
    except (ValueError, AttributeError) as exc:
        raise InvalidInput("Некорректная ссылка на ключи") from exc


class WindowsVault:
    """DPAPI CurrentUser: only the same Windows login can decrypt these files.

    This isolates local sellers in the application, not administrators sharing the OS login.
    No plaintext fallback is provided on unsupported platforms.
    """

    def __init__(self, directory: Path):
        self.directory = directory

    def _path(self, seller_id: str, reference: str) -> Path:
        if credential_owner(reference) != seller_id:
            raise InvalidInput("Ключи принадлежат другому продавцу")
        return self.directory / seller_id / (reference.split(":")[1] + ".dpapi")

    @staticmethod
    def _crypt(data: bytes, decrypt: bool = False) -> bytes:
        if os.name != "nt":
            raise InvalidInput("Хранение ключей ЧЗ доступно на Windows (DPAPI)")
        from ctypes import wintypes

        class Blob(ctypes.Structure):
            _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]

        buffer = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
        source = Blob(len(data), buffer)
        target = Blob()
        crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        function = crypt32.CryptUnprotectData if decrypt else crypt32.CryptProtectData
        function.argtypes = [
            ctypes.POINTER(Blob),
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(Blob),
        ]
        function.restype = wintypes.BOOL
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        kernel32.LocalFree.restype = ctypes.c_void_p
        # CRYPTPROTECT_UI_FORBIDDEN; never use CRYPTPROTECT_LOCAL_MACHINE.
        if not function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)):
            raise InvalidInput("Windows не смог обработать защищенные ключи")
        try:
            return ctypes.string_at(target.data, target.size)
        finally:
            kernel32.LocalFree(target.data)

    def put(self, seller_id: str, values: dict[str, str]) -> str:
        reference = f"{UUID(seller_id)}:{uuid4()}"
        encrypted = self._crypt(json.dumps(values).encode())
        path = self._path(seller_id, reference)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(encrypted)
        return reference

    def get(self, seller_id: str, reference: str) -> dict[str, str]:
        path = self._path(seller_id, reference)
        try:
            return json.loads(self._crypt(path.read_bytes(), decrypt=True))
        except (OSError, ValueError) as exc:
            raise InvalidInput("Ключи подключения недоступны; подключите аккаунт заново") from exc

    def delete(self, seller_id: str, reference: str) -> None:
        self._path(seller_id, reference).unlink(missing_ok=True)
