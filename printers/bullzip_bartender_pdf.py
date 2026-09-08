from __future__ import annotations

from runtime_safety import print_is_dry_run

import csv
import os
import subprocess
import time
from pathlib import Path
from typing import Any

from printers.coordination import serialized_print_job

from reportlab.lib.units import mm
from reportlab.pdfgen import canvas

from printers.bartender_label import (
    INTERNAL_LABEL_FIELDS,
    build_internal_label_row,
    choose_bartender_template,
)


_PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _expand_path(value: str) -> Path:
    """Expand environment variables and user home in a Windows-friendly path."""
    return Path(os.path.expandvars(os.path.expanduser(value))).resolve()


def bullzip_runonce_dir(config: dict[str, Any]) -> Path:
    configured = str(config.get("bullzip_runonce_dir") or "").strip()
    if configured:
        return _expand_path(configured)

    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / "PDF Writer" / "Bullzip PDF Printer"

    # Safe fallback for non-standard environments.
    return (_PROJECT_ROOT / "data" / "print" / "bullzip_runonce").resolve()


def write_bullzip_runonce(config: dict[str, Any], output_pdf: str | Path) -> Path:
    """Configure Bullzip for exactly the next print job.

    Bullzip reads runonce.ini for the next job, then removes it. This lets FBE
    save a BarTender print job to a known PDF path without any Save As dialog.
    """
    output = Path(output_pdf).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)

    folder = bullzip_runonce_dir(config)
    folder.mkdir(parents=True, exist_ok=True)
    runonce = folder / "runonce.ini"

    lines = [
        "[PDF Printer]",
        f"Output={output}",
        "ShowSettings=never",
        "ShowSaveAS=never",
        "ShowProgress=no",
        "ShowProgressFinished=no",
        "ShowPDF=no",
        "ConfirmOverwrite=no",
        "RememberLastFolderName=no",
        "RememberLastFileName=no",
    ]
    runonce.write_text("\n".join(lines), encoding="utf-8")
    return runonce


def write_single_internal_csv(csv_path: str | Path, product: dict[str, Any]) -> Path:
    path = Path(csv_path).resolve()
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


def _bt_switch(name: str, value: str | Path) -> str:
    # No manual quotes here. subprocess(list) handles spaces correctly.
    return f"/{name}={str(value).strip().strip(chr(34))}"


def _write_debug(config: dict[str, Any], name: str, lines: list[str]) -> None:
    try:
        base = Path(str(config.get("bartender_csv", "data/print/internal_label.csv"))).resolve().parent
        base.mkdir(parents=True, exist_ok=True)
        (base / name).write_text("\n".join(lines), encoding="utf-8")
    except Exception:
        pass


def _make_placeholder_pdf(path: Path, text: str, width_mm: int, height_mm: int) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    c = canvas.Canvas(str(path), pagesize=(width_mm * mm, height_mm * mm))
    c.setFont("Helvetica", 7)
    c.drawString(4 * mm, height_mm * mm - 8 * mm, text[:80])
    c.showPage()
    c.save()
    return path




def write_internal_batch_csv(csv_path: str | Path, rows: list[dict[str, Any]]) -> Path:
    """Write a multi-row CSV for a single BarTender template run."""
    path = Path(csv_path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=INTERNAL_LABEL_FIELDS,
            delimiter=";",
            lineterminator="\n",
        )
        writer.writeheader()
        for row in rows:
            writer.writerow({field: str(row.get(field, "") or "") for field in INTERNAL_LABEL_FIELDS})
    return path


def _make_placeholder_batch_pdf(path: Path, rows: list[dict[str, Any]], width_mm: int, height_mm: int) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    c = canvas.Canvas(str(path), pagesize=(width_mm * mm, height_mm * mm))
    for index, row in enumerate(rows, start=1):
        c.setFont("Helvetica", 7)
        text = f"DRY RUN internal label {index} {row.get('seller_article','')}"
        c.drawString(4 * mm, height_mm * mm - 8 * mm, text[:80])
        c.showPage()
    c.save()
    return path


