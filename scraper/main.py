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

import logging
import sys
from datetime import date

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


def _try_url(title: str, url: str, visited: set[str]) -> extract.Extraction | None:
    """Extract from one starting URL, following links up to 2 hops."""
    text = fetch.fetch_page_text(url)
    if not text:
        return None
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
    return result


def scrape_all() -> tuple[int, int]:
    """Scrape + extract every configured conference. Returns (ok, skipped)."""
    ok = skipped = 0
    for path in sorted(config.CONF_DIR.glob("*.yml")):
        stem = path.stem
        conf = yaml.safe_load(path.read_text())[0]
        candidates = _candidate_urls(conf)
        if not candidates:
            log.info("%s: no scrape URL configured; skipping", stem)
            skipped += 1
            continue

        result = None
        visited: set[str] = set()
        for url in candidates:
            if url in visited:
                continue
            visited.add(url)
            log.info("%s: trying %s", stem, url)
            result = _try_url(conf["title"], url, visited)
            if result is not None and result.found:
                break

        if result is None or not result.found or result.cycle is None:
            log.info("%s: no upcoming CFP found (%d URLs tried)", stem, len(visited))
            skipped += 1
            continue
        if result.confidence == "low":
            log.warning(
                "%s: extraction confidence low, not merging (notes: %s)",
                stem,
                result.notes,
            )
            skipped += 1
            continue

        # Drop dateless entries (e.g. an announced-but-unscheduled round)
        # instead of rejecting the whole cycle because of them.
        result.cycle.timeline = [
            t for t in result.cycle.timeline if t.deadline or t.abstract_deadline
        ]

        valid, reason = validate.validate_cycle(result.cycle)
        if not valid:
            log.warning("%s: extraction rejected by validation: %s", stem, reason)
            skipped += 1
            continue

        _merge_cycle(conf, result.cycle)
        path.write_text(yaml.safe_dump([conf], sort_keys=False, allow_unicode=True))
        log.info("%s: updated (confidence=%s)", stem, result.confidence)
        ok += 1
    return ok, skipped


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )

    log.info("Step 1/2: scraping all conferences")
    ok, skipped = scrape_all()
    log.info("Scrape results: %d updated, %d without new data", ok, skipped)

    log.info("Step 2/2: building site data")
    n = build.build()
    log.info("Wrote %s with %d conferences", config.SITE_DATA, n)
    return 0


if __name__ == "__main__":
    sys.exit(main())
