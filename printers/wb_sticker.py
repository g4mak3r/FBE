from __future__ import annotations

from runtime_safety import demo_locked

from pathlib import Path

from printers.raw_windows import raw_print_windows
from printers.coordination import serialized_print_job


def _write_wb_debug(sticker_path: str, printer_name: str, sticker_type: str, width_mm: int, height_mm: int) -> None:
    try:
        debug_dir = Path(sticker_path).resolve().parent / "print_debug"
        debug_dir.mkdir(parents=True, exist_ok=True)
        (debug_dir / "last_wb_print.txt").write_text(
            "\n".join(
                [
                    f"printer={printer_name}",
                    f"sticker_path={Path(sticker_path).resolve()}",
                    f"sticker_type={sticker_type}",
                    f"width_mm={width_mm}",
                    f"height_mm={height_mm}",
                ]
            ),
            encoding="utf-8",
        )
    except Exception:
        pass


def _print_image_windows(printer_name: str, image_path: str, width_mm: int, height_mm: int) -> None:
    """Print PNG/SVG-rendered raster sticker through the normal Windows printer driver.

    This is the safest path for Xprinter XP-365B via its Windows driver. It does not
    send ZPL/TSPL raw commands; it prints the exact WB PNG as an image sized to the
    configured label dimensions.
    """
    if demo_locked():
        return
    try:
        import win32con
        import win32ui
        from PIL import Image, ImageWin
    except ImportError as exc:
        raise RuntimeError("PNG printing requires pillow and pywin32 on Windows") from exc

    img = Image.open(image_path).convert("RGB")

    hdc = win32ui.CreateDC()
    hdc.CreatePrinterDC(printer_name)
    try:
        dpi_x = hdc.GetDeviceCaps(win32con.LOGPIXELSX)
        dpi_y = hdc.GetDeviceCaps(win32con.LOGPIXELSY)
        printable_w = hdc.GetDeviceCaps(win32con.HORZRES)
        printable_h = hdc.GetDeviceCaps(win32con.VERTRES)

        target_w = int(width_mm / 25.4 * dpi_x)
        target_h = int(height_mm / 25.4 * dpi_y)

        # Never draw outside the printable area exposed by the driver.
        target_w = min(target_w, printable_w)
        target_h = min(target_h, printable_h)

        dib = ImageWin.Dib(img)
        hdc.StartDoc(f"FBE WB {Path(image_path).stem}")
        try:
            hdc.StartPage()
            dib.draw(hdc.GetHandleOutput(), (0, 0, target_w, target_h))
            hdc.EndPage()
        finally:
            hdc.EndDoc()
    finally:
        hdc.DeleteDC()


@serialized_print_job
def print_wb_sticker(
    printer_name: str,
    sticker_path: str,
    sticker_type: str,
    dry_run: bool = True,
    width_mm: int = 58,
    height_mm: int = 40,
) -> None:
    _write_wb_debug(sticker_path, printer_name, sticker_type, width_mm, height_mm)

    if dry_run or demo_locked():
        print(f"[DRY RUN] WB sticker would print: {sticker_path} -> {printer_name}")
        return

    if sticker_type.startswith("zpl"):
        payload = Path(sticker_path).read_bytes()
        raw_print_windows(printer_name, payload, job_name=f"FBE WB {Path(sticker_path).stem}")
        return

    if sticker_type.lower() in {"png", "jpg", "jpeg"}:
        _print_image_windows(printer_name, sticker_path, width_mm=width_mm, height_mm=height_mm)
        return

    raise RuntimeError(
        f"Unsupported WB sticker print type: {sticker_type}. "
        "Use png for Xprinter via Windows driver, or zplv/zplh for true ZPL raw printing."
    )
