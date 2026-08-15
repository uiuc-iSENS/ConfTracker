"""Build site/data.json from the per-conference YAML files."""

import json
import logging
from datetime import date, datetime, timedelta, timezone

import yaml

from . import config

log = logging.getLogger(__name__)

# How far ahead recurring deadlines are projected: a little over a year, so
# one full cycle of a quarterly venue is always visible.
RECURRING_HORIZON_DAYS = 400


def _expand_recurring(conf: dict) -> None:
    """Project a venue's fixed annual deadlines onto the calendar.

    Some venues do not run a per-edition call for papers at all. IMWUT (whose
    accepted papers are presented at UbiComp/ISWC) takes submissions on the
    same dates every year, published by the journal rather than by any
    conference site -- so there is nothing for the scraper to find, and the
    venue would sit in "Awaiting CFP" forever despite having the most
    predictable deadlines of any venue we track. Declaring them in the YAML
    and projecting them here fixes that without pretending they were scraped.

    Generated entries go into data.json only; the YAML keeps the rule, not
    its expansion, so the dates roll forward on their own every year.
    """
    rec = (conf.get("isens") or {}).get("recurring")
    if not rec:
        return

    today = date.today()
    horizon = today + timedelta(days=RECURRING_HORIZON_DAYS)
    tz = rec.get("timezone") or "AoE"

    by_year: dict[int, list[dict]] = {}
    for spec in rec.get("deadlines") or []:
        # Key is "date", not "on": YAML 1.1 reads a bare `on:` as boolean true.
        try:
            month, day = (int(x) for x in str(spec["date"]).split("-"))
        except (KeyError, ValueError):
            log.warning("%s: unparseable recurring date %r", conf.get("title"), spec)
            continue
        for year in (today.year, today.year + 1, today.year + 2):
            try:
                when = date(year, month, day)
            except ValueError:  # e.g. Feb 29 in a non-leap year
                continue
            if not (today <= when <= horizon):
                continue
            entry = {"deadline": f"{when.isoformat()} 23:59:59"}
            if spec.get("track"):
                entry["track"] = spec["track"]
            if spec.get("comment"):
                entry["comment"] = spec["comment"]
            by_year.setdefault(year, []).append(entry)

    cycles = conf.get("confs") or []
    for year, entries in sorted(by_year.items()):
        cycle = next((c for c in cycles if c.get("year") == year), None)
        if cycle is None:
            cycle = {
                "year": year,
                "id": f"{conf['title'].lower().replace('/', '-')}{year % 100:02d}",
                "link": rec.get("source"),
                "timeline": [],
                "timezone": tz,
                "date": "TBD",
                "place": "TBD",
            }
            cycles.append(cycle)
        known = {
            (t.get("deadline"), t.get("track")) for t in cycle.get("timeline") or []
        }
        cycle["timeline"] = (cycle.get("timeline") or []) + [
            e for e in entries if (e["deadline"], e.get("track")) not in known
        ]
    conf["confs"] = sorted(cycles, key=lambda c: c["year"])


def build(conf_dir=None, out_path=None) -> int:
    # Resolved here, not in the signature: default arguments bind at import
    # time, so a caller overriding config.SITE_DATA would silently write to
    # the original path while the log reported the new one.
    conf_dir = conf_dir or config.CONF_DIR
    out_path = out_path or config.SITE_DATA

    conferences = []
    for path in sorted(conf_dir.glob("*.yml")):
        conf = yaml.safe_load(path.read_text())[0]
        _expand_recurring(conf)
        conferences.append(conf)

    data = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "conferences": conferences,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    return len(conferences)
