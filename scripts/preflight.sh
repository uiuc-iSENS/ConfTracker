#!/usr/bin/env bash
# Is ConfTracker actually able to run unattended on this machine?
#
#   scripts/preflight.sh            check everything, change nothing
#   scripts/preflight.sh --send-test  also send one test mail to REPORT_TO
#
# Every check here corresponds to something that has to be true at 02:00 with
# nobody watching. Run it after setup, after a machine reboot, and whenever
# the weekly health report says something looks wrong.
#
# Exit status: 0 if everything passed (warnings allowed), 1 if anything failed.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

SEND_TEST=0
[[ "${1:-}" == "--send-test" ]] && SEND_TEST=1

FAILED=0
WARNED=0

ok()   { printf '  [ OK ] %s\n' "$*"; }
warn() { printf '  [WARN] %s\n' "$*"; WARNED=$((WARNED + 1)); }
fail() { printf '  [FAIL] %s\n' "$*"; FAILED=$((FAILED + 1)); }
head_() { printf '\n%s\n' "$*"; }

echo "ConfTracker preflight -- $(date '+%F %T %Z') on $(hostname)"
echo "Repo: $ROOT"

# Load .env exactly the way the cron scripts do, so this checks what they see.
if [[ -f .env ]]; then
  set -a; source .env; set +a
fi

PY=".venv/bin/python"

# ---- 1. Python environment ------------------------------------------------
head_ "Python environment"
if [[ -x "$PY" ]]; then
  ok "venv present ($("$PY" -V 2>&1))"
  if "$PY" -c "import scraper.main, scraper.remind, scraper.inbox" 2>/dev/null; then
    ok "scraper package imports"
  else
    fail "scraper package does not import -- run: pip install -r requirements.txt"
  fi
else
  fail "no .venv -- run: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"
fi

# ---- 2. Extraction backend ------------------------------------------------
# The classic silent failure: cron's PATH has no ~/.local/bin, the `claude`
# CLI is invisible, the pipeline falls back to the API backend and every
# extraction fails on a missing key. The scripts fix the PATH themselves;
# this confirms there is something for them to find.
head_ "Claude extraction backend"
if [[ -n "${ANTHROPIC_API_KEY:-}" ]]; then
  ok "ANTHROPIC_API_KEY set (API backend)"
elif [[ -x "$HOME/.local/bin/claude" ]]; then
  ok "claude CLI at ~/.local/bin/claude (subscription backend)"
  if ! "$HOME/.local/bin/claude" --version >/dev/null 2>&1; then
    warn "claude CLI does not answer --version; a stale login fails silently"
  fi
elif command -v claude >/dev/null 2>&1; then
  warn "claude found at $(command -v claude), outside ~/.local/bin -- the cron scripts only add ~/.local/bin to PATH"
else
  fail "no API key and no claude CLI: every extraction will fail"
fi

# ---- 3. Optional: JS-rendered conference sites -----------------------------
head_ "Playwright (JS-rendered conference sites)"
if [[ -x "$PY" ]] && "$PY" -c "
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    p.chromium.launch().close()
" >/dev/null 2>&1; then
  ok "chromium launches"
else
  warn "chromium unavailable -- JS-rendered sites will be skipped (playwright install chromium)"
fi

# ---- 4. Git publishing ----------------------------------------------------
# Pushing IS the deployment: GitHub Pages serves docs/ from main. A push that
# cannot authenticate means the site freezes while the scrape keeps succeeding.
head_ "Git publishing"
if ! git remote get-url origin >/dev/null 2>&1; then
  fail "no 'origin' remote -- nothing will ever be published"
else
  ok "origin: $(git remote get-url origin)"
  if timeout 45 git ls-remote --exit-code origin HEAD >/dev/null 2>&1; then
    ok "remote reachable and credentials accepted"
  else
    fail "cannot authenticate to origin -- cron will commit forever and never push"
  fi
  if git rev-parse --abbrev-ref '@{u}' >/dev/null 2>&1; then
    ok "branch tracks $(git rev-parse --abbrev-ref '@{u}')"
  else
    warn "current branch has no upstream; 'git push origin HEAD' still works"
  fi
  unpushed="$(git rev-list --count '@{u}'..HEAD 2>/dev/null || echo 0)"
  if [[ "$unpushed" -gt 0 ]]; then
    warn "$unpushed commit(s) unpushed -- the published site is behind"
  else
    ok "nothing unpushed"
  fi
fi

# ---- 5. File locking ------------------------------------------------------
# The repo is on NFS, where flock is not guaranteed. If it silently no-ops,
# two scrapes can run at once and fight over the same YAML files.
head_ "Run locking (repo is on $(df -P . | tail -1 | awk '{print $1}'))"
mkdir -p logs
( exec 9>logs/.preflight.lock; flock -n 9 && sleep 3 ) &
sleep 0.5
if ( exec 9>logs/.preflight.lock; flock -n 9 ) 2>/dev/null; then
  fail "flock is not exclusive on this filesystem -- overlapping runs are possible"
