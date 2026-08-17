"""Send deadline reminders to subscribers.

Reads the published docs/data.json (not the YAML: data.json is what the site
shows, and it already has recurring venues expanded onto the calendar), works
out which deadlines have crossed one of a subscriber's lead times since the
last run, and mails each subscriber one digest of everything now due.

    python -m scraper.remind --stdout    # show what would go out
    python -m scraper.remind             # send it

Every (deadline, lead time) pair is recorded once sent, so running this more
often than daily is harmless and a missed day is caught up rather than
skipped -- a deadline that first appears three days out still produces a
reminder instead of silently falling through the 14- and 7-day marks.
"""

import argparse
import json
import logging
import math
import sys
from datetime import datetime, timedelta

from . import config, mail, subscriptions, validate

log = logging.getLogger(__name__)

# Sent-key bookkeeping is pruned once the deadline is this far past; keeping
# it briefly means a deadline that slips back and forth across midnight does
# not re-send.
PRUNE_AFTER_DAYS = 30


def _parse(value: str) -> datetime | None:
    for fmt in validate.DATE_FORMATS:
        try:
            return datetime.strptime(str(value).strip(), fmt)
        except ValueError:
            continue
    return None


def load_data() -> dict:
    return json.loads(config.SITE_DATA.read_text())


def events(data: dict, now: datetime | None = None) -> list[dict]:
    """Every still-future deadline on the site, soonest first.

    Abstract and paper dates become separate events: they are different
    things to be reminded about, and on most venues missing the abstract
    registration means the paper cannot be submitted at all.

    Times are compared naively, in the venue's own stated timezone. For an
    AoE deadline that makes the reminder up to half a day early and never
    late, which is the right direction to be wrong in.
    """
    now = now or datetime.now()
    out = []
    for conf in data.get("conferences") or []:
        key = subscriptions.venue_key(conf.get("title", ""))
        for cycle in conf.get("confs") or []:
            for entry in cycle.get("timeline") or []:
                for field, kind in (
                    ("abstract_deadline", "abstract"),
                    ("deadline", "paper"),
                ):
                    when = _parse(entry.get(field) or "")
                    if when is None or when <= now:
                        continue
                    out.append(
                        {
                            "conf": conf,
                            "key": key,
                            "title": conf.get("title", key),
                            "year": cycle.get("year"),
                            "link": cycle.get("link"),
                            "tz": cycle.get("timezone") or "AoE",
                            "track": entry.get("track"),
                            "comment": entry.get("comment") or "",
                            "kind": kind,
                            "raw": str(entry.get(field)),
                            "when": when,
                        }
                    )
    out.sort(key=lambda e: e["when"])
    return out


def event_key(ev: dict) -> str:
    return "|".join(
        [
            ev["key"],
            str(ev.get("year") or ""),
            ev.get("track") or "-",
            ev["kind"],
            ev["raw"],
        ]
    )


def _days_out(ev: dict, now: datetime) -> int:
    """Whole days until the deadline, rounded up: 23h away is 'in 1 day'."""
    return max(0, math.ceil((ev["when"] - now).total_seconds() / 86400))


def prune(sub: dict, now: datetime) -> None:
    """Forget bookkeeping for deadlines that are well past."""
    cutoff = now - timedelta(days=PRUNE_AFTER_DAYS)
    kept = {}
    for key, sent_on in (sub.get("sent") or {}).items():
        raw = key.rsplit("@", 1)[0].split("|")[-1]
        when = _parse(raw)
        if when is None or when >= cutoff:
            kept[key] = sent_on
    sub["sent"] = kept


def due_for(sub: dict, evs: list[dict], now: datetime) -> list[dict]:
    """Deadlines this subscriber should hear about right now.

    A lead time fires once. When several fire at the same moment -- a
    deadline that only appeared on the site two days before it closes has
    crossed the 14-, 7- and 3-day marks at once -- one reminder goes out and
    all of them are marked, so the reader gets a single "in 2 days", not
    three copies of the same news.
    """
    leads = sorted(set(sub.get("lead_days") or config.DEFAULT_LEAD_DAYS), reverse=True)
    sent = sub.setdefault("sent", {})
    want_tracks = sub.get("tracks") == "all"
    due = []

    for ev in evs:
        if ev.get("track") and not want_tracks:
            continue
        if not subscriptions.matches(sub, ev["conf"]):
            continue
        days = _days_out(ev, now)
        base = event_key(ev)
        crossed = [L for L in leads if days <= L and f"{base}@{L}" not in sent]
        if not crossed:
            continue
        due.append({**ev, "days": days, "marks": [f"{base}@{L}" for L in crossed]})
    return due


# ---------- rendering ----------

