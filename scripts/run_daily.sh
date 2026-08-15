#!/usr/bin/env bash
# Daily ConfTracker pipeline: sync -> scrape -> build -> commit -> deploy.
# Intended to run from cron on the lab machine, e.g.:
#   0 7 * * *  /path/to/ConfTracker/scripts/run_daily.sh >> /path/to/ConfTracker/logs/daily.log 2>&1
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
mkdir -p logs

# Load ANTHROPIC_API_KEY (and any overrides) from .env if present.
if [[ -f .env ]]; then
  set -a
  source .env
  set +a
fi

source .venv/bin/activate

echo "=== ConfTracker daily run: $(date -u +%FT%TZ) ==="
python -m scraper.main

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
