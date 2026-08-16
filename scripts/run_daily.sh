#!/usr/bin/env bash
# Daily ConfTracker pipeline: sync -> scrape -> build -> commit -> deploy.
# Intended to run from cron on the lab machine, e.g.:
#   0 7 * * *  /path/to/ConfTracker/scripts/run_daily.sh >> /path/to/ConfTracker/logs/daily.log 2>&1
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
mkdir -p logs

# cron's PATH is bare (/usr/bin:/bin), which hides the `claude` CLI installed
# under ~/.local/bin -- without this the extractor silently falls back to the
# API backend and every extraction fails on a missing key.
export PATH="$HOME/.local/bin:$PATH"

# Only one run at a time: a slow scrape must not overlap the next day's.
exec 9>logs/.daily.lock
flock -n 9 || { echo "Another run is in progress; exiting."; exit 0; }

# Load ANTHROPIC_API_KEY (and any overrides) from .env if present.
if [[ -f .env ]]; then
  set -a
  source .env
  set +a
fi

source .venv/bin/activate

echo "=== ConfTracker daily run: $(date -u +%FT%TZ) ==="

# Sync before scraping. Without this a conference YAML edited on GitHub never
# reaches the lab machine, and worse, the branches diverge: every subsequent
# push fails and the site quietly stops updating while the scrape keeps
# reporting success. --autostash covers a half-finished local edit.
if git remote get-url origin &>/dev/null; then
  git pull --rebase --autostash origin main \
    || echo "warning: pull failed; continuing with the local checkout"
fi

# Arguments pass through to the scraper, so the weekly cron entry can add
# --deep (follow every linked workshop to its own site) without a second
# script that would drift out of sync with this one.
python -m scraper.main "$@"

# Commit data changes for an audit trail (rollback = git revert).
if [[ -n "$(git status --porcelain data docs/data.json)" ]]; then
  git add data docs/data.json
  git commit -m "data: daily refresh $(date -u +%F)"
  echo "Committed data refresh."
else
  echo "No data changes today."
fi

# Publish: GitHub Pages serves docs/ from the main branch, so a push is
# the whole deployment.
if git remote get-url origin &>/dev/null; then
  git push origin HEAD
  echo "Pushed; GitHub Pages will redeploy shortly."
else
  echo "No git remote configured yet; skipping push."
fi

echo "=== Done: $(date -u +%FT%TZ) ==="