def _line(ev: dict) -> list[str]:
    when = ev["when"].strftime("%Y-%m-%d %H:%M")
    label = ev["kind"] if not ev.get("track") else f"{ev['track'].lower()} {ev['kind']}"
    head = f"  in {ev['days']:>3} day(s)   {ev['title']} {ev.get('year') or ''}".rstrip()
    lines = [head, f"      {label} deadline -- {when} {ev['tz']}"]
    if ev["comment"]:
        lines.append(f"      {ev['comment']}")
    if ev.get("link"):
        lines.append(f"      {ev['link']}")
    lines.append("")
    return lines


def render(sub: dict, due: list[dict], cat: list[dict]) -> tuple[str, str]:
    due = sorted(due, key=lambda e: e["when"])
    first = due[0]
    if len(due) == 1:
        what = f"{first['track'].lower()} " if first.get("track") else ""
        subject = (
            f"[ConfTracker] {first['title']} {what}{first['kind']} deadline "
            f"in {first['days']} day(s)"
        )
    else:
        subject = (
            f"[ConfTracker] {len(due)} deadlines coming up -- "
            f"{first['title']} in {first['days']} day(s)"
        )

    body = ["Deadlines you asked to be reminded about:", ""]
    for ev in due:
        body += _line(ev)
    body += [
        "Dates are as published by the venue; verify on the official site",
        "before submitting.",
        "",
        f"Full tracker: {config.SITE_URL}",
        "",
        "-- ",
        f"You subscribed {sub.get('email')} to: "
        + (
            ", ".join(subscriptions.describe(s, cat) for s in sub.get("selectors") or [])
            or "nothing"
        ),
        # Points at whichever signup channel is actually running -- telling
        # someone to reply to an address nobody reads is worse than nothing.
        "To stop, send 'unsubscribe': " + subscriptions.channel_hint(),
        "'pause' holds the mail without losing your selection, and 'list'",
        "shows what you are signed up for.",
    ]
    return subject, "\n".join(body)


# ---------- driving ----------

def run(dry_run: bool = False, only: str | None = None) -> int:
    """Mail every subscriber their due reminders. Returns the number sent."""
    now = datetime.now()
    data = load_data()
    evs = events(data, now)
    cat = subscriptions.catalog(data)
    store = subscriptions.load()
    sent_count = 0

    for email, sub in sorted(store.get("subscribers", {}).items()):
        if only and email != subscriptions.normalize_email(only):
            continue
        if sub.get("paused"):
            log.info("%s: paused, skipping", email)
            continue

        prune(sub, now)
        due = due_for(sub, evs, now)
        if not due:
            log.info("%s: nothing due", email)
            continue

        subject, body = render(sub, due, cat)
        if dry_run:
            print(f"--- to: {email}")
            print(f"Subject: {subject}")
            print()
            print(body)
            print()
            continue

        try:
            mail.send(subject, body, to=email, reply_to=config.SUBSCRIBE_ADDRESS)
        except Exception as e:
            # One bad address must not stop everyone else's mail, and nothing
            # is marked sent, so the next run retries it.
            log.error("%s: could not send reminder: %s", email, e)
            continue

        for ev in due:
            for mark in ev["marks"]:
                sub["sent"][mark] = now.date().isoformat()
        sent_count += 1
        log.info("%s: reminded about %d deadline(s)", email, len(due))

        # Record now, not once the whole run is over. Everything mailed so
        # far is marked only in memory until this lands, so a crash here used
        # to mean the entire run's digests going out again tomorrow.
        try:
            subscriptions.save(store)
        except OSError as e:
            # And stop, rather than keep mailing people whose reminders we
            # cannot record either. One person seeing a digest twice is a
            # bug; every remaining subscriber seeing one every day until
            # somebody notices the disk is full is the thing worth avoiding.
            log.error(
                "could not record reminders as sent (%s); stopping this run "
                "before mailing anyone we cannot record", e,
            )
            break

    return sent_count


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    ap = argparse.ArgumentParser(description="Send ConfTracker deadline reminders")
    ap.add_argument(
        "--stdout",
        "--dry-run",
        dest="dry_run",
        action="store_true",
        help="print the mail that would go out, send nothing, record nothing",
    )
    ap.add_argument("--only", help="restrict to one subscriber address")
    args = ap.parse_args()

    try:
        n = run(dry_run=args.dry_run, only=args.only)
    except FileNotFoundError as e:
        log.error("Missing input: %s", e)
        return 1
    except RuntimeError as e:
        log.error("%s", e)
        return 1
    if not args.dry_run:
        log.info("Sent %d reminder mail(s)", n)
    return 0


if __name__ == "__main__":
    sys.exit(main())
