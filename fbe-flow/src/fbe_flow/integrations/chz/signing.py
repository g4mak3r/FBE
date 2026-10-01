import base64
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path

from fbe_flow.core.errors import InvalidInput


class WindowsSigner:
    def __init__(self, directory: Path):
        self.directory = directory
        self.script = Path(__file__).parent / "crypto.ps1"

    def _run(self, args):
        if os.name != "nt":
            raise InvalidInput("Для УКЭП требуется Windows и КриптоПро CSP / CAdESCOM")
        try:
            result = subprocess.run(
                [
                    "powershell.exe",
                    "-NoProfile",
                    "-NonInteractive",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(self.script),
                    *args,
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=90,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise InvalidInput("Не удалось выполнить подпись УКЭП") from exc
        if result.returncode:
            raise InvalidInput("Проверьте КриптоПро, сертификат, носитель и разрешение на подпись")
        return result.stdout.strip()

    def certificates(self):
        return json.loads(self._run(["-Action", "list"]))

    def sign(self, content: bytes, thumbprint: str, *, detached: bool) -> str:
        if not re.fullmatch(r"[A-Fa-f0-9]{40}", thumbprint):
            raise InvalidInput("Выберите сертификат УКЭП")
        self.directory.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="chz-sign-", dir=self.directory) as tmp:
            path = Path(tmp) / "content.bin"
            path.write_bytes(content)
            args = ["-Action", "sign", "-Thumbprint", thumbprint, "-InputFile", str(path)]
            if detached:
                args.append("-Detached")
            signature = "".join(self._run(args).split())
        try:
            if not base64.b64decode(signature, validate=True):
                raise ValueError()
        except ValueError as exc:
            raise InvalidInput("КриптоПро вернул некорректную подпись") from exc
        return signature
