from __future__ import annotations

from pathlib import Path
from typing import Iterable

from PIL import Image
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas
from pypdf import PdfReader, PdfWriter


def _draw_svg_page(c: canvas.Canvas, svg_path: Path, page_w: float, page_h: float) -> None:
    """Draw an SVG sticker as vector content into the current PDF page.

    svglib keeps QR/barcode edges much sharper than the old PNG raster path.
    If an SVG is malformed, the caller will raise a clear error instead of silently
    falling back to low-quality raster output.
    """
    from svglib.svglib import svg2rlg
    from reportlab.graphics import renderPDF

    drawing = svg2rlg(str(svg_path))
    if drawing is None:
        raise ValueError(f"Не удалось прочитать SVG-стикер: {svg_path}")

    # Fit exactly into the configured WB label page. WB stickers are already
    # generated for 58x40 / 40x30, so non-uniform scaling is normally tiny/none.
    dw = float(getattr(drawing, "width", 0) or page_w)
    dh = float(getattr(drawing, "height", 0) or page_h)
    sx = page_w / dw if dw else 1.0
    sy = page_h / dh if dh else 1.0
    drawing.scale(sx, sy)
    renderPDF.draw(drawing, c, 0, 0)


def _draw_image_page(c: canvas.Canvas, image_path: Path, page_w: float, page_h: float) -> None:
    with Image.open(image_path) as img:
        img = img.convert("RGB")
        c.drawInlineImage(img, 0, 0, width=page_w, height=page_h)


def create_stickers_pdf(
    sticker_paths: Iterable[str | Path],
    output_path: str | Path,
    width_mm: int = 58,
    height_mm: int = 40,
) -> Path:
    """Create a one-sticker-per-page PDF for visual control/printing.

    v26: SVG stickers are drawn as vector PDF content. This is faster to download
    from WB than per-order image conversion workflows and preserves QR/barcode
    quality much better than embedding PNGs.
    """
    paths = [Path(p) for p in sticker_paths]
    if not paths:
        raise ValueError("No stickers for PDF")

    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)

    page_w = width_mm * mm
    page_h = height_mm * mm
    c = canvas.Canvas(str(output), pagesize=(page_w, page_h))

    rendered = 0
    for path in paths:
        if not path.exists():
            continue
        suffix = path.suffix.lower()
        if suffix == ".svg":
            _draw_svg_page(c, path, page_w, page_h)
        else:
            _draw_image_page(c, path, page_w, page_h)
        c.showPage()
        rendered += 1

    if rendered == 0:
        raise ValueError("No existing sticker files for PDF")

    c.save()
    return output


def prepend_pdf_file(
    header_pdf: str | Path,
    body_pdf: str | Path,
    output_path: str | Path,
) -> Path:
    """Prepend all pages from header_pdf before body_pdf without scaling either document."""
    header_reader = PdfReader(str(header_pdf))
    body_reader = PdfReader(str(body_pdf))
    writer = PdfWriter()
    for page in header_reader.pages:
        writer.add_page(page)
    for page in body_reader.pages:
        writer.add_page(page)
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("wb") as handle:
        writer.write(handle)
    return out


