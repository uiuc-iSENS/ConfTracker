"""Daily pipeline entry point.

    python -m scraper.main

Steps:
  1. For every conference: fetch its official page (requests, Playwright
     fallback for JS sites), extract the upcoming CFP cycle with Claude
     (schema-validated structured output, one-hop link following), and
     merge it after sanity checks.
  2. Rebuild site/data.json for the static frontend.

Run from cron on the lab machine; run_daily.sh wraps this with git commit
and deployment.
"""

import argparse
import json
import logging
import sys
from collections import Counter
from datetime import date, datetime, timezone

import yaml

from . import build, config, extract, fetch, validate

log = logging.getLogger("scraper")


def _carry_forward_tracks(title: str, previous: dict, entry: dict) -> int:
    """Keep track deadlines we already knew but did not re-find this run.

    Matched per track, on (track, name), rather than all-or-nothing. Workshop
    dates are the fragile ones: they come from pages that are frequently
    unreachable, and from the deep pass, which does not run every day. Losing
    them on an ordinary run would make workshops flicker on and off the site
    between Sundays.

    A date the run *did* find always wins -- that is how an extended deadline
    replaces the old one instead of being shadowed by it.
    """
    def dated(t: dict) -> bool:
        return bool(t.get("deadline") or t.get("abstract_deadline"))

    def still_open(t: dict) -> bool:
        # Nothing is gained by carrying a closed deadline forward, and a row
        # that no run will ever re-find -- a workshop since renamed, a generic
        # row superseded by named ones -- would otherwise be carried for good.
        dates = [
            validate._parse(str(t[f]))
            for f in ("deadline", "abstract_deadline")
            if t.get(f)
        ]
        dates = [d for d in dates if d]
        return bool(dates) and max(dates) >= datetime.now()

    found = {(t.get("track"), t.get("comment")) for t in entry["timeline"] if dated(t)}
    carried = [
        t
        for t in previous.get("timeline") or []
        if t.get("track")
        and dated(t)
        and still_open(t)
        and (t.get("track"), t.get("comment")) not in found
    ]
    if carried:
        log.info("%s: carrying %d known track deadline(s) forward", title, len(carried))
        entry["timeline"].extend(carried)
    return len(carried)


def _merge_cycle(conf: dict, cycle: extract.ConfCycle) -> None:
    """Insert/replace the cycle for its year in the conference's confs list."""
    entry = {
        "year": cycle.year,
        "id": f"{conf['title'].lower()}{cycle.year % 100:02d}",
        "link": cycle.link or conf.get("isens", {}).get("scrape", {}).get("url"),
        "timeline": [
            {
                k: v
                for k, v in {
                    "abstract_deadline": t.abstract_deadline,
                    "deadline": t.deadline,
                    "comment": t.comment,
                    "track": t.track,
                    # Kept so the site can link a workshop row to its own CFP,
                    # and so a later run can re-check that page directly.
                    "url": t.url,
                }.items()
                if v is not None
            }
            for t in cycle.timeline
        ],
        "timezone": cycle.timezone or "AoE",
        "date": cycle.date or "TBD",
        "place": cycle.place or "TBD",
    }

    previous = next(
        (c for c in conf.get("confs") or [] if c.get("year") == cycle.year), None
    )
    if previous:
        _carry_forward_tracks(conf["title"], previous, entry)

    confs = [c for c in conf.get("confs") or [] if c.get("year") != cycle.year]
    confs.append(entry)
    conf["confs"] = sorted(confs, key=lambda c: c["year"])


def _candidate_urls(conf: dict) -> list[str]:
    """Starting URLs to try, in order: the configured landing page, then
    year-pattern templates for next year and this year (many series index
    pages are stale, but per-year sites follow a predictable URL scheme)."""
    scrape = (conf.get("isens") or {}).get("scrape", {})
    urls = []
    if scrape.get("url"):
        urls.append(scrape["url"])
    year = date.today().year
    for template in scrape.get("templates") or []:
        for y in (year + 1, year):
            candidate = template.format(year=y)
            if candidate not in urls:
                urls.append(candidate)
    return urls