@serialized_print_job
def render_internal_labels_batch_pdf_with_bullzip(
    config: dict[str, Any],
    rows: list[dict[str, Any]],
    template_path: str | Path,
    output_pdf: str | Path,
    batch_name: str = "batch",
) -> Path:
    """Render many internal labels with one BarTender/Bullzip run.

    This is the fast path for the original-WB-PDF workflow. Instead of opening
    BarTender once per item, FBE writes a multi-row CSV for one template and asks
    BarTender to print all records in that CSV to Bullzip. The resulting PDF must
    contain one page per CSV row, in the same order.
    """
    if not rows:
        raise ValueError("No rows for BarTender batch")

    output = Path(output_pdf).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        output.unlink()

    template = Path(template_path).resolve()
    bartender_exe = Path(config["bartender_exe"]).resolve()
    bullzip_printer = str(config.get("bullzip_printer_name") or "Bullzip PDF Printer").strip()

    # Keep batch CSVs separate from the legacy one-row CSV so a failed batch does
    # not leave parents with a half-written manual file.
    csv_base = Path(str(config.get("bartender_csv", "data/print/internal_label.csv"))).resolve().parent
    csv_path = csv_base / "batches" / f"{batch_name}.csv"
    csv_path = write_internal_batch_csv(csv_path, rows)

    if not print_is_dry_run(config):
        if not template.exists():
            raise FileNotFoundError(f"BarTender template not found: {template}")
        if not bartender_exe.exists():
            raise FileNotFoundError(f"BarTender executable not found: {bartender_exe}")
        if not bullzip_printer:
            raise ValueError("bullzip_printer_name is empty")

    if print_is_dry_run(config):
        pdf = _make_placeholder_batch_pdf(
            output,
            rows,
            int(config.get("wb_sticker_width", 58)),
            int(config.get("wb_sticker_height", 40)),
        )
        _write_debug(config, f"last_bartender_batch_{batch_name}.txt", [
            "DRY RUN BATCH",
            f"rows={len(rows)}",
            str(bartender_exe),
            _bt_switch("F", template),
            _bt_switch("PRN", bullzip_printer),
            _bt_switch("D", csv_path),
            "/P",
            "/X",
            f"output={output}",
        ])
        return pdf

    runonce = write_bullzip_runonce(config, output)
    cmd = [
        str(bartender_exe),
        _bt_switch("F", template),
        _bt_switch("PRN", bullzip_printer),
        _bt_switch("D", csv_path),
        "/P",
        "/X",
    ]
    _write_debug(config, f"last_bartender_batch_{batch_name}.txt", [
        *cmd,
        f"rows={len(rows)}",
        f"csv={csv_path}",
        f"runonce={runonce}",
        f"output={output}",
    ])

    subprocess.run(
        cmd,
        check=True,
        timeout=int(config.get("bartender_pdf_timeout_seconds", 120)),
    )

    deadline = time.time() + int(config.get("bullzip_wait_timeout_seconds", 120))
    while time.time() < deadline:
        if output.exists() and output.stat().st_size > 0:
            return output
        time.sleep(0.3)

    raise FileNotFoundError(
        f"Bullzip did not create batch PDF {batch_name}: {output}. "
        f"Check data/print/last_bartender_batch_{batch_name}.txt and Bullzip runonce.ini location."
    )

@serialized_print_job
def render_internal_label_pdf_with_bullzip(
    config: dict[str, Any],
    product: dict[str, Any],
    order_id: int,
    output_pdf: str | Path,
) -> Path:
    """Render one internal label to PDF through BarTender -> Bullzip.

    This intentionally mirrors the proven manual workflow:
    BarTender prints the .btw template to the Bullzip PDF virtual printer, and
    Bullzip writes the result to a deterministic file path.
    """
    output = Path(output_pdf).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        output.unlink()

    csv_path = write_single_internal_csv(config["bartender_csv"], product)
    template_path = Path(choose_bartender_template(config, product)).resolve()
    bartender_exe = Path(config["bartender_exe"]).resolve()
    bullzip_printer = str(config.get("bullzip_printer_name") or "Bullzip PDF Printer").strip()

    if not template_path.exists():
        raise FileNotFoundError(f"BarTender template not found: {template_path}")
    if not bartender_exe.exists():
        raise FileNotFoundError(f"BarTender executable not found: {bartender_exe}")
    if not bullzip_printer:
        raise ValueError("bullzip_printer_name is empty")

    if print_is_dry_run(config):
        pdf = _make_placeholder_pdf(
            output,
            f"DRY RUN internal label {order_id} {product.get('seller_article','')}",
            int(config.get("wb_sticker_width", 58)),
            int(config.get("wb_sticker_height", 40)),
        )
        _write_debug(config, "last_bartender_bullzip_command.txt", [
            "DRY RUN",
            str(bartender_exe),
            _bt_switch("F", template_path),
            _bt_switch("PRN", bullzip_printer),
            _bt_switch("D", csv_path),
            "/P",
            "/X",
            f"output={output}",
        ])
        return pdf

    runonce = write_bullzip_runonce(config, output)
    cmd = [
        str(bartender_exe),
        _bt_switch("F", template_path),
        _bt_switch("PRN", bullzip_printer),
        _bt_switch("D", csv_path),
        "/P",
        "/X",
    ]
    _write_debug(config, "last_bartender_bullzip_command.txt", [
        *cmd,
        f"runonce={runonce}",
        f"output={output}",
    ])

    subprocess.run(
        cmd,
        check=True,
        timeout=int(config.get("bartender_pdf_timeout_seconds", 120)),
    )

    deadline = time.time() + int(config.get("bullzip_wait_timeout_seconds", 120))
    while time.time() < deadline:
        if output.exists() and output.stat().st_size > 0:
            return output
        time.sleep(0.3)

    raise FileNotFoundError(
        f"Bullzip did not create PDF for order {order_id}: {output}. "
        "Check data/print/last_bartender_bullzip_command.txt and Bullzip runonce.ini location."
    )