def create_supply_info_label_pdf(
    *,
    supply_name: str,
    supply_id: str,
    date_text: str,
    order_count: int,
    output_path: str | Path,
    width_mm: float = 58.0,
    height_mm: float = 40.0,
    title: str = "ПОСТАВКА",
    footer: str = "",
) -> Path:
    """Create a readable separator label for a supply/QR print batch."""
    regular_font, bold_font = _register_small_label_fonts()
    page_w = float(width_mm) * mm
    page_h = float(height_mm) * mm
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    c = canvas.Canvas(str(out), pagesize=(page_w, page_h), pageCompression=1)

    margin = 2.0 * mm
    usable_w = page_w - 2 * margin
    y = page_h - 3.4 * mm

    title_text = " ".join(str(title or "ПОСТАВКА").split()).upper()
    title_size = _fit_single_line(title_text, bold_font, 11.0, 6.0, usable_w)
    c.setFont(bold_font, title_size)
    c.drawCentredString(page_w / 2, y, title_text)
    y -= 4.2 * mm

    name = " ".join(str(supply_name or "Без названия").split())
    name_size = 9.2
    name_lines = _wrap_label_text(name, bold_font, name_size, usable_w, 3)
    while any("…" in line for line in name_lines) and name_size > 6.4:
        name_size -= 0.3
        name_lines = _wrap_label_text(name, bold_font, name_size, usable_w, 3)
    line_h = name_size * 1.15
    c.setFont(bold_font, name_size)
    for line in name_lines:
        c.drawCentredString(page_w / 2, y, line)
        y -= line_h

    y -= 0.8 * mm
    c.setLineWidth(0.5)
    c.line(margin, y, page_w - margin, y)
    y -= 4.0 * mm

    code = " ".join(str(supply_id or "").split())
    code_size = _fit_single_line(code, bold_font, 9.5, 6.2, usable_w)
    c.setFont(bold_font, code_size)
    c.drawCentredString(page_w / 2, y, code)
    y -= 4.2 * mm

    summary = f"{date_text or '—'}   •   ЗАКАЗОВ: {int(order_count)}"
    summary_size = _fit_single_line(summary, regular_font, 8.0, 5.8, usable_w)
    c.setFont(regular_font, summary_size)
    c.drawCentredString(page_w / 2, y, summary)

    footer_text = " ".join(str(footer or "").split())
    if footer_text:
        footer_size = _fit_single_line(footer_text, regular_font, 6.0, 4.4, usable_w)
        c.setFont(regular_font, footer_size)
        c.drawCentredString(page_w / 2, 1.7 * mm, footer_text)

    c.showPage()
    c.save()
    return out


def merge_alternating_pdfs(
    wb_pdf: str | Path,
    bartender_pdf: str | Path,
    output_path: str | Path,
) -> Path:
    """Merge two PDFs page-by-page: WB page 1, BT page 1, WB page 2, BT page 2."""
    wb_reader = PdfReader(str(wb_pdf))
    bt_reader = PdfReader(str(bartender_pdf))
    writer = PdfWriter()

    max_pages = max(len(wb_reader.pages), len(bt_reader.pages))
    for i in range(max_pages):
        if i < len(wb_reader.pages):
            writer.add_page(wb_reader.pages[i])
        if i < len(bt_reader.pages):
            writer.add_page(bt_reader.pages[i])

    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("wb") as f:
        writer.write(f)
    return out


def merge_wb_pdf_with_internal_pdf_files(
    wb_pdf: str | Path,
    internal_pdfs: list[str | Path],
    output_path: str | Path,
) -> Path:
    """Merge WB sticker PDF pages with a list of internal-label PDFs.

    Output order: WB page 1, internal PDF 1 page 1, WB page 2, internal PDF 2 page 1, ...
    """
    wb_reader = PdfReader(str(wb_pdf))
    internal_readers = [PdfReader(str(p)) for p in internal_pdfs]
    writer = PdfWriter()

    max_pages = max(len(wb_reader.pages), len(internal_readers))
    for i in range(max_pages):
        if i < len(wb_reader.pages):
            writer.add_page(wb_reader.pages[i])
        if i < len(internal_readers):
            reader = internal_readers[i]
            if len(reader.pages) > 0:
                writer.add_page(reader.pages[0])

    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("wb") as f:
        writer.write(f)
    return out



def merge_wb_pdf_with_internal_page_refs(
    wb_pdf: str | Path,
    internal_page_refs: list[tuple[str | Path, int]],
    output_path: str | Path,
) -> Path:
    """Merge WB pages with selected pages from one or more internal-label PDFs.

    internal_page_refs is ordered by the WB/picklist order. Each tuple is
    (internal_pdf_path, zero_based_page_index). This lets FBE render labels in
    fast template batches and still restore the exact original WB order.
    """
    wb_reader = PdfReader(str(wb_pdf))
    reader_cache: dict[str, PdfReader] = {}
    writer = PdfWriter()

    if len(wb_reader.pages) != len(internal_page_refs):
        raise ValueError(
            f"WB pages ({len(wb_reader.pages)}) != internal label refs ({len(internal_page_refs)})"
        )

    for i, (pdf_path, page_index) in enumerate(internal_page_refs):
        writer.add_page(wb_reader.pages[i])
        key = str(Path(pdf_path).resolve())
        reader = reader_cache.get(key)
        if reader is None:
            reader = PdfReader(key)
            reader_cache[key] = reader
        if page_index < 0 or page_index >= len(reader.pages):
            raise IndexError(f"Internal PDF page out of range: {key} page {page_index + 1}")
        writer.add_page(reader.pages[page_index])

    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("wb") as f:
        writer.write(f)
    return out