def _try_url(
    title: str, url: str, visited: set[str]
) -> tuple[extract.Extraction | None, str]:
    """Extract from one starting URL, following links up to 2 hops.

    Returns (result, reason). The reason distinguishes "the site was
    unreachable" from "the extractor failed" from "we got an answer", so the
    weekly health report can tell a broken scrape apart from a quiet CFP.
    """
    text = fetch.fetch_page_text(url)
    if not text:
        return None, "fetch_failed"
    result = extract.extract_cycle(title, url, text)
    for _ in range(2):
        if (
            result is None
            or result.found
            or not result.follow_url
            or not result.follow_url.startswith(("http://", "https://"))
            or result.follow_url in visited
        ):
            break
        visited.add(result.follow_url)
        log.info("%s: following %s", title, result.follow_url)
        follow_text = fetch.fetch_page_text(result.follow_url)
        if not follow_text:
            break
        result = extract.extract_cycle(title, result.follow_url, follow_text)
    return result, ("ok" if result is not None else "extract_failed")


def _augment_tracks(title: str, result: extract.Extraction, visited: set[str]) -> int:
    """Follow a workshops/posters listing page and merge its own deadlines.

    Individual workshops at one conference routinely close weeks apart, and
    those dates almost never appear on the main CFP page -- they live on a
    "Workshops" listing of their own. Merging them keeps each workshop's
    deadline attached to its parent venue instead of losing all of them.

    Only entries carrying a track are taken: the listing page's view of the
    main paper deadline is second-hand, and the main CFP already gave us it.
    """
    url = result.tracks_url
    if (
        not url
        or not url.startswith(("http://", "https://"))
        or url in visited
        or result.cycle is None
    ):
        return 0
    visited.add(url)
    log.info("%s: reading track listing %s", title, url)

    text = fetch.fetch_page_text(url)
    if not text:
        return 0
    listing = extract.extract_cycle(title, url, text)
    if listing is None or not listing.found or listing.cycle is None:
        return 0

    known = {
        (t.track, t.comment, t.deadline, t.abstract_deadline)
        for t in result.cycle.timeline
    }
    added = 0
    for entry in listing.cycle.timeline:
        if not entry.track:
            continue
        if not (entry.deadline or entry.abstract_deadline):
            continue
        key = (entry.track, entry.comment, entry.deadline, entry.abstract_deadline)
        if key in known:
            continue
        known.add(key)
        result.cycle.timeline.append(entry)
        added += 1
    if added:
        log.info("%s: +%d track deadline(s) from listing", title, added)
    return added


def _cycle_deadline_from_page(
    label: str, url: str, visited: set[str]
) -> extract.TimelineEntry | None:
    """Extract one submission deadline from a page that is about one thing.

    Used for a workshop's own site and for a manually pinned extra source.
    On such a page the deadline we want is the page's *main* deadline -- it
    is the workshop's own call for papers -- so the main-track entry wins,
    falling back to the earliest dated entry when everything is labelled.
    """
    if not url or not url.startswith(("http://", "https://")) or url in visited:
        return None
    visited.add(url)

    text = fetch.fetch_page_text(url)
    if not text:
        log.info("%s: could not fetch %s", label, url)
        return None
    result = extract.extract_cycle(label, url, text)
    if result is None or not result.found or result.cycle is None:
        return None
    if result.confidence == "low":
        log.info("%s: low confidence on %s, not using it", label, url)
        return None

    dated = [t for t in result.cycle.timeline if t.deadline or t.abstract_deadline]
    if not dated:
        return None
    return next((t for t in dated if not t.track), dated[0])


