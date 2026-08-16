#!/usr/bin/env bash
# Deadline reminders: read the signup mailbox, then mail whatever is now due.
# Intended to run from cron on the lab machine, e.g.:
#   */30 * * * *  /path/to/ConfTracker/scripts/run_reminders.sh --inbox-only >> /path/to/ConfTracker/logs/reminders.log 2>&1
#   30 13 * * *   /path/to/ConfTracker/scripts/run_reminders.sh              >> /path/to/ConfTracker/logs/reminders.log 2>&1
#
# Polling the mailbox often keeps signups feeling instant; the reminder pass
# runs once a day so a subscriber gets one digest rather than a trickle.
#
# Preview without sending or changing anything:
#   scripts/run_reminders.sh --stdout
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
mkdir -p logs

# Same reason as run_daily.sh: cron's PATH does not include ~/.local/bin.
export PATH="$HOME/.local/bin:$PATH"

# One at a time. This runs every half hour, and two overlapping polls both see
# the same unread mail before either marks it read -- which means two
# acknowledgements for one signup, and two copies of a reminder digest.
exec 8>logs/.reminders.lock
flock -n 8 || { echo "Another reminder run is in progress; exiting."; exit 0; }

# IMAP credentials for the signup mailbox live here, never in the repo.
if [[ -f .env ]]; then
  set -a
  source .env
  set +a
fi

# Reminders are opt-in. With no signup channel configured there is nothing to
# poll and nobody to mail, so leave without a word: this job runs every half
# hour, and a "not configured" line every half hour is 48 lines a day of noise
# in the log you would go to when something is actually wrong.
# scripts/preflight.sh is where that state is reported, once, clearly.
if [[ -z "${CONFTRACKER_SIGNUP_REPO:-}" && -z "${CONFTRACKER_IMAP_USER:-}" ]]; then
  exit 0
fi

source .venv/bin/activate

INBOX_ONLY=0
REMIND_ONLY=0
PASSTHRU=()
for arg in "$@"; do
  case "$arg" in
    --inbox-only)  INBOX_ONLY=1 ;;
    --remind-only) REMIND_ONLY=1 ;;
    *)             PASSTHRU+=("$arg") ;;
  esac
done

echo "=== ConfTracker reminders: $(date -u +%FT%TZ) ==="

# Signups first: someone who subscribes this morning should be included in
# today's reminder pass, not tomorrow's. Whichever channels are configured
# run; a channel that is down must not stop reminders going out to the people
# already subscribed, so neither failure is fatal.
if (( ! REMIND_ONLY )); then
  if [[ -n "${CONFTRACKER_SIGNUP_REPO:-}" ]]; then
    python -m scraper.issues "${PASSTHRU[@]+"${PASSTHRU[@]}"}" \
      || echo "warning: signup issue poll failed; existing subscribers unaffected"
  fi
  if [[ -n "${CONFTRACKER_IMAP_USER:-}" ]]; then
    python -m scraper.inbox "${PASSTHRU[@]+"${PASSTHRU[@]}"}" \
      || echo "warning: signup mailbox poll failed; existing subscribers unaffected"
  fi
fi

if (( ! INBOX_ONLY )); then
  python -m scraper.remind "${PASSTHRU[@]+"${PASSTHRU[@]}"}"
fi

echo "=== Done: $(date -u +%FT%TZ) ==="
