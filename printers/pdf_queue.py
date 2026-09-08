from __future__ import annotations

from runtime_safety import print_is_dry_run

import csv
import os
import subprocess
import time
import xml.sax.saxutils as xml_escape
from pathlib import Path
from typing import Any, Iterable

from PIL import Image
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas

from printers.bartender_label import (
    INTERNAL_LABEL_FIELDS,
    build_internal_label_row,
    choose_bartender_template,
)
from printers.coordination import serialized_print_job


_PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _write_csv(csv_path: str | Path, product: dict[str, Any]) -> Path:
    path = Path(csv_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    row = build_internal_label_row(product)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=INTERNAL_LABEL_FIELDS,
            delimiter=";",
            lineterminator="\n",
        )
        writer.writeheader()
        writer.writerow(row)
    return path


def _write_debug(path: Path, lines: Iterable[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(str(x) for x in lines), encoding="utf-8")


def _debug_path(config: dict[str, Any], name: str) -> Path:
    raw = str(config.get("print_debug_dir") or "data/print")
    base = Path(raw)
    if not base.is_absolute():
        base = (_PROJECT_ROOT / base).resolve()
    return base / name


@serialized_print_job
def export_internal_label_image(
    config: dict[str, Any],
    product: dict[str, Any],
    order_id: int,
    output_dir: str | Path,
) -> Path:
    """Render one BarTender internal label to PNG using BTXML ExportPrintPreviewToImage.

    This is used for the fast paired-PDF mode: FBE renders internal labels to images,
    alternates them with WB sticker PNGs, and prints/opens one PDF queue. The actual
    label layout is still fully controlled by your .btw templates.
    """
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    csv_path = Path(config["bartender_csv"])
    _write_csv(csv_path, product)

    template_path = Path(choose_bartender_template(config, product)).resolve()
    bartender_exe = Path(config["bartender_exe"]).resolve()

    if not template_path.exists():
        raise FileNotFoundError(f"BarTender template not found: {template_path}")
    if not bartender_exe.exists():
        raise FileNotFoundError(f"BarTender executable not found: {bartender_exe}")

    # Clean old exports for this order so we can detect the freshly rendered image.
    for old in out_dir.glob(f"internal_{order_id}_*.png"):
        try:
            old.unlink()
        except OSError:
            pass

    xml_dir = _debug_path(config, "btxml")
    xml_dir.mkdir(parents=True, exist_ok=True)
    xml_path = xml_dir / f"export_internal_{order_id}.xml"

    dpi = int(config.get("paired_pdf_dpi", 203))

    # IMPORTANT:
    # Do NOT specify a RecordSet name here. Different BarTender templates can name
    # their text-file database connection differently (e.g. not "TextFile 1" /
    # "Text File 1"), which caused BarTender error #3908.
    #
    # FBE writes exactly one row to config["bartender_csv"] before each export.
    # The .btw template must already be connected to that CSV file. BarTender then
    # uses the template's saved database connection, so there is no fragile
    # connection-name dependency in BTXML.
    xml = f'''<?xml version="1.0" encoding="utf-8"?>
<XMLScript Version="2.0">
  <Command Name="ExportInternal_{order_id}">
    <ExportPrintPreviewToImage ReturnImageInResponse="false">
      <Format>{xml_escape.escape(str(template_path))}</Format>
      <Folder>{xml_escape.escape(str(out_dir.resolve()))}</Folder>
      <FileNameTemplate>internal_{order_id}_%PageNumber%.png</FileNameTemplate>
      <ImageFormatType>PNG</ImageFormatType>
      <Colors>btColors24Bit</Colors>
      <DPI>{dpi}</DPI>
      <Overwrite>true</Overwrite>
      <IncludeMargins>false</IncludeMargins>
      <IncludeBorder>false</IncludeBorder>
      <BackgroundColor>16777215</BackgroundColor>
    </ExportPrintPreviewToImage>
  </Command>
</XMLScript>
'''
    xml_path.write_text(xml, encoding="utf-8")

    if print_is_dry_run(config):
        # Create a placeholder image so PDF generation can be tested in dry-run mode.
        placeholder = out_dir / f"internal_{order_id}_1.png"
        width_px = int(float(config.get("wb_sticker_width", 58)) / 25.4 * dpi)
        height_px = int(float(config.get("wb_sticker_height", 40)) / 25.4 * dpi)
        img = Image.new("RGB", (width_px, height_px), "white")
        img.save(placeholder)
        _write_debug(_debug_path(config, "last_bartender_export_command.txt"), [
            "DRY RUN export",
            str(bartender_exe),
            "/X",
            f"/XMLScript={xml_path.resolve()}",
        ])
        return placeholder

    cmd = [str(bartender_exe), "/X", f"/XMLScript={str(xml_path.resolve())}"]
    _write_debug(_debug_path(config, "last_bartender_export_command.txt"), cmd)
    subprocess.run(
        cmd,
        check=True,
        timeout=int(config.get("bartender_export_timeout_seconds", 60)),
    )

    # BarTender docs say each exported page is saved as a unique image. Usually page 1
    # becomes internal_<order>_1.png; glob makes this robust.
    deadline = time.time() + 10
    candidates: list[Path] = []
    while time.time() < deadline:
        candidates = sorted(out_dir.glob(f"internal_{order_id}_*.png"))
        if candidates:
            break
        time.sleep(0.2)

    if not candidates:
        raise FileNotFoundError(
            f"BarTender did not create internal label image for order {order_id}. "
            f"Check {xml_path} and data/print/last_bartender_export_command.txt"
        )
    return candidates[0]


def create_paired_pdf(
    pairs: list[tuple[Path, Path]],
    output_pdf: str | Path,
    width_mm: int = 58,
    height_mm: int = 40,
) -> Path:
    """Create one PDF queue: WB sticker page, internal label page, repeated."""
    if not pairs:
        raise ValueError("No pairs for PDF queue")

    out = Path(output_pdf)
    out.parent.mkdir(parents=True, exist_ok=True)

    page_w = width_mm * mm
    page_h = height_mm * mm
    c = canvas.Canvas(str(out), pagesize=(page_w, page_h))

    for wb_img, internal_img in pairs:
        for img_path in (wb_img, internal_img):
            if not Path(img_path).exists():
                raise FileNotFoundError(str(img_path))
            with Image.open(img_path) as img:
                img = img.convert("RGB")
                c.drawInlineImage(img, 0, 0, width=page_w, height=page_h)
                c.showPage()

    c.save()
    return out


@serialized_print_job
def print_pdf_queue(config: dict[str, Any], pdf_path: str | Path) -> str:
    """Try to print/open the paired PDF.

    Best silent mode: install SumatraPDF and set sumatra_pdf_exe in config.json.
    Fallback: Windows 'printto' verb. If the associated PDF app does not support it,
    we open the PDF for manual printing.
    """
    pdf = Path(pdf_path).resolve()
    printer = str(config.get("wb_printer_name") or config.get("internal_label_printer_name") or "").strip()
    sumatra = str(config.get("sumatra_pdf_exe") or "").strip()

    debug_lines = [f"pdf={pdf}", f"printer={printer}"]

    if print_is_dry_run(config):
        _write_debug(_debug_path(config, "last_pdf_print.txt"), ["DRY RUN", *debug_lines])
        return "dry-run"

    if sumatra and Path(sumatra).exists():
        cmd = [sumatra, "-print-to", printer, "-print-settings", "noscale", str(pdf)]
        _write_debug(_debug_path(config, "last_pdf_print.txt"), cmd)
        subprocess.run(cmd, check=True, timeout=int(config.get("pdf_print_timeout_seconds", 120)))
        return "sumatra"

    try:
        if os.name == "nt":
            # Uses the registered PDF application. Some viewers support printto,
            # some ignore it; if it fails, we fall back to opening the PDF.
            os.startfile(str(pdf), "printto", f'"{printer}"')  # type: ignore[attr-defined]
            _write_debug(_debug_path(config, "last_pdf_print.txt"), ["windows-printto", *debug_lines])
            return "windows-printto"
    except Exception as exc:
        _write_debug(_debug_path(config, "last_pdf_print.txt"), ["printto failed", repr(exc), *debug_lines])

    # Safe fallback: open PDF. User prints manually; order is preserved in one file.
    if os.name == "nt":
        os.startfile(str(pdf))  # type: ignore[attr-defined]
    _write_debug(_debug_path(config, "last_pdf_print.txt"), ["opened", *debug_lines])
    return "opened"