def _follow_track_sites(title: str, result: extract.Extraction, visited: set[str]) -> int:
    """Fill in workshops that advertise their deadline only on their own site.

    A parent venue's workshop listing usually gives a name and a link but no
    date -- each workshop runs its own CFP on its own page. Without this the
    listing yields entries with no deadline, which are dropped, and the
    workshop is invisible even though we knew its name and its URL.

    Bounded: one fetch per workshop still missing a date, up to
    MAX_TRACK_FOLLOWS of them, and never for a workshop the listing already
    dated.
    """
    if result.cycle is None:
        return 0
    pending = [
        t
        for t in result.cycle.timeline
        if t.track and t.url and not (t.deadline or t.abstract_deadline)
    ]
    if not pending:
        return 0
    if len(pending) > config.MAX_TRACK_FOLLOWS:
        log.info(
            "%s: %d undated tracks link out, following the first %d",
            title,
            len(pending),
            config.MAX_TRACK_FOLLOWS,
        )

    filled = 0
    for entry in pending[: config.MAX_TRACK_FOLLOWS]:
        name = entry.comment or entry.track
        found = _cycle_deadline_from_page(f"{name} ({title})", entry.url, visited)
        if found is None:
            continue
        entry.abstract_deadline = found.abstract_deadline
        entry.deadline = found.deadline
        filled += 1
        log.info("%s: %s -> %s", title, name, entry.deadline or entry.abstract_deadline)
    return filled


def _merge_extra_sources(conf: dict, result: extract.Extraction, visited: set[str]) -> int:
    """Scrape workshops pinned by hand in the venue's YAML.

    The automatic path only reaches a workshop the parent venue links to. One
    that exists solely on its own site, with no mention on the parent, cannot
    be discovered -- so it can be named explicitly instead. See README.
    """
    sources = (conf.get("isens") or {}).get("extra_sources") or []
    if not sources or result.cycle is None:
        return 0

    title = conf.get("title", "?")
    known = {(t.track, t.comment) for t in result.cycle.timeline}
    added = 0
    for src in sources:
        url = (src or {}).get("url")
        if not url:
            continue
        name = src.get("name")
        track = src.get("track") or "Workshop"
        found = _cycle_deadline_from_page(f"{name or url} ({title})", url, visited)
        if found is None:
            continue
        # The source's own name wins over whatever the page called itself:
        # it is what the YAML author wanted this row labelled.
        comment = name or found.comment
        if (track, comment) in known:
            continue
        known.add((track, comment))
        result.cycle.timeline.append(
            extract.TimelineEntry(
                abstract_deadline=found.abstract_deadline,
                deadline=found.deadline,
                comment=comment,
                track=track,
                url=url,
            )
        )
        added += 1
        log.info("%s: pinned source %s -> %s", title, comment, found.deadline)
    return added


