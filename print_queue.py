from __future__ import annotations

import json
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from printers.bartender_label import print_internal_label
from printers.coordination import serialized_print_job
from printers.wb_sticker import print_wb_sticker


_PRINT_LOG_LOCK = threading.Lock()


def log_print_event(event: dict[str, Any], config: dict[str, Any] | None = None) -> None:
    with _PRINT_LOG_LOCK:
        configured = str((config or {}).get("print_log_path") or "").strip()
        log_path = Path(configured) if configured else Path(__file__).resolve().parent / "logs" / "print_log.jsonl"
        if not log_path.is_absolute():
            log_path = (Path(__file__).resolve().parent / log_path).resolve()
        log_path.parent.mkdir(parents=True, exist_ok=True)
        event = {"ts": datetime.now().isoformat(timespec="seconds"), **event}
        with log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")


@serialized_print_job
def print_order_pair(
    config: dict[str, Any],
    order_id: int,
    seller_article: str,
    wb_sticker_path: str,
    product: dict[str, Any],
) -> None:
    print_wb_sticker(
        printer_name=config["wb_printer_name"],
        sticker_path=wb_sticker_path,
        sticker_type=config.get("wb_sticker_type", "png"),
        dry_run=config.get("dry_run_print", True),
        width_mm=int(config.get("wb_sticker_width", 58)),
        height_mm=int(config.get("wb_sticker_height", 40)),
    )

    time.sleep(float(config.get("print_gap_seconds", 0.7)))

    print_internal_label(config, product)

    log_print_event(
        {
            "order_id": order_id,
            "seller_article": seller_article,
            "wb_sticker_path": wb_sticker_path,
            "product": product,
            "dry_run": config.get("dry_run_print", True),
        },
        config=config,
    )
