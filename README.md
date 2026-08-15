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
                              one link when deadlines live on a subpage)
  validate.py              3. reject anything failing sanity checks
  build.py                 4. write docs/data.json
        │
        ▼  git push
docs/                      static frontend (no build step) — served by
                           GitHub Pages straight from the main branch
```

Guardrails for the fully automated updates: schema-validated LLM output,
date-plausibility checks, low-confidence extractions dropped, and every data
change is a git commit (rollback = `git revert`).

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

Cron (daily at 7:00 UTC):

```
0 7 * * * /path/to/ConfTracker/scripts/run_daily.sh >> /path/to/ConfTracker/logs/daily.log 2>&1
```

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
- Check `logs/daily.log` occasionally: venues repeatedly logging
  "could not fetch" need their `scrape.url` updated.
- Extraction backend is auto-selected: `ANTHROPIC_API_KEY` set → Anthropic
  SDK; otherwise the `claude` CLI (subscription login) if installed. Force
  with `CONFTRACKER_BACKEND=api|cli`. Model overrides: `CONFTRACKER_MODEL`
  (API backend, default `claude-opus-5`) / `CONFTRACKER_CLI_MODEL`
  (CLI backend, defaults to your Claude Code default model).