def merge_title_labels_with_official_kiz_pages(
    title_page_refs: list[tuple[str | Path, int]],
    kiz_page_refs: list[tuple[str | Path, int]],
    output_path: str | Path,
) -> Path:
    """Interleave locally generated title pages with untouched official KIZ PDF pages.

    Output order is title 1, official KIZ 1, title 2, official KIZ 2, ...
    The official pages are copied as PDF page objects; FBE does not decode,
    redraw or regenerate the Data Matrix symbol.
    """
    if len(title_page_refs) != len(kiz_page_refs):
        raise ValueError(
            f"Title labels ({len(title_page_refs)}) != official KIZ pages ({len(kiz_page_refs)})"
        )
    if not title_page_refs:
        raise ValueError("No labels for marking print PDF")

    reader_cache: dict[str, PdfReader] = {}
    writer = PdfWriter()

    def get_page(ref: tuple[str | Path, int]):
        pdf_path, page_index = ref
        key = str(Path(pdf_path).resolve())
        reader = reader_cache.get(key)
        if reader is None:
            reader = PdfReader(key)
            reader_cache[key] = reader
        if page_index < 0 or page_index >= len(reader.pages):
            raise IndexError(f"PDF page out of range: {key} page {page_index + 1}")
        return reader.pages[page_index]

    for title_ref, kiz_ref in zip(title_page_refs, kiz_page_refs):
        writer.add_page(get_page(title_ref))
        writer.add_page(get_page(kiz_ref))

    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("wb") as handle:
        writer.write(handle)
    return out


_SMALL_LABEL_FONTS: tuple[str, str] | None = None


def _register_small_label_fonts() -> tuple[str, str]:
    """Register a Cyrillic-capable font available on Windows or Linux.

    Font files are referenced from the host OS and are not bundled with FBE.
    """
    global _SMALL_LABEL_FONTS
    if _SMALL_LABEL_FONTS is not None:
        return _SMALL_LABEL_FONTS

    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    regular_candidates = [
        Path("C:/Windows/Fonts/arial.ttf"),
        Path("C:/Windows/Fonts/calibri.ttf"),
        Path("C:/Windows/Fonts/segoeui.ttf"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    ]
    bold_candidates = [
        Path("C:/Windows/Fonts/arialbd.ttf"),
        Path("C:/Windows/Fonts/calibrib.ttf"),
        Path("C:/Windows/Fonts/segoeuib.ttf"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
    ]

    regular_path = next((path for path in regular_candidates if path.exists()), None)
    bold_path = next((path for path in bold_candidates if path.exists()), None)
    if regular_path is None:
        raise RuntimeError(
            "Не найден шрифт с поддержкой кириллицы. Ожидался Arial/Calibri/Segoe UI в C:/Windows/Fonts."
        )
    if bold_path is None:
        bold_path = regular_path

    regular_name = "FBE_SmallLabel_Regular"
    bold_name = "FBE_SmallLabel_Bold"
    if regular_name not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont(regular_name, str(regular_path)))
    if bold_name not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont(bold_name, str(bold_path)))

    _SMALL_LABEL_FONTS = (regular_name, bold_name)
    return _SMALL_LABEL_FONTS


def _fit_single_line(text: str, font_name: str, max_size: float, min_size: float, max_width: float) -> float:
    from reportlab.pdfbase import pdfmetrics

    size = max_size
    clean = " ".join(str(text or "").split())
    while size > min_size and pdfmetrics.stringWidth(clean, font_name, size) > max_width:
        size -= 0.2
    return max(min_size, size)


