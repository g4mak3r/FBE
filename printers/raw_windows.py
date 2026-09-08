from __future__ import annotations


def raw_print_windows(printer_name: str, payload: bytes, job_name: str = "FBE Raw Print") -> None:
    from runtime_safety import demo_locked
    if demo_locked():
        return
    try:
        import win32print
    except ImportError as exc:
        raise RuntimeError("pywin32 is required for raw printing on Windows") from exc

    handle = win32print.OpenPrinter(printer_name)
    try:
        win32print.StartDocPrinter(handle, 1, (job_name, None, "RAW"))
        try:
            win32print.StartPagePrinter(handle)
            win32print.WritePrinter(handle, payload)
            win32print.EndPagePrinter(handle)
        finally:
            win32print.EndDocPrinter(handle)
    finally:
        win32print.ClosePrinter(handle)
