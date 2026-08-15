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

# A conference that has gone this many consecutive daily runs without a
# successful extraction is reported as needing a look.
STALE_RUNS = int(os.environ.get("CONFTRACKER_STALE_RUNS", "5"))
# Horizon for the "upcoming deadlines" digest section.
DIGEST_DAYS = int(os.environ.get("CONFTRACKER_DIGEST_DAYS", "45"))
