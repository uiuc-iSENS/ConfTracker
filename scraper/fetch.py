"""Fetch conference pages and convert them to plain text.

Strategy: plain HTTP first (covers most CFP pages), fall back to Playwright
for JavaScript-rendered sites when the static HTML has almost no text.
Playwright is optional -- if it is not installed, JS-heavy sites are skipped
with a warning.
"""

import logging
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from . import config

log = logging.getLogger(__name__)


def _html_to_text(html: str, base_url: str = "") -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    # Keep link targets visible so the extractor can suggest a follow-up URL
    # (e.g. a "Call for Papers" subpage).
    for a in soup.find_all("a", href=True):
        label = a.get_text(" ", strip=True)
        if label:
            a.replace_with(f"{label} [{urljoin(base_url, a['href'])}]")
    lines = [ln.strip() for ln in soup.get_text("\n").splitlines()]
    text = "\n".join(ln for ln in lines if ln)
    return text[: config.MAX_PAGE_TEXT]


def _fetch_static(url: str) -> tuple[str | None, int | None]:
    """Return (html, http_status). html is None on any failure."""
    try:
        r = requests.get(url, timeout=30, headers={"User-Agent": config.USER_AGENT})
        status = r.status_code
        r.raise_for_status()
        return r.text, status
    except requests.HTTPError:
        log.info("Static fetch got HTTP %s for %s", status, url)
        return None, status
    except Exception as e:
        log.warning("Static fetch failed for %s: %s", url, e)
        return None, None


def _fetch_playwright(url: str) -> str | None:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        log.warning("Playwright not installed; cannot render %s", url)
        return None
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(user_agent=config.USER_AGENT)
            page.goto(url, timeout=45_000, wait_until="networkidle")
            html = page.content()
            browser.close()
            return html
    except Exception as e:
        log.warning("Playwright fetch failed for %s: %s", url, e)
        return None


def fetch_page_text(url: str) -> str | None:
    """Return the visible text of a page, or None if unreachable."""
    html, status = _fetch_static(url)
    text = _html_to_text(html, url) if html else ""

    # Playwright is only worth trying when a browser could plausibly do
    # better: a JS-shell page (200 but almost no visible text) or a
    # bot-blocking response. A plain 404 stays a 404 in a browser.
    js_shell = html is not None and len(text) < 500
    bot_blocked = status in (401, 403, 406, 429, 503)
    if js_shell or bot_blocked:
        rendered = _fetch_playwright(url)
        if rendered:
            text = _html_to_text(rendered, url)

    return text or None
