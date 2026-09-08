from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def parse_wb_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    value = str(value).strip()
    if not value:
        return None
    try:
        if value.endswith('Z'):
            return datetime.fromisoformat(value.replace('Z', '+00:00'))
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def format_dt(value: str | None) -> str:
    dt = parse_wb_datetime(value)
    if not dt:
        return value or ''
    local = dt.astimezone()
    return local.strftime('%d.%m.%Y %H:%M')


def age_minutes_from_created_at(created_at: str | None) -> int | None:
    dt = parse_wb_datetime(created_at)
    if not dt:
        return None
    now = datetime.now(timezone.utc)
    return max(0, int((now - dt.astimezone(timezone.utc)).total_seconds() // 60))


def format_age(minutes: int | None) -> str:
    if minutes is None:
        return '—'
    hours = minutes // 60
    mins = minutes % 60
    if hours <= 0:
        return f'{mins}мин'
    return f'{hours}ч{mins:02d}мин'


def timer_class(minutes: int | None) -> str:
    if minutes is None:
        return 'timer-unknown'
    if minutes < 13 * 60:
        return 'timer-green'
    if minutes < 15 * 60:
        return 'timer-yellow'
    return 'timer-red'


def first_present(supply: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        value = supply.get(key)
        if value not in (None, ''):
            return value
    return None


def supply_status(supply: dict[str, Any]) -> dict[str, str]:
    """Map raw WB supply fields to simple FBE workflow statuses.

    User-facing interpretation for FBE Pack:
    - If WB already has scan/transfer/close date -> received by WB, purple.
    - If supply is not done and has no scan date -> still assembling, blue.
    - If it is done/closed but WB scan is not visible -> waiting for handover, yellow.

    WB field names are not always consistently populated across list/detail methods,
    so we check several common variants defensively.
    """
    received_marker = first_present(
        supply,
        (
            'scanDt', 'scannedAt', 'scanDate', 'scanTime',
            'deliveredAt', 'acceptedAt', 'transferredAt',
        ),
    )
    if received_marker:
        return {'label': 'Получена WB', 'class': 'status-transferred'}

    if not supply.get('done'):
        return {'label': 'На сборке', 'class': 'status-assembling'}

    return {'label': 'Ждет отгрузки', 'class': 'status-waiting'}


def sort_supplies_recent(supplies: list[dict[str, Any]], limit: int = 5) -> list[dict[str, Any]]:
    def key(s: dict[str, Any]):
        dt = parse_wb_datetime(s.get('createdAt'))
        if dt:
            return dt
        # Some WB list responses may be sparse; fall back to any later status date.
        for field in ('scanDt', 'closedAt', 'createdAt'):
            dt = parse_wb_datetime(s.get(field))
            if dt:
                return dt
        return datetime.min.replace(tzinfo=timezone.utc)

    result = sorted(supplies, key=key, reverse=True)
    return result[:limit]
