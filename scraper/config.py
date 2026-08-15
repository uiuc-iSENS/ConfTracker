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
