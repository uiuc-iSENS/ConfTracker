#!/usr/bin/env bash
# Install (or remove) ConfTracker's crontab entries, idempotently.
#
#   scripts/install_cron.sh            show what would change, change nothing
#   scripts/install_cron.sh --apply    install, backing up the current crontab
#   scripts/install_cron.sh --remove   take the entries out again
#
# The entries live between marker lines, so re-running --apply replaces the
# block rather than stacking a second copy of every job -- which is the usual
# way a scrape ends up running four times a night.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BEGIN="# >>> ConfTracker (managed by scripts/install_cron.sh) >>>"
END="# <<< ConfTracker <<<"

MODE="${1:-show}"

# Times are LOCAL to this machine -- cron has no opinion about UTC. The
# comments give the UTC equivalent for whoever reads the weekly report.
read -r -d '' BLOCK <<EOF || true
$BEGIN
# Nothing here mails on its own: every job redirects to logs/, and silent
# breakage is caught by the Monday health report instead.
MAILTO=""

# Scrape every conference, rebuild the site, commit and push. Nightly, when
# conference sites are idle and a slow run bothers nobody.
0 2 * * *     $ROOT/scripts/run_daily.sh >> $ROOT/logs/daily.log 2>&1

# Read the signup mailbox often, so subscribing feels immediate.
*/30 * * * *  $ROOT/scripts/run_reminders.sh --inbox-only >> $ROOT/logs/reminders.log 2>&1

# Send the day's reminders once, in the morning, after the nightly scrape has
# refreshed the deadlines -- one digest per person rather than a trickle.
15 8 * * *    $ROOT/scripts/run_reminders.sh --remind-only >> $ROOT/logs/reminders.log 2>&1

# Is any of the above still working? Mondays.
0 3 * * 1     $ROOT/scripts/weekly_report.sh >> $ROOT/logs/weekly.log 2>&1

# Keep logs/ from growing without bound.
30 3 * * 0    /usr/sbin/logrotate --state $ROOT/logs/.logrotate.state $ROOT/scripts/conftracker.logrotate >> $ROOT/logs/logrotate.log 2>&1
$END
EOF

current="$(crontab -l 2>/dev/null || true)"
# Everything except a previously-installed block.
without="$(printf '%s\n' "$current" | awk -v b="$BEGIN" -v e="$END" '
  $0 == b {skip = 1} !skip {print} $0 == e {skip = 0}')"

case "$MODE" in
  --remove) proposed="$without" ;;
  --apply|show|"")
    if [[ -n "$without" && "$without" != $'\n' ]]; then
      proposed="$without"$'\n'"$BLOCK"
    else
      proposed="$BLOCK"
    fi
    ;;
  *) echo "usage: $0 [--apply|--remove]" >&2; exit 2 ;;
esac

if [[ "$MODE" != "--apply" && "$MODE" != "--remove" ]]; then
  echo "=== crontab as it would become ==="
  printf '%s\n' "$proposed"
  echo
  echo "Nothing has been changed. Install it with:  $0 --apply"
  exit 0
fi

if [[ -n "$current" ]]; then
  backup="$ROOT/logs/crontab.backup.$(date +%Y%m%d-%H%M%S)"
  mkdir -p "$ROOT/logs"
  printf '%s\n' "$current" > "$backup"
  echo "Previous crontab saved to $backup"
fi

printf '%s\n' "$proposed" | crontab -
echo "crontab updated."
crontab -l | grep -E 'ConfTracker|^\*|^[0-9]' | sed 's/^/  /'

# An installed crontab does nothing if the daemon is not running -- which is
# easy to miss on a machine that was rebuilt or a container.
if pgrep -x cron >/dev/null 2>&1 || pgrep -x crond >/dev/null 2>&1; then
  echo "cron daemon is running."
else
  echo "WARNING: no cron daemon appears to be running -- these entries will never fire."
fi