def scrape_all(deep: bool = False) -> list[dict]:
    """Scrape + extract every configured conference.

    `deep` turns on the per-workshop pass: following each linked workshop to
    its own site is by far the most expensive thing this pipeline does (one
    fetch and one extraction per workshop, where the rest of a venue costs
    two or three in total), and workshop dates do not move day to day. The
    daily run therefore leaves it off and a weekly run turns it on; dates
    found by a deep run are carried forward in between.

    Returns one status record per conference, which main() appends to the run
    history for the weekly health report.
    """
    records = []
    for path in sorted(config.CONF_DIR.glob("*.yml")):
        stem = path.stem
        conf = yaml.safe_load(path.read_text())[0]
        candidates = _candidate_urls(conf)
        if not candidates:
            log.info("%s: no scrape URL configured; skipping", stem)
            records.append({"conf": stem, "status": "no_url", "urls_tried": 0})
            continue

        result = None
        reasons = []
        visited: set[str] = set()
        for url in candidates:
            if url in visited:
                continue
            visited.add(url)
            log.info("%s: trying %s", stem, url)
            result, reason = _try_url(conf["title"], url, visited)
            reasons.append(reason)
            if result is not None and result.found:
                break

        if result is None or not result.found or result.cycle is None:
            # A site we never reached, or an extractor that never answered, is
            # a broken pipeline; a site that simply has no CFP up yet is not.
            if reasons and all(r == "fetch_failed" for r in reasons):
                status = "fetch_failed"
                log.warning("%s: every URL unreachable (%d tried)", stem, len(visited))
            elif reasons and all(r != "ok" for r in reasons):
                status = "extract_failed"
                log.warning("%s: extraction failed on every URL", stem)
            else:
                status = "no_cfp"
                log.info("%s: no upcoming CFP found (%d URLs tried)", stem, len(visited))
            records.append({"conf": stem, "status": status, "urls_tried": len(visited)})
            continue
        if result.confidence == "low":
            log.warning(
                "%s: extraction confidence low, not merging (notes: %s)",
                stem,
                result.notes,
            )
            records.append(
                {
                    "conf": stem,
                    "status": "low_confidence",
                    "urls_tried": len(visited),
                    "notes": result.notes,
                }
            )
            continue

        # Individual workshop deadlines live on their own listing page; pull
        # them in before validating so they get the same checks. Then chase
        # the workshops whose dates are not even on that listing, and finally
        # any source pinned by hand in the YAML.
        tracks_added = _augment_tracks(conf["title"], result, visited)
        tracks_filled = (
            _follow_track_sites(conf["title"], result, visited) if deep else 0
        )
        # Pinned sources stay on the daily run: there are a handful of them
        # at most, and they are the venues nothing else can find.
        extra_added = _merge_extra_sources(conf, result, visited)

        # Drop dateless entries (e.g. an announced-but-unscheduled round)
        # instead of rejecting the whole cycle because of them.
        result.cycle.timeline = [
            t for t in result.cycle.timeline if t.deadline or t.abstract_deadline
        ]

        valid, reason = validate.validate_cycle(result.cycle)
        if not valid:
            log.warning("%s: extraction rejected by validation: %s", stem, reason)
            records.append(
                {
                    "conf": stem,
                    "status": "rejected",
                    "urls_tried": len(visited),
                    "notes": reason,
                }
            )
            continue

        _merge_cycle(conf, result.cycle)
        path.write_text(yaml.safe_dump([conf], sort_keys=False, allow_unicode=True))
        log.info("%s: updated (confidence=%s)", stem, result.confidence)
        records.append(
            {
                "conf": stem,
                "status": "updated",
                "urls_tried": len(visited),
                "confidence": result.confidence,
                "year": result.cycle.year,
                "tracks_added": tracks_added,
                "tracks_filled": tracks_filled,
                "extra_added": extra_added,
            }
        )
    return records


def _append_run_history(started: datetime, records: list[dict], deep: bool = False) -> None:
    """Append this run's outcome for the weekly health report to consume."""
    finished = datetime.now(timezone.utc)
    entry = {
        "started_at": started.isoformat(timespec="seconds"),
        "finished_at": finished.isoformat(timespec="seconds"),
        "duration_s": round((finished - started).total_seconds()),
        "backend": extract.active_backend(),
        "deep": deep,
        "results": records,
    }
    try:
        config.RUN_HISTORY.parent.mkdir(parents=True, exist_ok=True)
        with config.RUN_HISTORY.open("a") as fh:
            fh.write(json.dumps(entry) + "\n")
    except OSError as e:  # a missing history file must never fail the run
        log.warning("Could not append run history: %s", e)


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    ap = argparse.ArgumentParser(description="Scrape every conference and rebuild the site")
    ap.add_argument(
        "--deep",
        action="store_true",
        help="also follow each linked workshop to its own site for its "
             "deadline (expensive; intended for a weekly run)",
    )
    args = ap.parse_args()
    started = datetime.now(timezone.utc)

    log.info("Step 1/2: scraping all conferences%s", " (deep)" if args.deep else "")
    records = scrape_all(deep=args.deep)
    counts = Counter(r["status"] for r in records)
    log.info(
        "Scrape results: %s",
        ", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "nothing configured",
    )

    log.info("Step 2/2: building site data")
    n = build.build()
    log.info("Wrote %s with %d conferences", config.SITE_DATA, n)

    _append_run_history(started, records, deep=args.deep)
    return 0


if __name__ == "__main__":
    sys.exit(main())
