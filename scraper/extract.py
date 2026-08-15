"""Extract conference deadline data from page text using Claude.

Two interchangeable backends, auto-selected:
  - "api": the Anthropic SDK with structured outputs (needs ANTHROPIC_API_KEY)
  - "cli": headless Claude Code (`claude -p`), which authenticates with the
    machine's Claude subscription login -- no API key needed. Log in once by
    running `claude` interactively on the lab machine.

Force one with CONFTRACKER_BACKEND=api|cli. Either way the output is
validated against the same Pydantic schema before anything enters the data.
"""

import json
import logging
import os
import shutil
import subprocess
from datetime import date

import anthropic
from pydantic import BaseModel, ValidationError

from . import config

log = logging.getLogger(__name__)


class TimelineEntry(BaseModel):
    abstract_deadline: str | None = None  # "YYYY-MM-DD HH:MM:SS"
    deadline: str | None = None  # full-paper deadline, "YYYY-MM-DD HH:MM:SS"
    comment: str | None = None  # e.g. "Summer Deadlines", workshop name
    # null = main paper track; otherwise a short label such as
    # "Workshop", "Poster", "Demo", "Tutorial", "SRC", "Doctoral Forum"
    track: str | None = None


class ConfCycle(BaseModel):
    year: int  # year the conference takes place
    link: str | None = None
    timeline: list[TimelineEntry]
    timezone: str | None = None  # e.g. "UTC-12", "UTC+8", "AoE"
    date: str | None = None  # e.g. "October 17-21, 2026"
    place: str | None = None  # e.g. "Austin, Texas, USA"


class Extraction(BaseModel):
    found: bool  # false if the page has no concrete upcoming CFP deadlines
    cycle: ConfCycle | None = None
    confidence: str  # "high" | "medium" | "low"
    notes: str | None = None  # anything ambiguous worth logging
    # When found=false but the page links to a page that likely carries the
    # CFP deadlines (a "Call for Papers" subpage, the current edition's own
    # site), its absolute URL. The pipeline follows it exactly one hop.
    follow_url: str | None = None
    # A page listing the individual workshop / poster / demo / tutorial calls
    # with their own deadlines. Followed once, in addition to the main
    # result, because those dates rarely appear on the main CFP page.
    tracks_url: str | None = None


SYSTEM = """\
You extract paper-submission deadline information for academic conferences \
from web page text. Be precise: only report deadlines explicitly stated on \
the page; never guess or infer dates. If the page shows no concrete upcoming \
submission deadline, return found=false. Dates use "YYYY-MM-DD HH:MM:SS" \
(use 23:59:59 when no time is given). Timezone uses forms like "UTC-12", \
"UTC+8", or "AoE" (Anywhere on Earth). If the page lists multiple submission \
rounds (e.g. summer/winter, or abstract + full paper), include all of them in \
the timeline. Also extract non-main-track deadlines stated on the page -- \
workshops, posters, demos, tutorials, SRC, doctoral forum: set track to a \
short label like "Workshop" or "Poster" (main paper track keeps track=null), \
and put a specific workshop's name in comment. Keep comment to a few words \
(e.g. "Summer deadlines", a round name, or a workshop name) -- notification \
and camera-ready dates and other details belong in notes, not comment. \
Put abstract/title-registration deadlines in abstract_deadline \
and full-paper deadlines in deadline; if only one of the two is announced, \
leave the other null -- never fabricate or reuse a date across fields. Set \
confidence to "low" whenever dates are ambiguous, refer to \
a past edition, or might not be the paper-submission deadline. Link targets \
appear in the text as [absolute URLs]; if the page itself has no deadlines \
but links to a page that likely does (a Call for Papers subpage, or the \
current edition's dedicated site), put that one URL in follow_url. \
Separately, if the page links to a page listing the individual workshops \
(or posters/demos/tutorials) and their own submission deadlines, put that \
URL in tracks_url -- individual workshops usually have separate deadlines \
from the main track and from each other. When the page you are reading is \
itself such a listing, return every workshop as its own timeline entry: \
one entry per workshop, track set to "Workshop" (or "Poster"/"Demo"/\
"Tutorial"), and the workshop's own name or acronym in comment."""


def _prompt(conf_title: str, url: str, page_text: str) -> str:
    return (
        f"Today's date: {date.today().isoformat()}\n"
        f"Conference: {conf_title}\n"
        f"Page URL: {url}\n\n"
        f"Page text:\n---\n{page_text}\n---\n\n"
        f"Extract the upcoming call-for-papers cycle for {conf_title}."
    )


def _backend() -> str:
    mode = os.environ.get("CONFTRACKER_BACKEND", "auto")
    if mode in ("api", "cli"):
        return mode
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "api"
    if shutil.which("claude"):
        return "cli"
    return "api"  # will fail gracefully with an auth error in the log


def active_backend() -> str:
    """Which backend this run will use ("api" or "cli"). For run records."""
    return _backend()


# ---------- backend: Anthropic SDK ----------

def _extract_api(prompt: str) -> Extraction | None:
    client = anthropic.Anthropic()
    response = client.messages.parse(
        model=config.MODEL,
        max_tokens=4096,
        output_config={"effort": "low"},  # simple extraction task
        system=SYSTEM,
        messages=[{"role": "user", "content": prompt}],
        output_format=Extraction,
    )
    if response.stop_reason == "refusal" or response.parsed_output is None:
        log.warning("API backend: no usable output (stop_reason=%s)", response.stop_reason)
        return None
    return response.parsed_output


# ---------- backend: headless Claude Code (`claude -p`) ----------

def _extract_cli(prompt: str) -> Extraction | None:
    cmd = [
        "claude",
        "-p", prompt,
        "--system-prompt", SYSTEM,  # replace the agentic default entirely
        "--json-schema", json.dumps(Extraction.model_json_schema()),
        "--output-format", "json",
        "--no-session-persistence",
    ]
    cli_model = os.environ.get("CONFTRACKER_CLI_MODEL")
    if cli_model:
        cmd += ["--model", cli_model]

    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if proc.returncode != 0:
        log.error("claude -p failed (rc=%s): %s", proc.returncode, proc.stderr[:500])
        return None

    payload = json.loads(proc.stdout)
    structured = payload.get("structured_output")
    if structured is None:
        log.warning(
            "claude -p returned no structured output (subtype=%s)",
            payload.get("subtype"),
        )
        return None
    return Extraction.model_validate(structured)


# ---------- public entry point ----------

def extract_cycle(conf_title: str, url: str, page_text: str) -> Extraction | None:
    """Extract the upcoming CFP cycle from a page. None on any failure."""
    backend = _backend()
    prompt = _prompt(conf_title, url, page_text)
    try:
        if backend == "cli":
            return _extract_cli(prompt)
        return _extract_api(prompt)
    except (ValidationError, json.JSONDecodeError) as e:
        log.warning("%s backend returned malformed output for %s: %s", backend, conf_title, e)
        return None
    except Exception as e:
        # auth errors, timeouts, missing binary -- never kill the daily run
        log.error("Extraction (%s backend) failed for %s: %s", backend, conf_title, e)
        return None