def _wrap_label_text(
    text: str,
    font_name: str,
    font_size: float,
    max_width: float,
    max_lines: int,
) -> list[str]:
    from reportlab.pdfbase import pdfmetrics

    clean = " ".join(str(text or "").split())
    if not clean:
        return [""]

    words = clean.split(" ")
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = word if not current else f"{current} {word}"
        if pdfmetrics.stringWidth(candidate, font_name, font_size) <= max_width:
            current = candidate
            continue
        if current:
            lines.append(current)
            current = word
        else:
            # A single long token (often an article-like product name): split by characters.
            chunk = ""
            for char in word:
                next_chunk = chunk + char
                if chunk and pdfmetrics.stringWidth(next_chunk, font_name, font_size) > max_width:
                    lines.append(chunk)
                    chunk = char
                else:
                    chunk = next_chunk
            current = chunk
        if len(lines) >= max_lines:
            break

    if len(lines) < max_lines and current:
        lines.append(current)

    if len(lines) > max_lines:
        lines = lines[:max_lines]

    # If text was truncated, add an ellipsis while respecting width.
    joined = " ".join(lines)
    if len(joined) < len(clean) and lines:
        last = lines[-1].rstrip()
        while last and pdfmetrics.stringWidth(last + "…", font_name, font_size) > max_width:
            last = last[:-1]
        lines[-1] = (last + "…") if last else "…"
    return lines or [""]


