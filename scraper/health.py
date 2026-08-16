"""Weekly health report: is the daily pipeline still actually working?

The failure mode this guards against is a silent one. Every stage of the
daily run is deliberately fault-tolerant -- a conference site that gets
redesigned, an extractor that stops returning usable output, a push that
never lands -- all of them are logged and stepped over so one bad site can
never take down the run. That is right for the pipeline and useless for the
operator: from the outside a totally broken run and a healthy one both look
like "cron fired, exit 0".

So this reads what the runs actually recorded (logs/history.jsonl, written by
scraper.main) plus the current state of the repo, and mails a summary once a
week. Delivery goes through the university outbound relay, which accepts mail
from the lab machine's campus IP unauthenticated -- no credentials to store,
no MTA to run, nothing to renew.

    python -m scraper.health            # build and send
    python -m scraper.health --stdout   # print instead, for a look first
"""

import argparse
import json
import logging
import subprocess
import sys
from collections import Counter
from datetime import date, datetime, timedelta, timezone

import yaml

from . import config, mail, subscriptions, validate

log = logging.getLogger(__name__)

# Statuses that mean the pipeline itself misbehaved, as opposed to a
# conference that simply has no call for papers up right now.
BROKEN = {"fetch_failed", "extract_failed"}
UNUSABLE = {"low_confidence", "rejected"}

STATUS_LABEL = {
    "updated": "updated",
    "no_cfp": "no CFP",
    "low_confidence": "low conf",
    "rejected": "rejected",
    "fetch_failed": "unreachable",
    "extract_failed": "extract err",
    "no_url": "no URL",
}


# ---------- inputs ----------

def load_runs(days: int) -> list[dict]:
    """Run records from the last `days` days, oldest first."""
    if not config.RUN_HISTORY.exists():
        return []
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    runs = []
    for line in config.RUN_HISTORY.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
            started = datetime.fromisoformat(entry["started_at"])
        except (json.JSONDecodeError, KeyError, ValueError):
            continue  # a truncated line must not break the report
        if started >= cutoff:
            runs.append(entry)
    runs.sort(key=lambda r: r["started_at"])
    return runs


def history_started() -> datetime | None:
    """When run history began, so a young history is not read as missed runs."""
    if not config.RUN_HISTORY.exists():
        return None
    for line in config.RUN_HISTORY.read_text().splitlines():
        if not line.strip():
            continue
        try:
            return datetime.fromisoformat(json.loads(line)["started_at"])
        except (json.JSONDecodeError, KeyError, ValueError):
            continue
    return None


def _git(*args: str) -> str:
    try:
        return subprocess.run(
            ["git", *args],
            cwd=config.ROOT,
            capture_output=True,
            text=True,
            timeout=60,
        ).stdout.strip()
    except Exception as e:
        log.warning("git %s failed: %s", " ".join(args), e)
        return ""


def git_status() -> dict:
    """Last data commit and whether anything is sitting unpushed."""
    unpushed = _git("rev-list", "--count", "origin/main..HEAD")
    return {
        "last_commit": _git("log", "-1", "--format=%h  %s"),
        "last_commit_age": _git("log", "-1", "--format=%cr"),
        "unpushed": int(unpushed) if unpushed.isdigit() else None,
        "dirty": bool(_git("status", "--porcelain", "data", "docs/data.json")),
    }


def site_freshness() -> str | None:
    """When the published data.json was last regenerated."""
    try:
        data = json.loads(config.SITE_DATA.read_text())
        return data.get("generated_at")
    except (OSError, json.JSONDecodeError):
        return None


def _parse_deadline(value: str) -> datetime | None:
    for fmt in validate.DATE_FORMATS:
        try:
            return datetime.strptime(value.strip(), fmt)
        except ValueError:
            continue
    return None


def upcoming_deadlines(days: int) -> list[dict]:
    """Every deadline on record falling within the next `days` days."""
    now = datetime.now()
    horizon = now + timedelta(days=days)
    out = []
    for path in sorted(config.CONF_DIR.glob("*.yml")):
        try:
            conf = yaml.safe_load(path.read_text())[0]
        except (OSError, yaml.YAMLError, IndexError, TypeError):
            continue
        for cycle in conf.get("confs") or []:
            for entry in cycle.get("timeline") or []:
                for field in ("abstract_deadline", "deadline"):
                    raw = entry.get(field)
                    if not raw:
                        continue
                    when = _parse_deadline(str(raw))
                    if when is None or not (now <= when <= horizon):
                        continue
                    track = entry.get("track") or "main"
                    if field == "abstract_deadline":
                        track += " (abstract)"
                    out.append(
                        {
                            "stem": path.stem,
                            "title": f"{conf.get('title', path.stem)} {cycle.get('year', '')}".strip(),
                            "when": when,
                            "track": track,
                            "comment": entry.get("comment") or "",
                            "tz": cycle.get("timezone") or "",
                            "days": (when - now).days,
                        }
                    )
    out.sort(key=lambda d: d["when"])
    return out