else
  ok "flock is exclusive (concurrent runs will step aside)"
fi
wait 2>/dev/null
rm -f logs/.preflight.lock

# ---- 6. Outgoing mail -----------------------------------------------------
head_ "Outgoing mail"
if [[ -x "$PY" ]]; then
  "$PY" - <<'EOF'
import smtplib, sys
sys.path.insert(0, ".")
from scraper import config

warned = False
try:
    s = smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=15)
    s.ehlo()
    if config.SMTP_USER:
        # Authenticated sending (the group Gmail): actually log in. An app
        # password that has been revoked looks exactly like a working one
        # until the first reminder silently fails to go out.
        s.starttls(); s.ehlo()
        s.login(config.SMTP_USER, config.SMTP_PASSWORD)
        print(f"  [ OK ] {config.SMTP_HOST}:{config.SMTP_PORT} accepted the login for {config.SMTP_USER}")
        if config.REPORT_FROM.lower() != config.SMTP_USER.lower():
            print(f"  [WARN] sending as {config.SMTP_USER} but From: is {config.REPORT_FROM}"
                  " -- Gmail rewrites the From to the account it authenticated")
            warned = True
    else:
        print(f"  [ OK ] relay {config.SMTP_HOST}:{config.SMTP_PORT} accepts a connection (unauthenticated)")
        if not config.REPORT_FROM.lower().endswith("illinois.edu"):
            print(f"  [WARN] From: {config.REPORT_FROM} is not @illinois.edu"
                  " -- the campus relay needs that for SPF; mail will be filtered")
            warned = True
    s.quit()
except Exception as e:
    print(f"  [FAIL] {config.SMTP_HOST}:{config.SMTP_PORT} -- {type(e).__name__}: {e}")
    sys.exit(1)
sys.exit(2 if warned else 0)
EOF
  rc=$?
  [[ $rc -eq 1 ]] && FAILED=$((FAILED + 1))
  [[ $rc -eq 2 ]] && WARNED=$((WARNED + 1))
fi

if (( SEND_TEST )); then
  if "$PY" -c "
from scraper import config, mail
mail.send('[ConfTracker] preflight test', 'Sent by scripts/preflight.sh --send-test.\n\nIf this arrived, unattended mail works.')
print('  [ OK ] test mail sent to', config.REPORT_TO)
"; then :; else fail "could not send test mail"; fi
fi

# ---- 7. Reminder subsystem ------------------------------------------------
head_ "Deadline reminders"
if [[ -z "${CONFTRACKER_SIGNUP_REPO:-}" && -z "${CONFTRACKER_SUBSCRIBE_ADDRESS:-}" ]]; then
  warn "no signup channel configured -- reminders are off and the signup UI is hidden"
else
  if "$PY" -c "
import json, sys
r = json.load(open('docs/data.json')).get('reminders') or {}
sys.exit(0 if (r.get('repo') or r.get('address')) else 1)
" 2>/dev/null; then
    ok "docs/data.json carries the signup channel (site shows the buttons)"
  else
    warn "docs/data.json has no reminders block -- rebuild it: $PY -m scraper.build"
  fi

  # ---- signup channel: GitHub issues ----
  if [[ -n "${CONFTRACKER_SIGNUP_REPO:-}" ]]; then
    label="${CONFTRACKER_SIGNUP_LABEL:-reminder-signup}"
    if ! command -v gh >/dev/null 2>&1; then
      fail "gh CLI not found -- signup issues cannot be read"
    elif ! gh auth status >/dev/null 2>&1; then
      fail "gh is not authenticated (run: gh auth login) -- signup issues cannot be read"
    # isPrivate, not visibility: the latter does not exist in older gh
    # (Ubuntu ships 2.4.0), and asking for it fails the whole check.
    elif ! private="$(gh repo view "$CONFTRACKER_SIGNUP_REPO" --json isPrivate -q .isPrivate 2>/dev/null)"; then
      fail "signup repo $CONFTRACKER_SIGNUP_REPO not accessible to this gh login"
    else
      ok "signup repo $CONFTRACKER_SIGNUP_REPO reachable (private: $private)"
      # A signup issue contains someone's email address. On a public repo
      # that address is published for good -- editing it out later does not
      # help, because issue edit history is world-readable too.
      if [[ "$private" != "true" ]]; then
        fail "signup repo is public -- every signup would publish the subscriber's email address"
      fi
      # Via the API rather than `gh label list`: that subcommand only exists
      # from gh 2.6 onwards.
      if gh api "repos/$CONFTRACKER_SIGNUP_REPO/labels" -q '.[].name' 2>/dev/null | grep -qx "$label"; then
        ok "label '$label' exists"
      else
        # GitHub silently drops an unknown label from an issue-new URL, so
        # the issues would be filed and then never found by the poller.
        fail "label '$label' does not exist in the repo -- signups would be filed and never picked up"
      fi
      open_n="$(gh issue list --repo "$CONFTRACKER_SIGNUP_REPO" --label "$label" --state open --json number -q 'length' 2>/dev/null || echo '?')"
      ok "$open_n signup issue(s) waiting"
    fi
  fi

  # ---- signup channel: IMAP mailbox ----
  if [[ -n "${CONFTRACKER_SUBSCRIBE_ADDRESS:-}" ]]; then
    ok "signup address: $CONFTRACKER_SUBSCRIBE_ADDRESS"
    if [[ -z "${CONFTRACKER_IMAP_USER:-}" || -z "${CONFTRACKER_IMAP_PASSWORD:-}" ]]; then
      fail "IMAP credentials missing -- mailed signups will never be read (see .env.example)"
    else
    "$PY" - <<'EOF'
