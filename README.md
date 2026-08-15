# iSENS ConfTracker

Conference deadline tracker for the [iSENS lab](https://isens.cs.illinois.edu/)
(Wireless, Sensing, and Embedded Networked Systems, UIUC). A daily pipeline
gathers deadlines for the venues we care about and publishes a static site
with live countdowns, a 12-month deadline timeline, and tier/tag filters.

## How it works

```
data/conferences/*.yml     one YAML file per conference (venue info + an
                           `isens:` block for lab tier, tags, scrape URL)
        │
        ▼  daily cron (scripts/run_daily.sh)
scraper/                   Python pipeline, for every conference:
  fetch.py                 1. fetch the official page (requests, Playwright
                              fallback for JS-rendered sites)
  extract.py               2. extract the upcoming CFP cycle with Claude
                              (schema-validated structured output; follows
                              one link when deadlines live on a subpage, and
                              one more to the workshop/poster listing, whose
                              deadlines differ per workshop)
  validate.py              3. reject anything failing sanity checks
  build.py                 4. write docs/data.json
        │
        ▼  git push
docs/                      static frontend (no build step) — served by
                           GitHub Pages straight from the main branch

logs/history.jsonl         one record per daily run, read by
  scraper/health.py        the weekly health report (scripts/weekly_report.sh)
```

Guardrails for the fully automated updates: schema-validated LLM output,
date-plausibility checks, low-confidence extractions dropped, and every data
change is a git commit (rollback = `git revert`).

## Weekly health report

Every stage of the daily run is fault-tolerant — a redesigned conference
site, an extractor that returns nothing usable, a push that never lands are
all logged and stepped over, so one bad venue can never take down the run.
The flip side is that a badly broken run and a healthy one look identical
from the outside. So `scripts/weekly_report.sh` mails a summary each Monday:
per-day run counts, conferences that have stopped producing usable data
(separating "site unreachable" from "no CFP posted yet"), unpushed commits or
a stale `docs/data.json`, and a digest of deadlines in the next 45 days. If
the reports themselves stop arriving, the machine or its cron is down.

```bash
scripts/weekly_report.sh --stdout    # preview without sending
```

Mail goes out through the university outbound relay, which accepts mail from
the lab machine's campus IP unauthenticated — no credentials stored, no MTA
installed, nothing to renew. Recipient and relay are overridable with
`CONFTRACKER_REPORT_TO`, `CONFTRACKER_SMTP_HOST`, and `CONFTRACKER_SMTP_PORT`
(off campus, point those at an authenticated relay).

## Setup (lab machine)

```bash
git clone <this repo> && cd ConfTracker
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium          # optional, for JS-rendered sites

# --- Claude auth: pick ONE ---
# Option A: API key
echo 'ANTHROPIC_API_KEY=sk-ant-...' > .env   # not committed (.gitignore)
# Option B: Claude subscription (no API key). Install Claude Code
# (https://claude.com/claude-code), run `claude` once to log in; the
# pipeline then uses headless `claude -p` automatically.

python -m scraper.main               # test run
python -m http.server -d docs 8000   # preview at http://localhost:8000
```

Cron — the daily scrape, plus the Monday health report:

```
MAILTO=""
0 7 * * *  /path/to/ConfTracker/scripts/run_daily.sh    >> /path/to/ConfTracker/logs/daily.log 2>&1
0 8 * * 1  /path/to/ConfTracker/scripts/weekly_report.sh >> /path/to/ConfTracker/logs/weekly.log 2>&1
```

Both scripts prepend `~/.local/bin` to `PATH` themselves: cron's default
`PATH` does not include it, and without `claude` on `PATH` the extractor
silently falls back to the API backend and every extraction fails on a
missing key.

## Deployment (GitHub Pages)

1. Create a GitHub repository and push:

   ```bash
   git remote add origin git@github.com:<org>/ConfTracker.git
   git push -u origin main
   ```

2. Repo **Settings → Pages** → Source: *Deploy from a branch* →
   Branch `main`, folder `/docs`.
3. The site appears at `https://<org>.github.io/ConfTracker/`
   (custom domain optional under the same settings page).

After that, deployment is just the daily cron's `git push` — Pages
redeploys automatically within a minute. The lab machine needs push
access to the repo (an SSH deploy key with write access, or `gh auth login`).

## Adding / editing conferences

Add a file under `data/conferences/`, e.g. `data/conferences/example.yml`:

```yaml
- title: EXAMPLE
  description: Example Conference on Interesting Systems
  sub: NW
  rank:
    ccf: B        # kept for reference; the site displays CORE
    core: A
  dblp: example
  confs: []        # filled automatically
  isens:
    tier: 2        # 1 = core venues, 2 = relevant, 3 = peripheral (site grouping)
    tags: [sensing, mobile]
    scrape:
      url: https://example-conf.org/
      templates:                     # optional; {year} is expanded to
        - https://example-conf.org/{year}/   # next year, then this year
```

### Venues with fixed recurring deadlines

Some venues have no per-edition CFP to scrape. UbiComp/ISWC is the example:
its technical track is papers published in IMWUT, which takes submissions on
the same dates every year, announced by the journal rather than by any
conference site. Declare the rule instead of scraping it:

```yaml
  isens:
    recurring:
      source: https://dl.acm.org/journal/imwut/how-to-submit
      timezone: AoE
      deadlines:
        - date: '02-01'          # MM-DD; quote it — YAML reads `on:` as true
          comment: IMWUT new submissions
        - date: '08-01'
          track: Resubmission    # a track makes it a secondary row, not the
          comment: Major revisions only   # headline paper deadline
```

`build.py` projects these onto the calendar (~13 months ahead) each run, so
the dates roll forward on their own and the YAML never needs touching. The
expansion lands in `docs/data.json` only — the YAML keeps the rule, not its
output. A `track:` marks a date that is not a normal submission deadline;
IMWUT's August 1 date takes major-revision resubmissions only, and listing
it as a plain deadline would tell people to submit new papers on a day the
site rejects them.

Notes:

- Point `scrape.url` at a stable series landing page (e.g.
  `sigmobile.org/mobicom/`) — the extractor follows links (up to 2 hops) to
  the current edition's site or CFP subpage on its own. Many series index
  pages are stale, so also add a `templates` entry when per-year sites
  follow a predictable URL scheme; those are tried when the landing page
  yields nothing. For venues without any stable homepage, a WikiCFP search
  URL works: `http://www.wikicfp.com/cfp/servlet/tool.search?q=NAME&year=f`.
- `confs:` (the deadline history) is machine-maintained; manual edits are
  fine and survive until the scraper finds a newer cycle for that year.
- Workshops, posters and demos are tracked individually: the extractor is
  asked for a link to the venue's workshop listing and follows it once,
  returning one timeline entry per workshop with its own deadline and the
  workshop's name in `comment`. The site renders those as sub-rows under
  the parent venue. If that listing page is unreachable on a given day, the
  previously-known track deadlines are carried forward rather than
  disappearing until the page comes back.
- Check `logs/daily.log` occasionally: venues repeatedly logging
  "could not fetch" need their `scrape.url` updated.
- Extraction backend is auto-selected: `ANTHROPIC_API_KEY` set → Anthropic
  SDK; otherwise the `claude` CLI (subscription login) if installed. Force
  with `CONFTRACKER_BACKEND=api|cli`. Model overrides: `CONFTRACKER_MODEL`
  (API backend, default `claude-opus-5`) / `CONFTRACKER_CLI_MODEL`
  (CLI backend, defaults to your Claude Code default model).