# ---------- analysis ----------

def conference_health(runs: list[dict]) -> tuple[list[tuple[str, str, str]], int]:
    """Conferences worth a human look, as (severity, conf, explanation).

    Severity is "ERROR" for a pipeline that is broken for that conference and
    "WARN" for one that keeps producing nothing usable. Returns the list plus
    the count of conferences that are fine.
    """
    if not runs:
        return [], 0

    # Per conference, statuses across the window, most recent run last.
    # Restricted to venues still configured: a retired one (IPSN and IoTDI
    # were absorbed by SenSys in 2026) stays in the history window for days
    # after its YAML is deleted, and would be reported as broken the whole
    # time even though dropping it was deliberate.
    tracked = {path.stem for path in config.CONF_DIR.glob("*.yml")}
    per_conf: dict[str, list[str]] = {}
    for run in runs:
        for rec in run.get("results") or []:
            if rec["conf"] in tracked:
                per_conf.setdefault(rec["conf"], []).append(rec.get("status", "?"))

    # A conference with a deadline already on record and in the future is
    # fine even if recent runs found nothing new -- the data is captured.
    has_future = {d["stem"] for d in upcoming_deadlines(3650)}

    issues, healthy = [], 0
    for conf, statuses in sorted(per_conf.items()):
        recent = statuses[-config.STALE_RUNS :]
        counts = Counter(recent)
        n = len(recent)

        if counts["no_url"] == n:
            issues.append(("WARN", conf, "no scrape URL configured"))
        elif counts["fetch_failed"] + counts["extract_failed"] == n:
            worst = "unreachable" if counts["fetch_failed"] >= counts["extract_failed"] else "extractor errors"
            issues.append(("ERROR", conf, f"{worst} on every URL for {n} run(s)"))
        elif counts["updated"] == 0 and counts["low_confidence"] + counts["rejected"] > 0:
            reason = "low-confidence" if counts["low_confidence"] >= counts["rejected"] else "validation-rejected"
            issues.append(
                ("WARN", conf, f"{reason} every run for {n} run(s); nothing merged")
            )
        elif counts["updated"] == 0 and conf not in has_future:
            issues.append(
                ("WARN", conf, f"no CFP found for {n} run(s) and no future deadline on record")
            )
        else:
            healthy += 1

    order = {"ERROR": 0, "WARN": 1}
    issues.sort(key=lambda i: (order[i[0]], i[1]))
    return issues, healthy


def verdict(runs: list[dict], issues: list, git: dict, expected_runs: int) -> tuple[str, list[str]]:
    """Overall state plus the reasons behind it."""
    reasons = []

    if not runs:
        return "FAILING", ["no daily run recorded in the reporting window"]

    last = datetime.fromisoformat(runs[-1]["started_at"])
    age_h = (datetime.now(timezone.utc) - last).total_seconds() / 3600
    if age_h > 48:
        reasons.append(f"last run was {age_h / 24:.1f} days ago -- is cron still alive?")
    if len(runs) < expected_runs - 1:
        reasons.append(f"only {len(runs)} of ~{expected_runs} expected daily runs recorded")
    if git.get("unpushed"):
        reasons.append(f"{git['unpushed']} commit(s) never pushed -- the site is stale")

    errors = [i for i in issues if i[0] == "ERROR"]
    if errors:
        reasons.append(f"{len(errors)} conference(s) failing to scrape at all")

    if reasons:
        return ("FAILING" if (age_h > 48 or git.get("unpushed") or len(errors) > 5) else "DEGRADED"), reasons
    if issues:
        return "OK", [f"{len(issues)} conference(s) worth a look, nothing broken"]
    return "OK", ["all conferences scraped and published normally"]


# ---------- rendering ----------

