#!/usr/bin/env bash
# Weekly ConfTracker health report, emailed to the maintainer.
# Intended to run from cron on the lab machine, e.g.:
#   0 8 * * 1  /path/to/ConfTracker/scripts/weekly_report.sh >> /path/to/ConfTracker/logs/weekly.log 2>&1
#
# Preview it without sending:  scripts/weekly_report.sh --stdout
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
mkdir -p logs

# Same reason as run_daily.sh: cron's PATH does not include ~/.local/bin.
export PATH="$HOME/.local/bin:$PATH"

if [[ -f .env ]]; then
  set -a
  source .env
  set +a
fi

source .venv/bin/activate

echo "=== ConfTracker weekly report: $(date -u +%FT%TZ) ==="

# Refresh origin/main so the "unpushed commits" check reflects the remote
# rather than whatever this machine last saw.
git fetch --quiet origin || echo "warning: git fetch failed; unpushed count may be stale"

python -m scraper.health "$@"

echo "=== Done: $(date -u +%FT%TZ) ==="
