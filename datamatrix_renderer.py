from __future__ import annotations

import os
import platform
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any


class DataMatrixRenderError(RuntimeError):
    pass


class OfficialDataMatrixRenderer:
    """Render official marking-code strings with the CRPT developer library.

    The original code string is written to a temporary UTF-8 file so ASCII 29
    group separators reach Java unchanged. The value is never passed through a
    command-line argument or a spreadsheet cell.
    """

    def __init__(self, *, base_dir: str | Path, settings_loader):
        self.base_dir = Path(base_dir).resolve()
        self.settings_loader = settings_loader

    def settings(self) -> dict[str, Any]:
        return dict(self.settings_loader() or {})

    def _java_executable(self) -> str:
        default_java = "java.exe" if platform.system().lower() == "windows" else "java"
        configured = str(self.settings().get("java_exe") or default_java).strip()
        if not configured:
            configured = "java.exe" if platform.system().lower() == "windows" else "java"
        if Path(configured).is_file() or shutil.which(configured):
            return configured
        raise DataMatrixRenderError(
            "Java не найдена. Установите Java 8+ или укажите java_exe в config.json."
        )

    def _library_paths(self) -> tuple[Path, Path]:
        lib_dir = self.base_dir / "tools" / "datamatrix"
        helper = lib_dir / "fbe-datamatrix-helper.jar"
        official = lib_dir / "datamatrix-1.5.jar"
        missing = [str(path) for path in (helper, official) if not path.exists()]
        if missing:
            raise DataMatrixRenderError(
                "Не найдены библиотеки DataMatrix: " + ", ".join(missing)
            )
        return helper, official

    def readiness(self) -> dict[str, Any]:
        result = {
            "ready": False,
            "java": "",
            "official_library": False,
            "helper_library": False,
            "error": "",
        }
        try:
            helper, official = self._library_paths()
            result["official_library"] = official.exists()
            result["helper_library"] = helper.exists()
            java = self._java_executable()
            completed = subprocess.run(
                [java, "-version"],
                cwd=str(self.base_dir),
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
            if completed.returncode != 0:
                raise DataMatrixRenderError(
                    (completed.stderr or completed.stdout or "Java завершилась с ошибкой").strip()
                )
            result["java"] = java
            result["ready"] = True
        except Exception as exc:
            result["error"] = str(exc)
        return result

    def render_png(
        self,
        raw_code: str,
        output_path: str | Path,
        *,
        dpi: int = 300,
    ) -> Path:
        if not isinstance(raw_code, str) or not raw_code:
            raise DataMatrixRenderError("Пустой код маркировки")
        if dpi < 150 or dpi > 1200:
            raise DataMatrixRenderError("DPI DataMatrix должен быть от 150 до 1200")

        helper, official = self._library_paths()
        java = self._java_executable()
        output = Path(output_path)
        output.parent.mkdir(parents=True, exist_ok=True)

        runtime_dir = Path(self.settings().get("workspace_root") or self.base_dir / "data") / "runtime" / "datamatrix"
        runtime_dir.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix="cis_", suffix=".txt", dir=str(runtime_dir))
        os.close(fd)
        input_path = Path(temp_name)
        try:
            # All current KMs are ASCII plus control separators, but UTF-8 keeps
            # those bytes byte-for-byte while remaining future-proof.
            input_path.write_bytes(raw_code.encode("utf-8"))
            classpath = os.pathsep.join((str(helper), str(official)))
            completed = subprocess.run(
                [
                    java,
                    "-Djava.awt.headless=true",
                    "-cp",
                    classpath,
                    "fbe.datamatrix.RenderOne",
                    str(input_path),
                    str(output),
                    str(int(dpi)),
                ],
                cwd=str(self.base_dir),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=90,
                check=False,
            )
            if completed.returncode != 0:
                message = (completed.stderr or completed.stdout or "Неизвестная ошибка Java").strip()
                raise DataMatrixRenderError(f"Не удалось создать DataMatrix: {message}")
            if not output.exists() or output.stat().st_size < 100:
                raise DataMatrixRenderError("Java не создала корректный PNG DataMatrix")
            return output
        finally:
            try:
                input_path.unlink(missing_ok=True)
            except Exception:
                pass
