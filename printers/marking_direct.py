from __future__ import annotations

from runtime_safety import demo_locked

from pathlib import Path
from typing import Iterable

from PIL import Image, ImageDraw, ImageFont

from printers.coordination import serialized_print_job


def _font_path(*, bold: bool = False) -> Path:
    candidates = (
        [
            Path("C:/Windows/Fonts/arialbd.ttf"),
            Path("C:/Windows/Fonts/calibrib.ttf"),
            Path("C:/Windows/Fonts/segoeuib.ttf"),
            Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
        ]
        if bold
        else [
            Path("C:/Windows/Fonts/arial.ttf"),
            Path("C:/Windows/Fonts/calibri.ttf"),
            Path("C:/Windows/Fonts/segoeui.ttf"),
            Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
        ]
    )
    found = next((path for path in candidates if path.exists()), None)
    if found is None:
        raise RuntimeError("Не найден системный шрифт Arial/Calibri/Segoe UI для печати этикетки")
    return found


def _text_width(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont) -> int:
    box = draw.textbbox((0, 0), text, font=font)
    return max(0, box[2] - box[0])


def _fit_font(draw: ImageDraw.ImageDraw, text: str, max_width: int, max_size: int, min_size: int, *, bold: bool) -> ImageFont.FreeTypeFont:
    path = _font_path(bold=bold)
    clean = " ".join(str(text or "").split())
    for size in range(max_size, min_size - 1, -1):
        font = ImageFont.truetype(str(path), size=size)
        if _text_width(draw, clean, font) <= max_width:
            return font
    return ImageFont.truetype(str(path), size=min_size)


def _wrap(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, max_width: int, max_lines: int) -> list[str]:
    words = " ".join(str(text or "").split()).split()
    if not words:
        return [""]
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = word if not current else f"{current} {word}"
        if _text_width(draw, candidate, font) <= max_width:
            current = candidate
            continue
        if current:
            lines.append(current)
            current = word
        else:
            chunk = ""
            for char in word:
                test = chunk + char
                if chunk and _text_width(draw, test, font) > max_width:
                    lines.append(chunk)
                    chunk = char
                else:
                    chunk = test
            current = chunk
        if len(lines) >= max_lines:
            break
    if len(lines) < max_lines and current:
        lines.append(current)
    lines = lines[:max_lines]
    return lines or [""]


