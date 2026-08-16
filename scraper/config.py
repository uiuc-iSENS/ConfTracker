"""Shared configuration for the ConfTracker scraper pipeline."""

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONF_DIR = ROOT / "data" / "conferences"
# GitHub Pages serves the docs/ directory from the main branch.
SITE_DIR = ROOT / "docs"
SITE_DATA = SITE_DIR / "data.json"

# Model used for deadline extraction. Override with CONFTRACKER_MODEL.
MODEL = os.environ.get("CONFTRACKER_MODEL", "claude-opus-5")

USER_AGENT = (
    "ConfTracker/1.0 (+https://isens.cs.illinois.edu/; academic conference "
    "deadline tracker; contact: smartlab.uiuc@gmail.com)"
)

# Cap on page text handed to the LLM (characters)
MAX_PAGE_TEXT = 30_000

# ---- run history + weekly health report ----

LOG_DIR = ROOT / "logs"
# One JSON object per daily run; the weekly health report reads this instead
# of re-parsing the text log.
RUN_HISTORY = LOG_DIR / "history.jsonl"

SITE_URL = os.environ.get(
    "CONFTRACKER_SITE_URL", "https://uiuc-isens.github.io/ConfTracker/"
)

# The lab machine sits on a campus IP, so the university outbound relay
# accepts mail from it unauthenticated -- no credentials, no API, no MTA
# to install. Sender stays @illinois.edu so SPF passes at the relay.
REPORT_TO = os.environ.get("CONFTRACKER_REPORT_TO", "mt55@illinois.edu")
REPORT_FROM = os.environ.get("CONFTRACKER_REPORT_FROM", "mt55@illinois.edu")
SMTP_HOST = os.environ.get(
    "CONFTRACKER_SMTP_HOST", "outbound-relays.techservices.illinois.edu"
)
SMTP_PORT = int(os.environ.get("CONFTRACKER_SMTP_PORT", "25"))
# Only needed off campus, where the relay will not take unauthenticated mail.
SMTP_USER = os.environ.get("CONFTRACKER_SMTP_USER", "")
SMTP_PASSWORD = os.environ.get("CONFTRACKER_SMTP_PASSWORD", "")

# ---- deadline reminder subscriptions ----

# Subscriber addresses are personal data and the site repo is public, so the
# store lives outside it (private/ is gitignored). Override for a path on
# encrypted storage or a shared lab volume.
PRIVATE_DIR = ROOT / "private"
SUBSCRIBERS = Path(
    os.environ.get("CONFTRACKER_SUBSCRIBERS", PRIVATE_DIR / "subscribers.json")
)

# ---- signup channel A: a GitHub issue ----
#
# The site's buttons open a prefilled issue; scraper.issues reads it with the
# `gh` CLI (already authenticated on the lab machine), applies the command,
# comments and closes. No new credential anywhere.
#
# Point this at a PRIVATE repo. A signup issue contains the subscriber's
# email address, and on a public repo that address is published permanently
# -- editing it out afterwards does not help, because issue edit history is
# world-readable too.
SIGNUP_REPO = os.environ.get("CONFTRACKER_SIGNUP_REPO", "")
SIGNUP_LABEL = os.environ.get("CONFTRACKER_SIGNUP_LABEL", "reminder-signup")

# ---- signup channel B: an IMAP mailbox ----
# The signup mailbox. People mail it a one-line command ("subscribe mobicom");
# scraper.inbox reads it over IMAP and updates the store. Sending the request
# from your own mailbox is itself the proof you own the address, which is why
# there is no confirmation round-trip and no web form to host.
IMAP_HOST = os.environ.get("CONFTRACKER_IMAP_HOST", "imap.gmail.com")
IMAP_PORT = int(os.environ.get("CONFTRACKER_IMAP_PORT", "993"))
IMAP_USER = os.environ.get("CONFTRACKER_IMAP_USER", "")
IMAP_PASSWORD = os.environ.get("CONFTRACKER_IMAP_PASSWORD", "")
IMAP_MAILBOX = os.environ.get("CONFTRACKER_IMAP_MAILBOX", "INBOX")

# Address advertised on the site; defaults to whatever mailbox we poll.
SUBSCRIBE_ADDRESS = os.environ.get("CONFTRACKER_SUBSCRIBE_ADDRESS", "") or IMAP_USER

# Who may subscribe. Lab-only for now; set to an empty string to open signups
# to anyone. This is a policy gate, not an authentication one -- ownership of
# the address is already established by the request arriving from it.
SUBSCRIBE_DOMAINS = [
    d.strip().lower().lstrip("@")
    for d in os.environ.get("CONFTRACKER_SUBSCRIBE_DOMAINS", "illinois.edu").split(",")
    if d.strip()
]

# Days before a deadline that a reminder goes out, unless the subscriber
# picks their own with "days:30,7".
DEFAULT_LEAD_DAYS = [14, 7, 3, 1]
# Refuse to act on more than this many signup mails in one poll: a mail loop
# or a bored undergrad should cost one run, not the mailbox.
MAX_INBOX_MESSAGES = int(os.environ.get("CONFTRACKER_MAX_INBOX", "200"))

# A conference that has gone this many consecutive daily runs without a
# successful extraction is reported as needing a look.
STALE_RUNS = int(os.environ.get("CONFTRACKER_STALE_RUNS", "5"))
# Horizon for the "upcoming deadlines" digest section.
DIGEST_DAYS = int(os.environ.get("CONFTRACKER_DIGEST_DAYS", "45"))
