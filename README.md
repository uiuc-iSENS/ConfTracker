# iSENS ConfTracker

Conference deadline tracker for the [iSENS lab](https://isens.cs.illinois.edu/)
(Wireless, Sensing, and Embedded Networked Systems, UIUC). A daily pipeline
gathers deadlines for the venues we care about — including
[each workshop's own](#finding-workshop-deadlines) — and publishes a static
site with live countdowns, a 12-month deadline timeline, and tier/tag
filters. Lab members can [sign up](#email-reminders) for any venue, tier or
topic and get an email before each deadline.

## How it works

```
data/conferences/*.yml     one YAML file per conference (venue info + an
                           `isens:` block for lab tier, tags, scrape URL)
        │
        ▼  nightly cron (scripts/run_daily.sh; Sundays with --deep)
scraper/                   Python pipeline, for every conference:
  fetch.py                 1. fetch the official page (requests, Playwright
                              fallback for JS-rendered sites)
  extract.py               2. extract the upcoming CFP cycle with Claude
                              (schema-validated structured output; follows
                              one link when deadlines live on a subpage, and
                              one more to the workshop/poster listing)
  main.py                  3. chase each listed workshop to its own site for
                              its own deadline (--deep, weekly)
  validate.py              4. reject anything failing sanity checks
  build.py                 5. write docs/data.json
        │
        ▼  git push
docs/                      static frontend (no build step) — served by
                           GitHub Pages straight from the main branch

logs/history.jsonl         one record per run, read by
  scraper/health.py        the weekly health report (scripts/weekly_report.sh)

scraper/issues.py          signup issues -> subscriptions (or inbox.py, from
  scraper/remind.py        a mailbox), and subscriptions -> reminder mail
                           (scripts/run_reminders.sh; see Email reminders)
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

## Email reminders

People can subscribe to the deadlines they care about and get mail before
each one — by default 14, 7, 3 and 1 days ahead, abstract registration
deadlines included as events of their own.

The site is static files on GitHub Pages, so there is no server to POST a
signup form to. Instead a **remind me** button opens a prefilled message
whose *title is the command*, and the lab machine picks it up from there:

```
                click "remind me"
docs/           ─────────────────►  a prefilled GitHub issue
  app.js                              title: subscribe mobicom
                                      body:  Email: alice@illinois.edu
                                                │  (subscriber hits Submit)
                                                ▼  cron, every 30 min
scraper/issues.py    reads it with the `gh` CLI, applies the command,
                     comments with the result, closes the issue
        │
        ▼            private/subscribers.json   (gitignored: real addresses,
        │                                         public repo)
        ▼  cron, once a day
scraper/remind.py    reads docs/data.json, works out which deadlines crossed
                     a lead time since the last run, mails one digest each
```

The button cannot file the issue by itself — a static page would need an
embedded GitHub token to do that, readable by anyone who views source — so
the subscriber fills in their address and presses Submit. Three clicks, no
account for us to run, and the one credential involved (`gh`, already logged
in on the lab machine) never leaves it.

`scripts/subscribe.sh` does the same thing from the command line, for adding
someone by hand or seeing who is on the list.

### The commands

The same vocabulary whichever channel is in use — it is an issue title here,
an email subject line there:

```
subscribe mobicom sensys      one or more venues
subscribe all                 every venue tracked
subscribe tier1               everything at a lab tier
subscribe tag:sensing         everything with a topic tag
subscribe all tracks:all      include workshop/poster/demo calls
subscribe all days:30,7,1     choose your own lead times
unsubscribe mobicom           drop one selection
unsubscribe                   remove the address entirely
pause / resume                keep the selection, hold the mail
list                          reply with the current subscription
help                          reply with this syntax
```

Unrecognised words are never guessed at — they come back to the sender as
ignored, along with the list of venues that can be named. Venue names are
matched loosely: any unambiguous prefix works, so `ubicomp` finds
UbiComp/ISWC.

### The signup repo must be private

A signup issue contains someone's email address. On a public repo that
address is published permanently — editing it out afterwards does not help,
because issue edit history is world-readable too. `scripts/preflight.sh`
fails outright if the configured repo is not private.

Create it once, then give the lab team Read access (enough to open issues):

```bash
gh repo create uiuc-iSENS/ConfTracker-signups --private \
  --description "Deadline reminder signups for ConfTracker"
gh label create reminder-signup --repo uiuc-iSENS/ConfTracker-signups \
  --description "A ConfTracker reminder subscription request" --color 0E8A16
```

The label matters more than it looks: GitHub silently drops an unknown label
from an `issues/new?labels=…` URL, so without it every signup would be filed
and then never found by the poller. Preflight checks for it.

Worth being explicit about one thing this channel does *not* do: an issue
proves the author owns a GitHub account, not that they own the address they
typed in. The domain allow-list, a private lab-only repo, and unsubscribe
instructions in every reminder are what stand in for that. The mailbox
channel below is stronger on exactly this point — a request arriving *from*
an address is proof the sender owns it — which is the trade you are making.

### Setup

Signups are limited to `illinois.edu` addresses out of the box. Set
`CONFTRACKER_SUBSCRIBE_DOMAINS` to a comma-separated list to change that, or
to an empty string to open signups to anyone. In `.env`:

```bash
CONFTRACKER_SIGNUP_REPO=uiuc-iSENS/ConfTracker-signups   # private, see above
CONFTRACKER_SIGNUP_LABEL=reminder-signup
CONFTRACKER_SUBSCRIBE_DOMAINS=illinois.edu
```

Then rebuild the site data so the buttons appear, and check it:

```bash
.venv/bin/python -m scraper.build     # writes the channel into docs/data.json
scripts/preflight.sh                  # repo private? label present? gh authed?
.venv/bin/python -m scraper.issues --stdout   # dry run against real issues
```

Changing the channel only changes `docs/data.json`, so a rebuild is required
— otherwise the site keeps pointing wherever it pointed before.

To add or inspect subscriptions by hand at any time:

```bash
scripts/subscribe.sh --list
scripts/subscribe.sh alice@illinois.edu subscribe tier1 tracks:all
scripts/subscribe.sh alice@illinois.edu unsubscribe
```

### Signing up by email instead

Setting `CONFTRACKER_IMAP_USER` turns the buttons into `mailto:` links and
`scraper/inbox.py` reads that mailbox over IMAP, applying the same commands
and replying with what it did. The command goes in the subject line, with the
body read as a fallback, so replying `unsubscribe` to a reminder works.

Both channels can run at once — they share the store and the vocabulary — and
the reply text follows whichever one is configured, so nobody is ever told to
mail an address that is not being read.

For Gmail this needs an app password, which Google only issues with 2FA
enabled on the account. That is the reason the GitHub channel is the default
here, and the trade is real: an email arriving *from* an address proves the
sender owns it, which an issue cannot.

### Which address the mail comes from

Two supported setups, and they cannot be mixed:

- **Campus relay** (default). Accepts mail from the lab machine's IP without
  credentials — nothing to store, nothing to renew — but the sender has to
  stay `@illinois.edu` or SPF fails. Reminders carry `Reply-To:` the signup
  mailbox so replies still reach a mailbox that is read.
- **The group Gmail.** Reminders and health reports go out *as*
  `smartlab.uiuc@gmail.com`, DKIM-signed by Google, and replies land in the
  same mailbox the signups come from — no `Reply-To` indirection. Uses the
  same app password as IMAP, over port 587 (465 is not supported):

  ```bash
  CONFTRACKER_REPORT_FROM=smartlab.uiuc@gmail.com
  CONFTRACKER_SMTP_HOST=smtp.gmail.com
  CONFTRACKER_SMTP_PORT=587
  CONFTRACKER_SMTP_USER=smartlab.uiuc@gmail.com
  CONFTRACKER_SMTP_PASSWORD=<the same app password>
  ```

What does *not* work is putting a `gmail.com` sender on mail pushed through
the campus relay: SPF and DKIM both fail to align for `gmail.com`, and the
mail gets filtered. `scripts/preflight.sh` checks for exactly that mismatch,
and logs in with the app password rather than merely opening a connection —
a revoked app password otherwise looks identical to a working one until the
first reminder silently fails to send.

Gmail's consumer sending limit is ~500 recipients/day, far above one digest
per subscriber; it matters only if signups are ever opened to the public.

Cron:

```
*/30 * * * *  /path/to/ConfTracker/scripts/run_reminders.sh --inbox-only >> /path/to/ConfTracker/logs/reminders.log 2>&1
30 13 * * *   /path/to/ConfTracker/scripts/run_reminders.sh              >> /path/to/ConfTracker/logs/reminders.log 2>&1
```

Frequent mailbox polls make signups feel instant; the reminder pass runs once
a day so each subscriber gets one digest rather than a trickle. Both are safe
to run more often — every (deadline, lead time) pair is recorded once sent,
so nothing is mailed twice, and a missed day is caught up rather than skipped.
A deadline that first appears three days before it closes still produces a
reminder instead of falling silently through the 14- and 7-day marks.

Preview without sending, replying, or marking anything read:

```bash
scripts/run_reminders.sh --stdout
```

Notes:

- The configured channel is what puts the reminder UI on the site: `build.py`
  writes it into `docs/data.json`, and the frontend hides the buttons
  entirely when neither channel is set. Whatever is written there is
  **public** — a repo name is harmless, a mailbox address is a choice (use
  the lab's, not a personal one).
- `private/subscribers.json` is the only copy of the subscriber list and is
  deliberately outside git (`.gitignore`). Back it up with the rest of the
  machine; `CONFTRACKER_SUBSCRIBERS` moves it elsewhere.
- Auto-replies, mailing-list traffic and anything with `Auto-Submitted:` set
  are ignored rather than parsed — that is what stops a bounce of our own
  acknowledgement from looping.
- Reminders compare naive timestamps in the venue's stated timezone. For an
  AoE deadline that makes the mail up to half a day early and never late.

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

Then configure and schedule it — see [Unattended operation](#unattended-operation)
below:

```bash
cp .env.example .env && chmod 600 .env   # optional; empty .env is valid
scripts/install_cron.sh --apply          # schedule everything
scripts/preflight.sh                     # verify it can actually run
```

## Finding workshop deadlines

A workshop is where a lot of lab work actually goes, and its deadline is the
hardest kind to find: it is set by the workshop's own organisers, announced
on the workshop's own site, and usually weeks away from the parent
conference's dates. Three layers, each catching what the one before missed:

1. **The parent's CFP page.** Whatever the main call already states —
   "Poster/Demo: Aug 31" — comes back as a timeline entry with a `track`.
2. **The workshop listing.** The extractor is asked for a link to the venue's
   workshops page and follows it once, returning one entry per workshop with
   its own name in `comment`. This is where most venues put them.
3. **Each workshop's own site.** Listings very often give a name and a link
   and *no date* — the date lives on the workshop's own page. Any entry that
   comes back with a link but no deadline is fetched and extracted directly.

Layer 3 is by far the most expensive thing the pipeline does — one fetch and
one extraction *per workshop*, where the rest of a venue costs two or three
in total — so it does not run daily. `--deep` turns it on, and cron does that
once a week:

```bash
python -m scraper.main          # daily: layers 1 and 2
python -m scraper.main --deep   # weekly: all three
```

It is bounded further at `CONFTRACKER_MAX_TRACK_FOLLOWS` (default 8) fetches
per venue, and never re-fetches a workshop the listing already dated, so a
venue with forty workshops cannot become forty extractions.

Between deep runs the dates found by the last one are carried forward, per
track, matched on (track, name) — so workshops do not flicker on and off the
site between Sundays. A date a run *does* find always wins over the carried
one, which is how an extended deadline replaces the old one instead of being
shadowed by it. The same mechanism covers a listing page that is simply
unreachable on a given day. Workshops still undated after all three layers
are dropped rather than shown blank.

Where a workshop's own page was found, the site links its row to it.

### Workshops the crawler cannot reach

The layers above all start from the parent venue. A workshop that exists
only on its own site, with no mention anywhere on the conference's pages,
cannot be discovered — so name it explicitly:

```yaml
  isens:
    extra_sources:
      - url: https://hotwireless.example.org/2027/
        name: HotWireless        # what the row is called on the site
        track: Workshop          # optional, defaults to Workshop
```

Each is scraped like a small venue and merged as a sub-row under the parent,
with `name` winning over whatever the page calls itself. Use it for the long
tail, not as the normal path — anything the conference links to is found on
its own, and a pinned URL goes stale when the workshop moves to next year's
domain.

## Unattended operation

The point of ConfTracker is that nobody has to remember it exists. Four jobs
run from the lab machine's crontab; a fifth keeps the logs from growing
forever.

```bash
scripts/install_cron.sh          # show the block, change nothing
scripts/install_cron.sh --apply  # install it (backs up the old crontab first)
scripts/install_cron.sh --remove # take it out again
```

The entries sit between marker lines, so re-running `--apply` replaces the
block instead of stacking a second copy of every job.

| When (machine local time) | What | Log |
|---|---|---|
| 02:00 Mon–Sat | scrape every venue, rebuild `docs/data.json`, commit, push | `logs/daily.log` |
| 02:00 Sundays | the same, plus `--deep` (each workshop's own site) | `logs/daily.log` |
| every 30 min | read the signup mailbox, apply and acknowledge commands | `logs/reminders.log` |
| 08:15 daily | mail each subscriber the deadlines now due | `logs/reminders.log` |
| 03:00 Mondays | health report: is any of this still working? | `logs/weekly.log` |
| 03:30 Sundays | rotate logs (8 weeks, compressed) | `logs/logrotate.log` |

**Cron fires in local time, not UTC.** This machine runs `America/Chicago`,
so 02:00 in the crontab is 07:00 UTC. The health report's footer quotes UTC;
the two differ by the current offset, which is the usual source of "why did
it run an hour early in November".

### Verifying it

```bash
scripts/preflight.sh              # every precondition, checked, changes nothing
scripts/preflight.sh --send-test  # ...and put one test mail through the relay
```

Preflight checks the things that fail silently at 02:00: that the venv
imports, that a Claude backend is actually reachable from the PATH the cron
scripts build, that `git push` can authenticate (a scrape that succeeds and
a push that cannot is invisible from the outside — the site simply stops
changing), that `flock` is genuinely exclusive on this NFS mount, that the
relay and signup mailbox answer, that `docs/data.json` is fresh, and that the
crontab entries exist at all. Exit status is non-zero if anything failed, so
it drops straight into a monitoring check if you ever want one.

Run it after setup, after a reboot, and whenever the weekly report looks odd.

### How it fails, and how you find out

Each job is deliberately fault-tolerant — one unreachable conference site
cannot take down a run — which means a badly broken run and a healthy one
look identical from outside. Everything below is therefore reported rather
than assumed:

- **The scrape stops working.** The Monday health report lists conferences
  that have gone `STALE_RUNS` runs without usable data, separating "site
  unreachable" from "no CFP posted yet".
- **The push stops working.** The report counts unpushed commits and flags a
  stale `docs/data.json`; `run_daily.sh` now pulls with `--rebase --autostash`
  first, so an edit made on GitHub does not leave the branches diverged
  forever.
- **Reminders stop going out.** The report shows the subscriber count and the
  date of the last reminder, and warns if nothing has gone out in 14 days
  despite having subscribers.
- **Everything stops.** The reports themselves stop arriving. That is the
  signal that the machine or its cron is down — which is why the report is
  mailed rather than written to a file.

Two jobs take a `flock` before doing anything (`logs/.daily.lock`,
`logs/.reminders.lock`), so a slow run is skipped rather than overlapped.

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
- Workshops, posters and demos are tracked individually — see
  [Finding workshop deadlines](#finding-workshop-deadlines) below.
- Check `logs/daily.log` occasionally: venues repeatedly logging
  "could not fetch" need their `scrape.url` updated.
- Extraction backend is auto-selected: `ANTHROPIC_API_KEY` set → Anthropic
  SDK; otherwise the `claude` CLI (subscription login) if installed. Force
  with `CONFTRACKER_BACKEND=api|cli`. Model overrides: `CONFTRACKER_MODEL`
  (API backend, default `claude-opus-5`) / `CONFTRACKER_CLI_MODEL`
  (CLI backend, defaults to your Claude Code default model).
