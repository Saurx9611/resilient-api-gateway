"""Cron parsing and explicit local-wall-clock DST resolution."""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import CroniterBadDateError, croniter

UTC = timezone.utc


def validate_schedule(cron_expression: str, timezone_name: str) -> tuple[str, ZoneInfo]:
    if not isinstance(cron_expression, str) or len(cron_expression) > 128 or len(cron_expression.split()) != 5:
        raise ValueError("cron_expression must be a five-field cron expression")
    if not croniter.is_valid(cron_expression, strict=True):
        raise ValueError("cron_expression is invalid or impossible")
    if not isinstance(timezone_name, str) or not 1 <= len(timezone_name) <= 64:
        raise ValueError("timezone must be an IANA timezone name")
    try:
        zone = ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError("timezone must be an available IANA timezone") from None
    return cron_expression, zone


def _valid_instants(wall_time: datetime, zone: ZoneInfo) -> list[datetime]:
    """Return UTC instants represented by a local wall time (0, 1, or 2)."""
    instants: set[datetime] = set()
    for fold in (0, 1):
        local = wall_time.replace(tzinfo=zone, fold=fold)
        instant = local.astimezone(UTC)
        if instant.astimezone(zone).replace(tzinfo=None) == wall_time:
            instants.add(instant)
    return sorted(instants)


def next_occurrence(cron_expression: str, timezone_name: str, after: datetime) -> datetime:
    """Return next cron wall-time as UTC; skip DST gaps, choose first fold instant."""
    _, zone = validate_schedule(cron_expression, timezone_name)
    if after.tzinfo is None or after.utcoffset() is None:
        raise ValueError("after must be timezone-aware")
    after_utc = after.astimezone(UTC)
    wall_base = after_utc.astimezone(zone).replace(tzinfo=None)
    # strict is supported by the validator; the iterator itself takes no strict kwarg.
    iterator = croniter(cron_expression, wall_base)
    for _ in range(1000):
        try:
            candidate = iterator.get_next(datetime)
        except CroniterBadDateError as exc:
            raise ValueError("cron expression has no occurrence in the supported search horizon") from exc
        instants = _valid_instants(candidate, zone)
        # No instant means the wall time falls in the spring-forward gap.
        # An ambiguous fall-back wall time resolves to the first UTC instant.
        for instant in instants:
            if instant > after_utc:
                return instant
    raise ValueError("cron expression has no resolvable occurrence in the supported search horizon")
