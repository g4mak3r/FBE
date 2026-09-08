from __future__ import annotations

import platform
from typing import Any


# Windows keyboard layout identifiers.
_ENGLISH_US_KLID = "00000409"
_WM_INPUTLANGCHANGEREQUEST = 0x0050
_KLF_ACTIVATE = 0x00000001


def force_english_keyboard_layout() -> dict[str, Any]:
    """Ask the foreground Windows application to switch to the US English layout.

    FBE is a local Windows application opened in a browser. A browser cannot directly
    change the operating-system keyboard layout, therefore the local FastAPI process
    sends WM_INPUTLANGCHANGEREQUEST to the current foreground window. The marking-code
    parser still performs a Russian-to-English keyboard recovery as a safety net.
    """
    if platform.system().lower() != "windows":
        return {
            "ok": False,
            "supported": False,
            "message": "Переключение раскладки доступно только в Windows; автокоррекция ввода остается включена.",
        }

    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        hkl_type = getattr(wintypes, "HKL", wintypes.HANDLE)
        user32.LoadKeyboardLayoutW.argtypes = [wintypes.LPCWSTR, wintypes.UINT]
        user32.LoadKeyboardLayoutW.restype = hkl_type
        user32.GetForegroundWindow.argtypes = []
        user32.GetForegroundWindow.restype = wintypes.HWND
        user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        user32.PostMessageW.restype = wintypes.BOOL

        hkl = user32.LoadKeyboardLayoutW(_ENGLISH_US_KLID, _KLF_ACTIVATE)
        if not hkl:
            error = ctypes.get_last_error()
            raise OSError(error, "LoadKeyboardLayoutW failed")

        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            raise RuntimeError("Не найдено активное окно браузера")

        hkl_value = int(hkl) if isinstance(hkl, int) else int(ctypes.cast(hkl, ctypes.c_void_p).value or 0)
        posted = user32.PostMessageW(
            hwnd,
            _WM_INPUTLANGCHANGEREQUEST,
            0,
            hkl_value,
        )
        if not posted:
            error = ctypes.get_last_error()
            raise OSError(error, "PostMessageW failed")

        return {
            "ok": True,
            "supported": True,
            "message": "Для поля КИЗ включена английская раскладка.",
        }
    except Exception as exc:
        return {
            "ok": False,
            "supported": True,
            "message": f"Windows не подтвердил переключение раскладки: {exc}. Автокоррекция ввода остается включена.",
        }
