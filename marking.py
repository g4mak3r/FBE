from __future__ import annotations

from collections import Counter
import re
from typing import Any


_TRUE_VALUES = {"да", "yes", "y", "true", "1", "+"}
_VALID_GTIN_LENGTHS = {8, 12, 13, 14}
_GS = "\x1d"


_RU_LAYOUT_MARKERS = re.compile(r"[А-Яа-яЁё№]")

# Physical-key mapping from the Russian Windows layout back to US English.
# It is applied only when Cyrillic/layout-specific characters are detected, so a
# correctly scanned ASCII code is never rewritten.
_EN_LOWER_KEYS = "`qwertyuiop[]asdfghjkl;'zxcvbnm,./"
_RU_LOWER_KEYS = "ёйцукенгшщзхъфывапролджэячсмитьбю."
_EN_UPPER_KEYS = '~QWERTYUIOP{}ASDFGHJKL:"ZXCVBNM<>?'
_RU_UPPER_KEYS = "ЁЙЦУКЕНГШЩЗХЪФЫВАПРОЛДЖЭЯЧСМИТЬБЮ,"
_EN_SHIFT_NUMBER_KEYS = '!@#$%^&*()_+'
_RU_SHIFT_NUMBER_KEYS = '!"№;%:?*()_+'
_RU_TO_EN_LAYOUT = str.maketrans(
    _RU_LOWER_KEYS + _RU_UPPER_KEYS + _RU_SHIFT_NUMBER_KEYS,
    _EN_LOWER_KEYS + _EN_UPPER_KEYS + _EN_SHIFT_NUMBER_KEYS,
)


def recover_english_keyboard_layout(value: Any) -> tuple[str, bool]:
    """Recover scanner text entered while Windows used the Russian layout.

    Keyboard-wedge scanners send physical key presses. With the Russian layout,
    Latin letters become Cyrillic characters. Marking codes are ASCII-only, so
    Cyrillic in a scanned code is an unambiguous layout error.
    """
    text = "" if value is None else str(value)
    if not _RU_LAYOUT_MARKERS.search(text):
        return text, False
    return text.translate(_RU_TO_EN_LAYOUT), True


class MarkingCodeError(ValueError):
    pass


def clean_text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def is_marking_required(value: Any) -> bool:
    return clean_text(value).lower() in _TRUE_VALUES


def normalize_gtin(value: Any) -> str:
    """Normalize an Excel GTIN without losing leading zeroes stored as text."""
    text = clean_text(value).replace("\u00a0", "").replace(" ", "")
    if text.startswith("'"):
        text = text[1:]
    if text.endswith(".0") and text[:-2].isdigit():
        text = text[:-2]
    return text


def canonical_gtin14(value: Any) -> str:
    gtin = normalize_gtin(value)
    if gtin.isdigit() and len(gtin) in _VALID_GTIN_LENGTHS:
        return gtin.zfill(14)
    return gtin


def validate_gtin(gtin: str) -> str | None:
    if not gtin:
        return "GTIN не заполнен"
    if not gtin.isdigit():
        return "GTIN должен содержать только цифры"
    if len(gtin) not in _VALID_GTIN_LENGTHS:
        return f"Некорректная длина GTIN: {len(gtin)}"
    return None


def seller_article_from_order(order: dict[str, Any]) -> str:
    return clean_text(order.get("article") or order.get("supplierArticle") or order.get("seller_article"))


def normalize_scanned_code(value: Any) -> str:
    """Preserve the code while accepting GS notations and a wrong RU layout.

    Browser/scanner combinations may pass ASCII 29 directly or show it as
    <GS>, [GS], {GS}, \x1D, or \u001D. Keyboard-wedge scanners can also
    type Cyrillic when Windows is left in the Russian layout; that input is
    recovered back to the corresponding US-English physical keys.
    """
    text = "" if value is None else str(value)
    text = text.replace("\r", "").replace("\n", "")
    text, _ = recover_english_keyboard_layout(text)
    replacements = {
        "<GS>": _GS,
        "[GS]": _GS,
        "{GS}": _GS,
        "\\x1d": _GS,
        "\\x1D": _GS,
        "\\u001d": _GS,
        "\\u001D": _GS,
    }
    for source, target in replacements.items():
        text = text.replace(source, target)
    if text.startswith("]d2") or text.startswith("]Q3"):
        text = text[3:]
    return text


def display_marking_code(raw_code: str, max_length: int = 96) -> str:
    text = str(raw_code or "").replace(_GS, "<GS>")
    return text if len(text) <= max_length else text[: max_length - 1] + "…"


