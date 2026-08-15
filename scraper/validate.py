"""Sanity checks applied before any LLM-extracted data enters the dataset.

The pipeline is fully automated (no human review gate), so these checks are
the safety net: reject anything that is not a well-formed, plausible,
forward-looking deadline.
"""

import logging
from datetime import date, datetime, timedelta

from .extract import ConfCycle

log = logging.getLogger(__name__)

DATE_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d")


def _parse(value: str) -> datetime | None:
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(value.strip(), fmt)
        except ValueError:
            continue
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