import imaplib, sys
sys.path.insert(0, ".")
from scraper import config
try:
    with imaplib.IMAP4_SSL(config.IMAP_HOST, config.IMAP_PORT) as m:
        m.login(config.IMAP_USER, config.IMAP_PASSWORD)
        m.select(config.IMAP_MAILBOX, readonly=True)
        ok, res = m.search(None, "UNSEEN")
        n = len(res[0].split()) if ok == "OK" else "?"
    print(f"  [ OK ] signup mailbox reachable ({n} unread)")
except Exception as e:
    print(f"  [FAIL] signup mailbox login failed: {e}")
    sys.exit(1)
EOF
      [[ $? -ne 0 ]] && FAILED=$((FAILED + 1))
    fi
  fi

  # ---- the store itself, whichever channel filled it ----
  store="${CONFTRACKER_SUBSCRIBERS:-$ROOT/private/subscribers.json}"
  if [[ -f "$store" ]]; then
    perms="$(stat -c '%a' "$store")"
    count="$("$PY" -c "import json;print(len(json.load(open('$store')).get('subscribers',{})))" 2>/dev/null || echo '?')"
    if [[ "$perms" == "600" ]]; then
      ok "subscriber store: $count subscriber(s), mode $perms"
    else
      warn "subscriber store is mode $perms, expected 600 -- it holds real addresses"
    fi
  else
    ok "no subscribers yet (store is created on the first signup)"
  fi
fi

# ---- 8. Published data freshness ------------------------------------------
head_ "Published data"
if [[ -f docs/data.json ]]; then
  "$PY" - <<'EOF'
import json, sys
from datetime import datetime, timezone
d = json.load(open("docs/data.json"))
gen = d.get("generated_at")
age = (datetime.now(timezone.utc) - datetime.fromisoformat(gen)).total_seconds() / 3600
n = len(d.get("conferences") or [])
mark = "[ OK ]" if age < 48 else "[WARN]"
print(f"  {mark} docs/data.json: {n} venues, generated {age:.1f}h ago")
sys.exit(0 if age < 48 else 2)
EOF
  [[ $? -eq 2 ]] && WARNED=$((WARNED + 1))
else
  fail "docs/data.json missing -- the site has nothing to render"
fi

# ---- 9. Cron --------------------------------------------------------------
head_ "Cron"
if crontab -l 2>/dev/null | grep -q 'ConfTracker'; then
  ok "$(crontab -l 2>/dev/null | grep -c 'ConfTracker/scripts') ConfTracker entries installed"
  crontab -l 2>/dev/null | grep 'ConfTracker/scripts' | sed 's/^/         /'
else
  fail "no ConfTracker entries in crontab -- nothing is scheduled (scripts/install_cron.sh)"
fi
echo "         cron fires in this machine's local time: $(date '+%Z, now %H:%M') (UTC $(date -u '+%H:%M'))"

# ---- 10. Disk -------------------------------------------------------------
head_ "Disk"
avail="$(df -Pk . | tail -1 | awk '{print $4}')"
if [[ "$avail" -lt 1048576 ]]; then
  warn "under 1 GB free on the repo filesystem"
else
  ok "$(df -Ph . | tail -1 | awk '{print $4}') free on the repo filesystem"
fi

# ---- verdict --------------------------------------------------------------
echo
if (( FAILED )); then
  echo "PREFLIGHT: $FAILED failure(s), $WARNED warning(s) -- unattended operation will not work as configured."
  exit 1
fi
echo "PREFLIGHT: all checks passed${WARNED:+ ($WARNED warning(s))}."
exit 0