def parse_marking_code(value: Any) -> dict[str, Any]:
    raw_code = normalize_scanned_code(value)
    if not raw_code:
        raise MarkingCodeError("Код не получен")
    if len(raw_code) < 18:
        raise MarkingCodeError("Слишком короткая строка для кода маркировки")

    # Standard GS1 DataMatrix starts with application identifier 01 (GTIN-14)
    # followed by AI 21 (serial). We only parse stable elements here and keep
    # the original full sequence unchanged for WB/SUZ operations.
    match = re.search(r"(?:^|\x1d)01(\d{14})21", raw_code)
    if not match:
        match = re.search(r"01(\d{14})21", raw_code)
    if not match:
        raise MarkingCodeError("Не удалось найти в коде связку 01 + GTIN-14 + 21")

    gtin = match.group(1)
    serial_start = match.end()
    tail = raw_code[serial_start:]
    serial = tail.split(_GS, 1)[0]
    if not serial:
        raise MarkingCodeError("В коде не найден серийный номер после идентификатора 21")

    original_text = "" if value is None else str(value).replace("\r", "").replace("\n", "")
    _, layout_corrected = recover_english_keyboard_layout(original_text)
    return {
        "raw_code": raw_code,
        "gtin": gtin,
        "serial": serial,
        "display_code": display_marking_code(raw_code),
        "layout_corrected": layout_corrected,
    }


def _order_id(order: dict[str, Any]) -> int:
    return int(order.get("id") or order.get("order_id") or 0)


def _order_raw(order: dict[str, Any]) -> dict[str, Any]:
    raw = order.get("raw")
    return raw if isinstance(raw, dict) else order


def _sticker_display(order: dict[str, Any]) -> str:
    part_a = clean_text(order.get("sticker_part_a"))
    part_b = clean_text(order.get("sticker_part_b"))
    if part_a or part_b:
        return f"{part_a} {part_b}".strip()
    number = clean_text(order.get("sticker_number"))
    return number or str(_order_id(order))


def _extract_wb_sgtin_meta(meta_order: dict[str, Any] | None) -> dict[str, Any]:
    result = {
        "available": False,
        "values": [],
        "decision": "",
        "decision_value": None,
    }
    if not meta_order:
        return result
    meta = meta_order.get("meta") if isinstance(meta_order.get("meta"), dict) else {}
    sgtin = meta.get("sgtin") if isinstance(meta, dict) else None
    if isinstance(sgtin, dict):
        value = sgtin.get("value")
        if isinstance(value, list):
            result["values"] = [str(x) for x in value]
        elif value:
            result["values"] = [str(value)]
        result["available"] = True
    details = meta_order.get("metaDetails")
    if isinstance(details, list):
        for detail in details:
            if isinstance(detail, dict) and str(detail.get("key") or "").lower() == "sgtin":
                result["available"] = True
                result["decision"] = clean_text(detail.get("decision"))
                result["decision_value"] = detail.get("value")
                break
    return result


def wb_decision_label(decision: str) -> tuple[str, str]:
    key = clean_text(decision)
    labels = {
        "required": ("КИЗ требуется", "required"),
        "filled": ("КИЗ закреплен, проверка пока не проводится", "filled"),
        "pending": ("Проверяется WB", "pending"),
        "valid": ("КИЗ принят WB", "valid"),
        "sgtinMaySell": ("КИЗ принят WB", "valid"),
    }
    if key in labels:
        return labels[key]
    if not key:
        return ("Статус WB не получен", "unknown")
    return (key, "error")


