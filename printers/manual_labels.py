from __future__ import annotations

from runtime_safety import print_is_dry_run

import csv
import subprocess
import time
from pathlib import Path
from typing import Any

from printers.coordination import serialized_print_job

from reportlab.lib.units import mm
from reportlab.pdfgen import canvas

from printers.bartender_label import _bt_switch
from printers.bullzip_bartender_pdf import write_bullzip_runonce


_PROJECT_ROOT = Path(__file__).resolve().parents[1]


# IMPORTANT: manual printing intentionally uses two different CSV contracts.
# Do not merge these into one file: BarTender templates become fragile when a
# single temporary CSV contains fields from different source sheets.
SAMPLE_LABEL_FIELDS = ["№", "Name", "WB code", "QR link"]
NAME_LABEL_FIELDS = [
    "Артикул продавца",
    "Подкатегория",
    "Продукт",
    "Объем (парфюмерия)",
    "Пробник",
    "Объем пробника",
    "Категория",
    "КИЗ",
    "№",
    "Название",
    "Название мотива",
    "Формат",
]


def _clean(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _project_relative(path_value: str | Path) -> Path:
    path = Path(str(path_value))
    return path if path.is_absolute() else (_PROJECT_ROOT / path).resolve()


def manual_csv_path(config: dict[str, Any], label_type: str) -> Path:
    by_type = config.get("manual_bartender_csv_by_type") or {}
    if label_type in by_type:
        return _project_relative(by_type[label_type])

    # Backwards-compatible fallback, but v14 config should use the by_type map.
    fallback = config.get("manual_bartender_csv", "data/print/manual_label.csv")
    return _project_relative(fallback)


def manual_fields(label_type: str) -> list[str]:
    if label_type == "samples":
        return SAMPLE_LABEL_FIELDS
    if label_type == "names":
        return NAME_LABEL_FIELDS
    raise ValueError(f"Unknown manual label type: {label_type}")


def template_for_manual_kind(config: dict[str, Any], label_type: str) -> Path:
    templates = config.get("manual_bartender_templates") or {}
    if label_type not in templates:
        raise ValueError(f"Manual label template is not configured: {label_type}")
    return Path(str(templates[label_type])).resolve()


def build_manual_row(label_type: str, record: dict[str, Any], quantity: int | None = None) -> dict[str, str]:
    """Build one BarTender row.

    Quantity is represented by duplicating rows, not by a quantity column. This
    keeps BarTender templates simple: they print all records from the temporary
    CSV and each row equals exactly one physical label.
    """
    if label_type == "samples":
        return {
            "№": _clean(record.get("№")),
            "Name": _clean(record.get("Name")),
            "WB code": _clean(record.get("WB code")),
            "QR link": _clean(record.get("QR link")),
        }

    if label_type == "names":
        raw = record.get("raw_excel") or {}
        return {
            "Артикул продавца": _clean(raw.get("Артикул продавца") or record.get("seller_article")),
            "Подкатегория": _clean(raw.get("Подкатегория") or record.get("subcategory")),
            "Продукт": _clean(raw.get("Продукт") or record.get("product")),
            "Объем (парфюмерия)": _clean(raw.get("Объем (парфюмерия)") or record.get("volume")),
            "Пробник": _clean(raw.get("Пробник") or record.get("sample")),
            "Объем пробника": _clean(raw.get("Объем пробника") or record.get("sample_volume")),
            "Категория": _clean(raw.get("Категория") or record.get("category")),
            "КИЗ": _clean(raw.get("КИЗ") or record.get("excel_kiz")),
            "№": _clean(raw.get("№") or record.get("name_no")),
            "Название": _clean(raw.get("Название") or record.get("product")),
            "Название мотива": _clean(raw.get("Название мотива") or record.get("inspiration_name")),
            "Формат": _clean(raw.get("Формат") or raw.get("Объем (парфюмерия)") or record.get("volume")),
        }

    raise ValueError(f"Unknown manual label type: {label_type}")


def write_manual_label_csv(csv_path: str | Path, rows: list[dict[str, str]], label_type: str) -> Path:
    path = Path(csv_path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = manual_fields(label_type)

    # Normalize rows so the file contains exactly the expected columns.
    normalized = [{field: _clean(row.get(field)) for field in fields} for row in rows]

    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields, delimiter=";", lineterminator="\n")
        writer.writeheader()
        writer.writerows(normalized)
    return path


def _write_debug(config: dict[str, Any], file_name: str, lines: list[str]) -> None:
    try:
        base_value = str(config.get("print_debug_dir") or "data/print")
        base = _project_relative(base_value)
        base.mkdir(parents=True, exist_ok=True)
        (base / file_name).write_text("\n".join(lines), encoding="utf-8")
    except Exception:
        pass


def _make_placeholder_pdf(path: Path, rows: list[dict[str, str]], width_mm: int, height_mm: int) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    c = canvas.Canvas(str(path), pagesize=(width_mm * mm, height_mm * mm))
    for i, row in enumerate(rows, start=1):
        c.setFont("Helvetica", 7)
        title = row.get("Name") or row.get("Продукт") or "manual label"
        c.drawString(4 * mm, height_mm * mm - 7 * mm, f"DRY RUN {i}: {title}"[:80])
        c.showPage()
    c.save()
    return path


@serialized_print_job
def run_manual_bartender_print(
    config: dict[str, Any],
    label_type: str,
    rows: list[dict[str, str]],
    printer_name: str,
) -> None:
    csv_path = write_manual_label_csv(manual_csv_path(config, label_type), rows, label_type)
    template_path = template_for_manual_kind(config, label_type)
    bartender_exe = Path(str(config["bartender_exe"])).resolve()
    printer = str(printer_name or "").strip().strip('"')

    cmd = [
        str(bartender_exe),
        _bt_switch("F", str(template_path)),
        _bt_switch("PRN", printer),
        _bt_switch("D", str(csv_path)),
        "/P",
        "/X",
    ]
    _write_debug(config, f"last_manual_{label_type}_bartender_command.txt", [
        *cmd,
        f"csv={csv_path}",
        f"template={template_path}",
        f"rows={len(rows)}",
    ])

    if print_is_dry_run(config):
        return

    if not template_path.exists():
        raise FileNotFoundError(f"BarTender manual template not found: {template_path}")
    if not bartender_exe.exists():
        raise FileNotFoundError(f"BarTender executable not found: {bartender_exe}")
    if not printer:
        raise ValueError("Printer name is empty")
    if not csv_path.exists():
        raise FileNotFoundError(f"Manual CSV was not created: {csv_path}")

    subprocess.run(cmd, check=True, timeout=int(config.get("manual_bartender_timeout_seconds", 120)))


@serialized_print_job
def render_manual_labels_pdf_with_bullzip(
    config: dict[str, Any],
    label_type: str,
    rows: list[dict[str, str]],
    output_pdf: str | Path,
) -> Path:
    output = Path(output_pdf).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        output.unlink()

    if print_is_dry_run(config):
        pdf = _make_placeholder_pdf(
            output,
            rows,
            int(config.get("small_label_width", config.get("wb_sticker_width", 58))),
            int(config.get("small_label_height", config.get("wb_sticker_height", 40))),
        )
        _write_debug(config, f"last_manual_{label_type}_bartender_pdf_command.txt", ["DRY RUN", f"output={output}"])
        return pdf

    runonce = write_bullzip_runonce(config, output)
    run_manual_bartender_print(
        config=config,
        label_type=label_type,
        rows=rows,
        printer_name=str(config.get("bullzip_printer_name") or "Bullzip PDF Printer"),
    )

    deadline = time.time() + int(config.get("manual_pdf_timeout_seconds", config.get("bullzip_wait_timeout_seconds", 120)))
    while time.time() < deadline:
        if output.exists() and output.stat().st_size > 0:
            return output
        time.sleep(0.3)

    raise FileNotFoundError(
        f"Bullzip did not create manual label PDF: {output}. "
        f"runonce={runonce}. Check data/print/last_manual_{label_type}_bartender_pdf_command.txt"
    )
