from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo


CAIRO = ZoneInfo("Africa/Cairo")
UTC = timezone.utc

# The single canonical rule for a KBC business date, recorded on every row so
# the derivation can be audited without re-reading this module.
BUSINESS_DATE_RULE = "AWARE_SCHEDULED_START_TO_AFRICA_CAIRO_DATE"


def require_aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timezone-aware datetime required")
    return value


def cairo_day_utc_window(target_date: date) -> tuple[datetime, datetime]:
    start = datetime.combine(target_date, time.min, tzinfo=CAIRO)
    end = datetime.combine(target_date + timedelta(days=1), time.min, tzinfo=CAIRO)
    return start.astimezone(UTC), end.astimezone(UTC)


def cairo_business_date(value: datetime) -> date:
    """
    THE canonical business date: a timezone-aware instant converted to
    Africa/Cairo, then .date().

    Never derive it from a UTC date directly, from a naive datetime, or by
    slicing a raw Graph string: Cairo is UTC+2/+3, so the UTC date and the
    Cairo date differ for every instant from 21:00/22:00 UTC onwards.
    """
    return require_aware(value).astimezone(CAIRO).date()


def business_date_trace(value: datetime) -> dict[str, str]:
    """Auditable derivation of one business date, for run diagnostics."""
    aware = require_aware(value)
    local = aware.astimezone(CAIRO)
    return {
        "rule": BUSINESS_DATE_RULE,
        "aware_scheduled_start_utc": aware.astimezone(UTC).isoformat(),
        "cairo_datetime": local.isoformat(),
        "cairo_utc_offset": local.strftime("%z"),
        "session_date": local.date().isoformat(),
    }


def parse_graph_datetime(value: str, timezone_name: str | None = None) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is not None:
        return parsed.astimezone(UTC)
    zones = {"UTC": UTC, "Etc/UTC": UTC, "Africa/Cairo": CAIRO, "Egypt Standard Time": CAIRO}
    zone = zones.get(timezone_name or "")
    if zone is None:
        raise ValueError(f"unsupported naive Graph timezone: {timezone_name or '<missing>'}")
    return parsed.replace(tzinfo=zone).astimezone(UTC)
