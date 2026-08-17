"""Sanity checks applied before any LLM-extracted data enters the dataset.

The pipeline is fully automated (no human review gate), so these checks are
the safety net: reject anything that is not a well-formed, plausible,
forward-looking deadline.
"""

import logging
import re
from datetime import date, datetime, timedelta, timezone

from .extract import ConfCycle

log = logging.getLogger(__name__)

DATE_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d")

# Anywhere on Earth is UTC-12: the last zone in which a given date is still
# that date. Also the fallback for a timezone string we cannot read, because
# it is what these CFPs overwhelmingly mean when they say nothing at all.
AOE_OFFSET_HOURS = -12

_TZ_RE = re.compile(r"UTC([+-]\d{1,2})(?::(\d{2}))?", re.IGNORECASE)


def tz_offset_hours(tz: str) -> float:
    """Hours from UTC for a venue's stated timezone.

    Deliberately mirrors tzOffsetHours() in docs/app.js. The site and the
    reminder mail must not disagree about when a deadline is -- the mail is
    what sends someone to the site to look.
    """
    if not tz or tz.strip().lower() == "aoe":
        return AOE_OFFSET_HOURS
    m = _TZ_RE.fullmatch(tz.strip())
    if not m:
        log.warning("unrecognised timezone %r; treating it as AoE", tz)
        return AOE_OFFSET_HOURS
    hours = int(m.group(1))
    minutes = int(m.group(2) or 0)
    return hours + (minutes / 60 if hours >= 0 else -minutes / 60)


def to_utc(when: datetime, tz: str) -> datetime:
    """A venue's wall clock as the instant it actually happens.

    Everything that compares a deadline against the present has to go through
    here. Comparing the two naively errs early for a zone behind us -- AoE
    and UTC-7 both are -- and errs *late* for CET, JST or China time, which
    is the one direction a deadline tracker must never be wrong in.
    """
    return when.replace(tzinfo=timezone.utc) - timedelta(hours=tz_offset_hours(tz))


def _parse(value: str) -> datetime | None:
    """A deadline as a naive wall clock in the venue's own timezone.

    A bare date means the end of that day rather than the start of it, which
    is both what a CFP means by it and what docs/app.js already assumes.
    Reading it as midnight would put a deadline a whole day early.
    """
    raw = str(value).strip()
    for fmt in DATE_FORMATS:
        try:
            when = datetime.strptime(raw, fmt)
        except ValueError:
            continue
        if fmt == "%Y-%m-%d":
            when = when.replace(hour=23, minute=59, second=59)
        return when
    return None


def validate_cycle(cycle: ConfCycle) -> tuple[bool, str]:
    """Return (ok, reason)."""
    today = date.today()

    if not cycle.timeline:
        return False, "empty timeline"

    if not (today.year - 1 <= cycle.year <= today.year + 2):
        return False, f"implausible conference year {cycle.year}"

    horizon_lo = datetime.now() - timedelta(days=400)
    horizon_hi = datetime.now() + timedelta(days=730)

    for entry in cycle.timeline:
        if entry.deadline is None and entry.abstract_deadline is None:
            return False, "timeline entry with no dates"
        for label, value in (
            ("deadline", entry.deadline),
            ("abstract_deadline", entry.abstract_deadline),
        ):
            if value is None:
                continue
            parsed = _parse(value)
            if parsed is None:
                return False, f"unparseable {label}: {value!r}"
            if not (horizon_lo <= parsed <= horizon_hi):
                return False, f"{label} {value!r} outside plausible window"

    return True, "ok"