def render_title_png(
    *,
    seller_article: str,
    display_name: str,
    assembly_task: str = "",
    output_path: str | Path,
    width_mm: float = 30.0,
    height_mm: float = 20.0,
    dpi: int = 300,
) -> Path:
    width = max(1, round(width_mm / 25.4 * dpi))
    height = max(1, round(height_mm / 25.4 * dpi))
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    margin = max(8, round(1.0 / 25.4 * dpi))
    usable_width = width - margin * 2

    article = " ".join(str(seller_article or "").split())
    name = " ".join(str(display_name or article).split())
    task_id = " ".join(str(assembly_task or "").split())
    task_digits = "".join(ch for ch in task_id if ch.isdigit())
    task_text = (task_digits[-4:] if task_digits else task_id[-4:]) or "—"

    # Only the last four digits of the WB assembly task are needed for pairing
    # the title label with the following KIZ. Keep them large and centered.
    task_font = _fit_font(draw, task_text, usable_width, 38, 24, bold=True)
    task_y = height - max(8, round(0.7 / 25.4 * dpi))
    draw.text((width / 2, task_y), task_text, font=task_font, fill="black", anchor="ms")

    article_font = _fit_font(draw, article, usable_width, 21, 13, bold=False)
    task_box = draw.textbbox((0, 0), task_text, font=task_font)
    task_h = task_box[3] - task_box[1]
    article_y = task_y - task_h - max(2, round(0.25 / 25.4 * dpi))
    draw.text((width / 2, article_y), article, font=article_font, fill="black", anchor="ms")

    separator_y = height - max(62, round(5.3 / 25.4 * dpi))
    draw.line((margin, separator_y, width - margin, separator_y), fill="black", width=max(1, dpi // 300))

    top = max(6, round(0.6 / 25.4 * dpi))
    bottom = separator_y - max(5, round(0.6 / 25.4 * dpi))
    available_h = max(1, bottom - top)
    chosen_font: ImageFont.FreeTypeFont | None = None
    chosen_lines: list[str] = []
    line_gap = 0
    font_path = _font_path(bold=True)
    for size in range(38, 17, -1):
        font = ImageFont.truetype(str(font_path), size=size)
        line_gap_candidate = max(1, round(size * 0.08))
        line_h = draw.textbbox((0, 0), "АБ", font=font)[3]
        max_lines = max(1, min(5, available_h // max(1, line_h + line_gap_candidate)))
        lines = _wrap(draw, name, font, usable_width, max_lines)
        total_h = len(lines) * line_h + max(0, len(lines) - 1) * line_gap_candidate
        if total_h <= available_h and " ".join(lines).replace(" ", "") == name.replace(" ", ""):
            chosen_font = font
            chosen_lines = lines
            line_gap = line_gap_candidate
            break
    if chosen_font is None:
        chosen_font = ImageFont.truetype(str(font_path), size=18)
        chosen_lines = _wrap(draw, name, chosen_font, usable_width, 5)
        line_gap = 1

    line_h = draw.textbbox((0, 0), "АБ", font=chosen_font)[3]
    total_h = len(chosen_lines) * line_h + max(0, len(chosen_lines) - 1) * line_gap
    y = top + max(0, (available_h - total_h) // 2)
    for line in chosen_lines:
        draw.text((width / 2, y), line, font=chosen_font, fill="black", anchor="ma")
        y += line_h + line_gap

    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    image.save(out, format="PNG", dpi=(dpi, dpi), optimize=True)
    return out


def render_datamatrix_label_png(
    *,
    datamatrix_png: str | Path,
    gtin: str,
    serial: str,
    output_path: str | Path,
    width_mm: float = 30.0,
    height_mm: float = 20.0,
    dpi: int = 300,
) -> Path:
    width = max(1, round(width_mm / 25.4 * dpi))
    height = max(1, round(height_mm / 25.4 * dpi))
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)

    with Image.open(datamatrix_png) as source:
        matrix = source.convert("RGB")
    text_area = max(28, round(2.5 / 25.4 * dpi))
    max_matrix = min(width - 18, height - text_area - 8)
    scale = min(max_matrix / matrix.width, max_matrix / matrix.height, 1.0)
    if scale < 1.0:
        matrix = matrix.resize((max(1, round(matrix.width * scale)), max(1, round(matrix.height * scale))), Image.Resampling.NEAREST)
    x = (width - matrix.width) // 2
    y = max(2, (height - text_area - matrix.height) // 2)
    image.paste(matrix, (x, y))

    human = f"01 {''.join(ch for ch in str(gtin) if ch.isdigit())} 21 {serial}".strip()
    font = _fit_font(draw, human, width - 12, 18, 11, bold=False)
    draw.text((width / 2, height - 5), human, font=font, fill="black", anchor="ms")

    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    image.save(out, format="PNG", dpi=(dpi, dpi), optimize=True)
    return out


def _fit_inside(source_width: int, source_height: int, box_width: int, box_height: int) -> tuple[int, int]:
    """Return a positive size preserving aspect ratio inside the target box."""
    if source_width <= 0 or source_height <= 0 or box_width <= 0 or box_height <= 0:
        return 1, 1
    scale = min(box_width / source_width, box_height / source_height)
    return max(1, round(source_width * scale)), max(1, round(source_height * scale))


def _logical_labels_per_driver_page(actual_height_mm: float, label_height_mm: float) -> int:
    """Detect a driver form containing several physical 20 mm labels.

    Some Xprinter drivers keep a 30x40 mm form even when FBE sends a 30x20 mm
    bitmap. Printing one bitmap per GDI page then advances over one printed and
    one empty label. When the driver ignores the job-specific 30x20 DEVMODE, we
    place two logical labels into that 40 mm page instead.
    """
    if actual_height_mm <= 0 or label_height_mm <= 0:
        return 1
    ratio = actual_height_mm / label_height_mm
    nearest = max(1, round(ratio))
    if 2 <= nearest <= 4 and abs(ratio - nearest) <= 0.30:
        return nearest
    return 1


@serialized_print_job
def print_images_windows(
    *,
    printer_name: str,
    image_paths: Iterable[str | Path],
    width_mm: float,
    height_mm: float,
    job_name: str,
    dry_run: bool = False,
    safe_margin_mm: float = 0.4,
    debug_path: str | Path = "data/print/last_marking_direct_print.txt",
) -> str:
    paths = [Path(path).resolve() for path in image_paths]
    if not paths:
        raise ValueError("Нет этикеток для печати")
    for path in paths:
        if not path.exists():
            raise FileNotFoundError(str(path))

    debug = Path(debug_path)
    debug.parent.mkdir(parents=True, exist_ok=True)
    base_debug = [
        f"printer={printer_name}",
        f"job={job_name}",
        f"requested_size_mm={width_mm}x{height_mm}",
        f"safe_margin_mm={safe_margin_mm}",
        f"logical_labels={len(paths)}",
        *(str(path) for path in paths),
    ]
    if dry_run or demo_locked():
        debug.write_text("\n".join(base_debug + ["mode=dry-run"]), encoding="utf-8")
        return "dry-run"
    if not printer_name.strip():
        raise RuntimeError("В config.json не указан small_label_printer_name")

    try:
        import win32con
        import win32gui
        import win32print
        import win32ui
        from PIL import ImageWin
    except ImportError as exc:
        raise RuntimeError("Для моментальной печати на Windows нужны pillow и pywin32") from exc

    printer_handle = None
    hdc = None
    custom_media_requested = False
    custom_media_validated = False
    try:
        printer_handle = win32print.OpenPrinter(printer_name)
        printer_info = win32print.GetPrinter(printer_handle, 2)
        devmode = printer_info.get("pDevMode") if isinstance(printer_info, dict) else None
        if devmode is not None:
            try:
                devmode.PaperSize = 0
                devmode.PaperWidth = int(round(width_mm * 10.0))
                devmode.PaperLength = int(round(height_mm * 10.0))
                devmode.Orientation = getattr(win32con, "DMORIENT_PORTRAIT", 1)
                devmode.Scale = 100
                devmode.Copies = 1
                fields = int(getattr(devmode, "Fields", 0) or 0)
                for constant_name in (
                    "DM_PAPERSIZE",
                    "DM_PAPERWIDTH",
                    "DM_PAPERLENGTH",
                    "DM_ORIENTATION",
                    "DM_SCALE",
                    "DM_COPIES",
                ):
                    fields |= int(getattr(win32con, constant_name, 0) or 0)
                try:
                    devmode.Nup = getattr(win32con, "DMNUP_ONEUP", 2)
                    fields |= int(getattr(win32con, "DM_NUP", 0) or 0)
                except Exception:
                    pass
                devmode.Fields = fields
                custom_media_requested = True
                result = win32print.DocumentProperties(
                    0,
                    printer_handle,
                    printer_name,
                    devmode,
                    devmode,
                    int(getattr(win32con, "DM_IN_BUFFER", 8))
                    | int(getattr(win32con, "DM_OUT_BUFFER", 2)),
                )
                custom_media_validated = int(result) >= 0
            except Exception:
                custom_media_validated = False

        if devmode is not None:
            try:
                raw_hdc = win32gui.CreateDC("WINSPOOL", printer_name, devmode)
                hdc = win32ui.CreateDCFromHandle(raw_hdc)
            except Exception:
                hdc = None
        if hdc is None:
            hdc = win32ui.CreateDC()
            hdc.CreatePrinterDC(printer_name)
            custom_media_validated = False

        dpi_x = max(1, hdc.GetDeviceCaps(win32con.LOGPIXELSX))
        dpi_y = max(1, hdc.GetDeviceCaps(win32con.LOGPIXELSY))
        printable_w = max(1, hdc.GetDeviceCaps(win32con.HORZRES))
        printable_h = max(1, hdc.GetDeviceCaps(win32con.VERTRES))
        physical_w_px = max(1, hdc.GetDeviceCaps(getattr(win32con, "PHYSICALWIDTH", 110)))
        physical_h_px = max(1, hdc.GetDeviceCaps(getattr(win32con, "PHYSICALHEIGHT", 111)))
        offset_x = max(0, hdc.GetDeviceCaps(getattr(win32con, "PHYSICALOFFSETX", 112)))
        offset_y = max(0, hdc.GetDeviceCaps(getattr(win32con, "PHYSICALOFFSETY", 113)))
        physical_w_mm = physical_w_px / dpi_x * 25.4
        physical_h_mm = physical_h_px / dpi_y * 25.4

        if physical_w_mm > width_mm * 2.6 or physical_h_mm > height_mm * 4.6:
            raise RuntimeError(
                "Драйвер принтера не принял формат 30×20 мм и вернул слишком большой лист "
                f"{physical_w_mm:.1f}×{physical_h_mm:.1f} мм. "
                "Создайте в свойствах Xprinter пользовательский формат 30×20 мм."
            )

        labels_per_page = _logical_labels_per_driver_page(physical_h_mm, height_mm)
        requested_w_px = max(1, round(width_mm / 25.4 * dpi_x))
        requested_h_px = max(1, round(height_mm / 25.4 * dpi_y))
        margin_x = max(0, round(max(0.0, safe_margin_mm) / 25.4 * dpi_x))
        margin_y = max(0, round(max(0.0, safe_margin_mm) / 25.4 * dpi_y))

        debug_lines = base_debug + [
            f"custom_media_requested={int(custom_media_requested)}",
            f"custom_media_validated={int(custom_media_validated)}",
            f"device_dpi={dpi_x}x{dpi_y}",
            f"physical_px={physical_w_px}x{physical_h_px}",
            f"physical_mm={physical_w_mm:.3f}x{physical_h_mm:.3f}",
            f"printable_px={printable_w}x{printable_h}",
            f"physical_offset_px={offset_x},{offset_y}",
            f"labels_per_driver_page={labels_per_page}",
        ]

        hdc.StartDoc(job_name)
        try:
            for page_start in range(0, len(paths), labels_per_page):
                group = paths[page_start : page_start + labels_per_page]
                hdc.StartPage()
                try:
                    for slot_index, path in enumerate(group):
                        slot_top = round(slot_index * printable_h / labels_per_page)
                        slot_bottom = round((slot_index + 1) * printable_h / labels_per_page)
                        slot_height = max(1, slot_bottom - slot_top)

                        logical_w = min(requested_w_px, printable_w)
                        logical_h = min(requested_h_px, slot_height)

                        # GDI coordinates start at the printer's printable origin, not at
                        # the physical media edge. Centering against HORZRES alone shifts
                        # the image whenever left/right hardware margins are asymmetric.
                        # Convert the physical media center to printable-area coordinates.
                        physical_center_x = (physical_w_px / 2.0) - offset_x
                        logical_left = round(physical_center_x - logical_w / 2.0)
                        logical_left = min(max(0, logical_left), max(0, printable_w - logical_w))
                        logical_top = slot_top + max(0, (slot_height - logical_h) // 2)

                        box_left = logical_left + margin_x
                        box_top = logical_top + margin_y
                        box_right = logical_left + logical_w - margin_x
                        box_bottom = logical_top + logical_h - margin_y
                        if box_right <= box_left or box_bottom <= box_top:
                            raise RuntimeError("Печатная область принтера меньше безопасной области этикетки")

                        with Image.open(path) as opened:
                            source = opened.convert("RGB")
                        draw_w, draw_h = _fit_inside(
                            source.width,
                            source.height,
                            box_right - box_left,
                            box_bottom - box_top,
                        )
                        draw_left = box_left + ((box_right - box_left) - draw_w) // 2
                        draw_top = box_top + ((box_bottom - box_top) - draw_h) // 2
                        dib = ImageWin.Dib(source)
                        dib.draw(
                            hdc.GetHandleOutput(),
                            (draw_left, draw_top, draw_left + draw_w, draw_top + draw_h),
                        )
                        debug_lines.append(
                            f"label={page_start + slot_index + 1};slot={slot_index + 1}/{labels_per_page};"
                            f"rect={draw_left},{draw_top},{draw_left + draw_w},{draw_top + draw_h};file={path.name}"
                        )
                finally:
                    hdc.EndPage()
        finally:
            hdc.EndDoc()

        mode = "windows-driver-custom" if labels_per_page == 1 else f"windows-driver-packed-{labels_per_page}"
        debug.write_text("\n".join(debug_lines + [f"mode={mode}"]), encoding="utf-8")
        return mode
    finally:
        if hdc is not None:
            try:
                hdc.DeleteDC()
            except Exception:
                pass
        if printer_handle is not None:
            try:
                win32print.ClosePrinter(printer_handle)
            except Exception:
                pass
