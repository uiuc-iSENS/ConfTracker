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

import json
import logging
import sys
from collections import Counter
from datetime import date, datetime, timezone

import yaml

from . import build, config, extract, fetch, validate

log = logging.getLogger("scraper")


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
                }.items()
                if v is not None
            }
            for t in cycle.timeline
        ],
        "timezone": cycle.timezone or "AoE",
        "date": cycle.date or "TBD",
        "place": cycle.place or "TBD",
    }
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


def scrape_all() -> list[dict]:
    """Scrape + extract every configured conference.

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
            }
        )
    return records


def _append_run_history(started: datetime, records: list[dict]) -> None:
    """Append this run's outcome for the weekly health report to consume."""
    finished = datetime.now(timezone.utc)
    entry = {
        "started_at": started.isoformat(timespec="seconds"),
        "finished_at": finished.isoformat(timespec="seconds"),
        "duration_s": round((finished - started).total_seconds()),
        "backend": extract.active_backend(),
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
    started = datetime.now(timezone.utc)

    log.info("Step 1/2: scraping all conferences")
    records = scrape_all()
    counts = Counter(r["status"] for r in records)
    log.info(
        "Scrape results: %s",
        ", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "nothing configured",
    )

    log.info("Step 2/2: building site data")
    n = build.build()
    log.info("Wrote %s with %d conferences", config.SITE_DATA, n)

    _append_run_history(started, records)
    return 0


if __name__ == "__main__":
    sys.exit(main())