def build_marking_requirements(
    orders: list[dict[str, Any]],
    catalog: Any,
    assignments: dict[int, dict[str, Any]] | None = None,
    wb_meta: dict[int, dict[str, Any]] | None = None,
    free_codes: dict[str, int] | None = None,
    pending_codes: dict[str, int] | None = None,
) -> dict[str, Any]:
    """Build marking demand and the per-order working queue."""
    assignments = assignments or {}
    wb_meta = wb_meta or {}
    free_codes = free_codes or {}
    pending_codes = pending_codes or {}
    grouped: dict[str, dict[str, Any]] = {}
    missing_gtin: dict[tuple[str, str], dict[str, Any]] = {}
    unresolved: Counter[str] = Counter()
    non_marked_orders = 0
    marking_orders = 0
    marking_profile_counts: Counter[str] = Counter()
    order_rows: list[dict[str, Any]] = []

    for order in orders:
        article = seller_article_from_order(order)
        product = catalog.find_by_article(article) if article else None
        order_id = _order_id(order)
        if not product:
            unresolved[article or "—"] += 1
            continue

        if not is_marking_required(product.get("kiz_required")):
            non_marked_orders += 1
            continue

        marking_orders += 1
        profile = clean_text(product.get("marking_profile")).upper() or "CUSTOM"
        marking_profile_counts[profile] += 1
        gtin = normalize_gtin(product.get("gtin"))
        gtin14 = canonical_gtin14(gtin)
        reason = validate_gtin(gtin)
        name = clean_text(product.get("display_name")) or article
        assignment = assignments.get(order_id)
        meta_info = _extract_wb_sgtin_meta(wb_meta.get(order_id))
        raw_order = _order_raw(order)
        advertised_meta = {
            str(x).lower() for x in (raw_order.get("requiredMeta") or []) + (raw_order.get("optionalMeta") or [])
        }
        wb_available = bool(meta_info["available"] or "sgtin" in advertised_meta)
        decision_label, decision_class = wb_decision_label(str(meta_info["decision"] or ""))

        order_rows.append(
            {
                "order_id": order_id,
                "seller_article": article,
                "name": name,
                "gtin": gtin,
                "gtin14": gtin14,
                "gtin_error": reason or "",
                "marking_profile": profile,
                "sticker_display": _sticker_display(order),
                "assignment": assignment,
                "assignment_display": display_marking_code(str((assignment or {}).get("raw_code") or "")),
                "wb_meta_available": wb_available,
                "wb_values": meta_info["values"],
                "wb_decision": meta_info["decision"],
                "wb_decision_label": decision_label,
                "wb_decision_class": decision_class,
            }
        )

        if reason:
            key = (article, reason)
            row = missing_gtin.setdefault(
                key,
                {
                    "seller_article": article,
                    "name": name,
                    "gtin": gtin,
                    "reason": reason,
                    "count": 0,
                },
            )
            row["count"] += 1
            continue

        row = grouped.setdefault(
            gtin14,
            {
                "gtin": gtin14,
                "count": 0,
                "articles": set(),
                "names": set(),
                "profiles": set(),
                "assigned": 0,
            },
        )
        row["count"] += 1
        row["articles"].add(article)
        row["names"].add(name)
        row["profiles"].add(profile)
        if assignment:
            row["assigned"] += 1

    rows: list[dict[str, Any]] = []
    for gtin, row in grouped.items():
        names = sorted(row["names"])
        articles = sorted(row["articles"])
        need = int(row["count"])
        assigned = int(row["assigned"])
        free = max(0, int(free_codes.get(gtin, 0)))
        pending = max(0, int(pending_codes.get(gtin, 0)))
        remaining = max(0, need - assigned)
        to_order = max(0, remaining - free - pending)
        rows.append(
            {
                "gtin": gtin,
                "count": need,
                "assigned": assigned,
                "free": free,
                "pending": pending,
                "remaining": remaining,
                "to_order": to_order,
                "name": names[0] if len(names) == 1 else "; ".join(names),
                "articles": articles,
                "articles_text": ", ".join(articles),
                "profiles": sorted(row["profiles"]),
                "profile_label": ", ".join(
                    {
                        "PERFUMERY": "Парфюмерия",
                        "CHEMISTRY": "Дезодоранты и косметика",
                        "CUSTOM": "Другие",
                        "AUTO": "Другие",
                    }.get(value, value.title())
                    for value in sorted(row["profiles"])
                ),
            }
        )
    rows.sort(key=lambda item: (-int(item["count"]), item["name"].lower(), item["gtin"]))
    order_rows.sort(key=lambda item: (bool(item["assignment"]), item["name"].lower(), item["order_id"]))

    missing = sorted(
        missing_gtin.values(),
        key=lambda item: (-int(item["count"]), item["seller_article"], item["reason"]),
    )
    unresolved_rows = [
        {"seller_article": article, "count": count, "reason": "Артикул не найден в каталоге товаров"}
        for article, count in sorted(unresolved.items(), key=lambda pair: (-pair[1], pair[0]))
    ]

    ready_orders = sum(int(row["count"]) for row in rows)
    free_codes_total = sum(min(int(row["free"]), int(row["remaining"])) for row in rows)
    pending_codes_total = sum(min(int(row["pending"]), max(0, int(row["remaining"]) - int(row["free"]))) for row in rows)
    to_order_total = sum(int(row["to_order"]) for row in rows)
    assigned_orders = sum(1 for row in order_rows if row["assignment"])
    sent_wb_orders = sum(
        1 for row in order_rows if str((row["assignment"] or {}).get("status") or "") in {"sent_wb", "accepted_wb"}
    )
    missing_orders = sum(int(row["count"]) for row in missing)
    unresolved_orders = sum(unresolved.values())

    profile_labels = {
        "PERFUMERY": "Парфюмерия",
        "CHEMISTRY": "Дезодоранты и косметика",
        "CUSTOM": "Другие маркируемые товары",
        "AUTO": "Другие маркируемые товары",
    }
    category_counts = [
        {"key": key.lower(), "label": profile_labels.get(key, key.title()), "count": int(count)}
        for key, count in marking_profile_counts.most_common()
        if int(count) > 0
    ]

    warnings: list[str] = []
    if missing_orders:
        warnings.append("Для части маркируемых товаров не заполнен или некорректен GTIN.")
    if unresolved_orders:
        warnings.append("Для части заказов не найден артикул в каталоге товаров, поэтому необходимость маркировки определить нельзя.")

    return {
        "total_orders": len(orders),
        "marking_orders": marking_orders,
        "ready_orders": ready_orders,
        "assigned_orders": assigned_orders,
        "sent_wb_orders": sent_wb_orders,
        "non_marked_orders": non_marked_orders,
        "missing_orders": missing_orders,
        "unresolved_orders": unresolved_orders,
        "gtin_count": len(rows),
        "free_codes_total": free_codes_total,
        "pending_codes_total": pending_codes_total,
        "to_order_total": to_order_total,
        "category_counts": category_counts,
        "rows": rows,
        "orders": order_rows,
        "missing": missing,
        "unresolved": unresolved_rows,
        "warnings": warnings,
        "can_order": marking_orders > 0 and missing_orders == 0 and unresolved_orders == 0 and to_order_total > 0,
    }
