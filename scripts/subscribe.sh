#!/usr/bin/env bash
# Manage reminder subscribers by hand -- the admin counterpart to the signup
# buttons on the site.
#
#   scripts/subscribe.sh --list
#   scripts/subscribe.sh alice@illinois.edu subscribe tier1 tracks:all
#   scripts/subscribe.sh alice@illinois.edu unsubscribe
#   scripts/subscribe.sh alice@illinois.edu subscribe all days:30,7,1 --notify
#
# The command words are exactly what the site's buttons put in an issue title,
# so handling a signup that arrived some other way is copy and paste.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

export PATH="$HOME/.local/bin:$PATH"

if [[ -f .env ]]; then
  set -a
  source .env
  set +a
fi

source .venv/bin/activate
exec python -m scraper.subscribe "$@"
