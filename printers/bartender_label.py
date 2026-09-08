from __future__ import annotations

from runtime_safety import print_is_dry_run

import csv
import subprocess
from pathlib import Path
from typing import Any

from printers.coordination import serialized_print_job


# This is the exact contract between FBE and BarTender.
# BarTender templates should use only these fields from internal_label.csv.
INTERNAL_LABEL_FIELDS = [
    "seller_article",
    "category",
    "subcategory",
    "product_name",
    "product_volume",
    "sample",
    "sample_volume",
    # Ready-to-print BarTender field. Empty when there is no sample;
    # otherwise e.g. "+ Сливочная Ваниль 1мл".
    "sample_text",
]


def choose_bartender_template(config: dict[str, Any], product: dict[str, Any]) -> str:
    category = str(product.get("category") or "").strip()
    templates_by_category = config.get("bartender_templates_by_category") or {}

    if category and category in templates_by_category:
        return str(templates_by_category[category])

    fallback = config.get("bartender_template") or templates_by_category.get("default")
    if not fallback:
        raise ValueError("BarTender template is not configured")
    return str(fallback)


def _build_sample_text(sample: str, sample_volume: str) -> str:
    sample = str(sample or "").strip()
    sample_volume = str(sample_volume or "").strip()
    if not sample:
        return ""
    sample_name = " ".join(part for part in [sample, sample_volume] if part).strip()
    return f"+ {sample_name}" if sample_name else ""


def build_internal_label_row(product: dict[str, Any]) -> dict[str, str]:
    """Map the normalized Excel product row to the minimal BarTender CSV row."""
    sample = str(product.get("sample", "") or "")
    sample_volume = str(product.get("sample_volume", "") or "")

    return {
        "seller_article": str(product.get("seller_article", "") or ""),
        "category": str(product.get("category", "") or ""),
        "subcategory": str(product.get("subcategory", "") or ""),
        # In your Excel, the actual printable product name comes from column "Продукт".
        "product_name": str(product.get("product", "") or product.get("display_name", "") or ""),
        "product_volume": str(product.get("volume", "") or ""),
        "sample": sample,
        "sample_volume": sample_volume,
        "sample_text": _build_sample_text(sample, sample_volume),
    }


def write_internal_label_csv(csv_path: str, product: dict[str, Any]) -> None:
    Path(csv_path).parent.mkdir(parents=True, exist_ok=True)
    row = build_internal_label_row(product)

    with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=INTERNAL_LABEL_FIELDS,
            delimiter=";",
            lineterminator="\n",
        )
        writer.writeheader()
        writer.writerow(row)


def _bt_switch(name: str, value: str) -> str:
    """Return a BarTender command-line switch.

    Important: when subprocess is called with a list, DO NOT add manual quotes
    inside the argument. Python/Windows will quote the whole argument when needed.
    Manual quotes caused BarTender to parse paths/printers incorrectly, e.g.
    printer name became '\\' or /perfume.btw was treated as a separate switch.
    """
    clean_value = str(value).strip().strip('"')
    return f"/{name}={clean_value}"


def _write_debug_command(cmd: list[str], config: dict[str, Any]) -> None:
    try:
        csv_path = Path(str(config.get("bartender_csv", "data/print/internal_label.csv")))
        debug_path = csv_path.parent / "last_bartender_command.txt"
        debug_path.parent.mkdir(parents=True, exist_ok=True)
        debug_path.write_text("\n".join(cmd), encoding="utf-8")
    except Exception:
        # Debug logging must never break printing.
        pass


@serialized_print_job
def print_internal_label(config: dict[str, Any], product: dict[str, Any]) -> None:
    csv_path = str(Path(config["bartender_csv"]).resolve())
    template_path = str(Path(choose_bartender_template(config, product)).resolve())
    bartender_exe = str(Path(config["bartender_exe"]).resolve())
    printer_name = str(config["internal_label_printer_name"]).strip().strip('"')

    write_internal_label_csv(csv_path, product)

    if not Path(template_path).exists():
        raise FileNotFoundError(
            "BarTender template not found. Make sure the project is fully EXTRACTED from ZIP "
            f"and the .btw file exists: {template_path}"
        )
    if not Path(bartender_exe).exists():
        raise FileNotFoundError(f"BarTender executable not found: {bartender_exe}")
    if not printer_name:
        raise ValueError("internal_label_printer_name is empty in config.json")

    if print_is_dry_run(config):
        category = product.get("category", "")
        print(
            f"[DRY RUN] BarTender would print category={category!r} "
            f"template={template_path} CSV={csv_path} printer={printer_name}"
        )
        return

    cmd = [
        bartender_exe,
        _bt_switch("F", template_path),
        _bt_switch("PRN", printer_name),
        _bt_switch("D", csv_path),
        "/P",
        "/X",
    ]
    _write_debug_command(cmd, config)
    subprocess.run(
        cmd,
        check=True,
        timeout=max(10, int(config.get("bartender_print_timeout_seconds", 120))),
    )