def create_small_title_labels_pdf(
    labels: list[dict[str, object]],
    output_path: str | Path,
    *,
    layout_scale: float = 85.0 / 55.0,
) -> Path:
    """Create one enlarged title page per official KIZ page.

    Every title page inherits the exact MediaBox size of its corresponding
    official Chestny ZNAK PDF page (normally A4 portrait in standard-print
    mode). The original title layout was 20x30 mm and looked correct when the
    whole document was printed at 85%. Chestny ZNAK KIZ pages require 55%, so
    the title layout is enlarged by 85/55 before it is placed in the top-left
    corner. At 55% print scale it therefore has the same physical size and
    readability as the former layout at 85%.

    The official KIZ pages are not transformed here.
    """
    if not labels:
        raise ValueError("No title labels")

    from reportlab.lib.pagesizes import A4
    from reportlab.pdfbase import pdfmetrics

    _regular_font, bold_font = _register_small_label_fonts()
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)

    try:
        scale = float(layout_scale)
    except (TypeError, ValueError):
        scale = 85.0 / 55.0
    # Keep malformed config values from creating a title block larger than A4.
    scale = min(3.0, max(0.5, scale))

    default_page_w, default_page_h = A4
    c = canvas.Canvas(
        str(output),
        pagesize=(default_page_w, default_page_h),
        pageCompression=1,
    )

    # Base layout: 20 mm wide x 30 mm high. Enlarging it by 85/55 means
    # approximately 30.91 x 46.36 mm in the source A4 PDF. Printed at 55%,
    # it becomes approximately 17 x 25.5 mm - exactly the old 85% result.
    label_w = 20 * scale * mm
    label_h = 30 * scale * mm

    for label in labels:
        # Match the corresponding official KIZ page exactly. Standard-print
        # PDFs from Chestny ZNAK are normally A4 portrait, but inheriting the
        # MediaBox avoids mixed page sizes if the source differs slightly.
        try:
            page_w = float(label.get("width_pt") or default_page_w)
            page_h = float(label.get("height_pt") or default_page_h)
        except (TypeError, ValueError):
            page_w, page_h = default_page_w, default_page_h
        if page_w <= 0 or page_h <= 0:
            page_w, page_h = default_page_w, default_page_h

        c.setPageSize((page_w, page_h))

        article = " ".join(str(label.get("seller_article") or "").split())
        name = " ".join(str(label.get("display_name") or article).split())

        # PDF coordinates start at the bottom-left. The enlarged title block
        # remains flush with the top-left corner of the full-size source page.
        box_x = 0.0
        box_y = page_h - label_h

        margin_x = 0.8 * scale * mm
        margin_top = 0.8 * scale * mm
        margin_bottom = 0.8 * scale * mm
        usable_w = label_w - 2 * margin_x

        # Scale typography and spacing together, not only the bounding box.
        # This preserves the proportions that were readable at 85%. Long
        # seller articles are wrapped to two lines so they never run outside
        # the left edge of the A4 page.
        article_size = 8.4 * scale
        article_lines: list[str] = []
        for size_step in range(0, 22):
            candidate_size = max(5.6 * scale, (8.4 - size_step * 0.14) * scale)
            candidate_lines = _wrap_label_text(
                article,
                bold_font,
                candidate_size,
                usable_w,
                2,
            )
            article_lines = candidate_lines
            article_size = candidate_size
            if all(
                pdfmetrics.stringWidth(line, bold_font, candidate_size) <= usable_w + 0.1
                for line in candidate_lines
            ):
                break

        article_line_height = article_size * 1.04
        article_bottom_baseline = box_y + margin_bottom + 0.25 * scale * mm
        article_block_h = len(article_lines) * article_line_height if article else 0.0
        separator_y = article_bottom_baseline + article_block_h + 0.9 * scale * mm

        if article:
            c.setFont(bold_font, article_size)
            article_y = article_bottom_baseline + (len(article_lines) - 1) * article_line_height
            for article_line in article_lines:
                c.drawCentredString(box_x + label_w / 2, article_y, article_line)
                article_y -= article_line_height

        c.setLineWidth(0.3 * scale)
        c.line(
            box_x + margin_x,
            separator_y,
            box_x + label_w - margin_x,
            separator_y,
        )

        name_bottom = separator_y + 0.85 * scale * mm
        name_top = box_y + label_h - margin_top
        available_h = max(10.0 * scale, name_top - name_bottom)

        chosen_size = 8.6 * scale
        lines: list[str] = []
        line_height = 0.0
        for size_step in range(0, 28):
            candidate_size = max(5.6 * scale, (8.6 - size_step * 0.14) * scale)
            candidate_line_height = candidate_size * 1.1
            max_lines = min(7, max(1, int(available_h // candidate_line_height)))
            candidate_lines = _wrap_label_text(
                name,
                bold_font,
                candidate_size,
                usable_w,
                max_lines,
            )
            if len(candidate_lines) * candidate_line_height <= available_h + 0.4 * scale:
                lines = candidate_lines
                chosen_size = candidate_size
                line_height = candidate_line_height
                break

        if not lines:
            chosen_size = 5.6 * scale
            line_height = chosen_size * 1.1
            max_lines = min(7, max(1, int(available_h // line_height)))
            lines = _wrap_label_text(name, bold_font, chosen_size, usable_w, max_lines)

        c.setFont(bold_font, chosen_size)
        total_h = len(lines) * line_height
        y = name_bottom + (available_h + total_h) / 2 - line_height
        for line in lines:
            c.drawCentredString(box_x + label_w / 2, y, line)
            y -= line_height

        c.showPage()

    c.save()
    return output

def merge_pdf_page_refs(
    page_refs: list[tuple[str | Path, int]],
    output_path: str | Path,
) -> Path:
    """Copy selected PDF pages into a new PDF in the supplied order."""
    if not page_refs:
        raise ValueError("No PDF pages to merge")

    reader_cache: dict[str, PdfReader] = {}
    writer = PdfWriter()
    for pdf_path, page_index in page_refs:
        key = str(Path(pdf_path).resolve())
        reader = reader_cache.get(key)
        if reader is None:
            reader = PdfReader(key)
            reader_cache[key] = reader
        if page_index < 0 or page_index >= len(reader.pages):
            raise IndexError(f"PDF page out of range: {key} page {page_index + 1}")
        writer.add_page(reader.pages[page_index])

    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("wb") as handle:
        writer.write(handle)
    return out


def create_native_title_labels_pdf(
    labels: list[dict[str, object]],
    output_path: str | Path,
    *,
    width_mm: float = 30.0,
    height_mm: float = 20.0,
) -> Path:
    """Create compact title labels in their native thermal-label size."""
    if not labels:
        raise ValueError("No title labels")

    regular_font, bold_font = _register_small_label_fonts()
    page_w = float(width_mm) * mm
    page_h = float(height_mm) * mm
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    c = canvas.Canvas(str(out), pagesize=(page_w, page_h), pageCompression=1)

    margin_x = 1.0 * mm
    usable_w = page_w - 2 * margin_x
    for label in labels:
        article = " ".join(str(label.get("seller_article") or "").split())
        name = " ".join(str(label.get("display_name") or article).split())
        task_id = " ".join(str(label.get("assembly_task") or label.get("order_id") or "").split())
        task_digits = "".join(ch for ch in task_id if ch.isdigit())
        task_text = (task_digits[-4:] if task_digits else task_id[-4:]) or "—"

        # The title label immediately precedes its KIZ. Four trailing digits are
        # enough for visual matching and are much faster to read on a 30x20 label.
        task_size = _fit_single_line(task_text, bold_font, 10.8, 8.0, usable_w)
        c.setFont(bold_font, task_size)
        c.drawCentredString(page_w / 2, 1.0 * mm, task_text)

        article_size = _fit_single_line(article, regular_font, 5.5, 3.8, usable_w)
        c.setFont(regular_font, article_size)
        c.drawCentredString(page_w / 2, 4.0 * mm, article)

        separator_y = 6.2 * mm
        c.setLineWidth(0.35)
        c.line(margin_x, separator_y, page_w - margin_x, separator_y)

        name_bottom = 6.7 * mm
        name_top = page_h - 0.8 * mm
        available_h = name_top - name_bottom
        chosen_size = 9.4
        lines: list[str] = []
        line_height = 0.0
        for step in range(34):
            candidate_size = max(5.0, 9.4 - step * 0.14)
            candidate_height = candidate_size * 1.05
            max_lines = max(1, min(5, int(available_h // candidate_height)))
            candidate_lines = _wrap_label_text(
                name, bold_font, candidate_size, usable_w, max_lines
            )
            fits_height = len(candidate_lines) * candidate_height <= available_h + 0.2
            is_complete = not any("…" in line for line in candidate_lines)
            if fits_height and is_complete:
                chosen_size = candidate_size
                line_height = candidate_height
                lines = candidate_lines
                break
        if not lines:
            chosen_size = 5.0
            line_height = chosen_size * 1.05
            lines = _wrap_label_text(name, bold_font, chosen_size, usable_w, 5)

        c.setFont(bold_font, chosen_size)
        total_h = len(lines) * line_height
        y = name_bottom + (available_h + total_h) / 2 - line_height
        for line in lines:
            c.drawCentredString(page_w / 2, y, line)
            y -= line_height
        c.showPage()

    c.save()
    return out


def create_datamatrix_labels_pdf(
    labels: list[dict[str, object]],
    output_path: str | Path,
    *,
    width_mm: float = 30.0,
    height_mm: float = 20.0,
) -> Path:
    """Place official-library DataMatrix PNGs on native 30x20 mm pages.

    The matrix is centered horizontally. Below it FBE prints the human-readable
    identification part only: AI 01 + GTIN and AI 21 + serial. Cryptographic
    groups 91/92/93 stay encoded in the matrix and are not retyped as text.
    """
    if not labels:
        raise ValueError("No DataMatrix labels")

    from PIL import Image

    regular_font, _bold_font = _register_small_label_fonts()
    page_w = float(width_mm) * mm
    page_h = float(height_mm) * mm
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    c = canvas.Canvas(str(out), pagesize=(page_w, page_h), pageCompression=1)

    for label in labels:
        png_path = Path(str(label.get("png_path") or ""))
        if not png_path.exists():
            raise FileNotFoundError(f"DataMatrix PNG not found: {png_path}")
        with Image.open(png_path) as image:
            px_w, px_h = image.size
            dpi_info = image.info.get("dpi") or (300.0, 300.0)
            dpi_x = float(dpi_info[0] or 300.0)
            dpi_y = float(dpi_info[1] or dpi_x)
        natural_w_mm = px_w / dpi_x * 25.4
        natural_h_mm = px_h / dpi_y * 25.4

        # Official renderer at 300 dpi normally produces ~16.26 mm. Keep the
        # natural module geometry, shrinking only if a future template is larger.
        max_matrix_mm = min(float(height_mm) - 3.2, float(width_mm) - 2.0)
        scale = min(1.0, max_matrix_mm / max(natural_w_mm, natural_h_mm))
        draw_w = natural_w_mm * scale * mm
        draw_h = natural_h_mm * scale * mm
        x = (page_w - draw_w) / 2
        y = page_h - draw_h - 0.45 * mm
        c.drawImage(
            str(png_path),
            x,
            y,
            width=draw_w,
            height=draw_h,
            preserveAspectRatio=True,
            mask="auto",
        )

        gtin = "".join(ch for ch in str(label.get("gtin") or "") if ch.isdigit())
        serial = str(label.get("serial") or "")
        human = f"01 {gtin} 21 {serial}".strip()
        max_text_w = page_w - 1.2 * mm
        text_size = _fit_single_line(human, regular_font, 4.5, 3.0, max_text_w)
        c.setFont(regular_font, text_size)
        c.drawCentredString(page_w / 2, 0.65 * mm, human)
        c.showPage()

    c.save()
    return out