def render(days: int = 7) -> tuple[str, str]:
    """Build the report. Returns (subject, body)."""
    runs = load_runs(days)
    git = git_status()
    issues, healthy = conference_health(runs)

    # Don't count days from before tracking existed as missed runs.
    started = history_started()
    expected = days
    if started is not None:
        age_days = (datetime.now(timezone.utc) - started).days
        expected = max(1, min(days, age_days + 1))
    state, reasons = verdict(runs, issues, git, expected_runs=expected)

    L = []
    a = L.append
    today = date.today().isoformat()
    a(f"ConfTracker health report -- {today}  (last {days} days)")
    a(config.SITE_URL)
    a("")
    a(f"OVERALL: {state}")
    for r in reasons:
        a(f"  - {r}")
    a("")

    a("DAILY RUNS")
    if runs:
        a(f"  {'date':<12}{'updated':>8}{'no CFP':>8}{'low/rej':>9}{'errors':>8}{'runtime':>9}")
        for run in runs:
            c = Counter(r.get("status") for r in run.get("results") or [])
            started = datetime.fromisoformat(run["started_at"])
            a(
                f"  {started.date().isoformat():<12}"
                f"{c['updated']:>8}{c['no_cfp']:>8}"
                f"{c['low_confidence'] + c['rejected']:>9}"
                f"{sum(c[s] for s in BROKEN):>8}"
                f"{run.get('duration_s', 0) // 60:>7}m "
            )
        backends = {r.get("backend") for r in runs}
        a(f"  {len(runs)} run(s) recorded; extraction backend: {', '.join(sorted(b or '?' for b in backends))}")
    else:
        a("  No runs recorded. Either cron is not firing or the pipeline dies")
        a("  before it can write logs/history.jsonl -- check logs/daily.log.")
    a("")

    a("PUBLISH")
    a(f"  last commit:  {git.get('last_commit') or 'unknown'}  ({git.get('last_commit_age') or '?'})")
    unpushed = git.get("unpushed")
    a(f"  unpushed:     {'none' if unpushed == 0 else unpushed if unpushed is not None else 'unknown (no origin/main ref)'}")
    a(f"  uncommitted:  {'yes -- data changed but never committed' if git.get('dirty') else 'none'}")
    a(f"  data.json:    generated {site_freshness() or 'unknown'}")
    a("")

    a("CONFERENCES NEEDING ATTENTION")
    if issues:
        for severity, conf, why in issues:
            a(f"  [{severity:<5}] {conf:<14} {why}")
        a(f"  ({healthy} other conference(s) healthy)")
    else:
        a(f"  None -- all {healthy} conference(s) behaving.")
    a("")

    # Reminder mail fails the same silent way the scrape does: cron stops
    # firing, or the signup mailbox stops answering, and the only visible
    # symptom is that nobody gets told about anything.
    if config.SUBSCRIBE_ADDRESS:
        a("REMINDER SUBSCRIPTIONS")
        try:
            store = subscriptions.load()
        except RuntimeError as e:
            a(f"  [ERROR] {e}")
        else:
            people = store.get("subscribers", {})
            paused = sum(1 for s in people.values() if s.get("paused"))
            last = max(
                (d for s in people.values() for d in (s.get("sent") or {}).values()),
                default=None,
            )
            a(f"  signup mailbox: {config.SUBSCRIBE_ADDRESS}")
            a(f"  subscribers:    {len(people)}" + (f"  ({paused} paused)" if paused else ""))
            a(f"  last reminder:  {last or 'none sent yet'}")
            if people and last:
                age = (date.today() - date.fromisoformat(last)).days
                if age > 14:
                    a(
                        f"  [WARN ] nothing sent for {age} days despite "
                        f"{len(people)} subscriber(s) -- is run_reminders.sh in cron?"
                    )
        a("")

    deadlines = upcoming_deadlines(config.DIGEST_DAYS)
    a(f"UPCOMING DEADLINES (next {config.DIGEST_DAYS} days)")
    if deadlines:
        for d in deadlines:
            # Extraction notes occasionally land in comment and run long;
            # keep the digest to one line per deadline.
            comment = d["comment"]
            if len(comment) > 42:
                comment = comment[:39].rstrip() + "..."
            note = f"  {comment}" if comment else ""
            a(
                f"  {d['when'].strftime('%Y-%m-%d %H:%M')} {d['tz']:<5} "
                f"{d['days']:>3}d  {d['title']:<18} {d['track']:<16}{note}"
            )
    else:
        a("  Nothing on record in that window.")
    a("")

    a("-- ")
    a("ConfTracker on lambda-vector. Daily scrape 07:00 UTC, this report Mondays 08:00 UTC.")
    a("If these reports stop arriving entirely, the machine or its cron is down.")
    a(f"Logs: {config.LOG_DIR}/daily.log   Config: {config.CONF_DIR}")

    subject = f"[ConfTracker] {state} -- weekly health report {today}"
    return subject, "\n".join(L)


# ---------- delivery ----------

def send(subject: str, body: str, to: str | None = None) -> None:
    mail.send(subject, body, to)


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    ap = argparse.ArgumentParser(description="Weekly ConfTracker health report")
    ap.add_argument("--stdout", action="store_true", help="print instead of emailing")
    ap.add_argument("--to", help=f"recipient (default {config.REPORT_TO})")
    ap.add_argument("--days", type=int, default=7, help="reporting window (default 7)")
    args = ap.parse_args()

    subject, body = render(args.days)
    if args.stdout:
        print(subject)
        print()
        print(body)
        return 0

    try:
        send(subject, body, args.to)
    except Exception as e:
        # Print it so the failed report still lands in the cron log.
        log.error("Could not send health report: %s", e)
        print(subject)
        print(body)
        return 1
    log.info("Health report sent to %s", args.to or config.REPORT_TO)
    return 0


if __name__ == "__main__":
    sys.exit(main())
