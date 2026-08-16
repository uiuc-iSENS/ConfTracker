"""Turn GitHub signup issues into reminder subscriptions.

Reading a mailbox automatically needs credentials Google only issues behind
2FA. This uses the one credential the lab machine already has: the `gh` CLI
is logged in, so it can read issues, comment and close them with nothing new
to store and nothing that expires on its own.

    site button  ->  a prefilled issue in the signup repo
                        title: subscribe MobiCom tracks:all
                        body:  Email: alice@illinois.edu
    this module  ->  applies it, mails a confirmation to that address,
                     comments with the result, closes the issue

The confirmation is not a formality. GitHub notifies the issue's author at
their *account* address, which is not necessarily the one they typed on the
Email: line -- so without a mail to the address we actually subscribed, a
typo there stays invisible until the reminders never turn up.

    python -m scraper.issues --stdout   # read and show, change nothing
    python -m scraper.issues            # apply, comment, close

Two things are worth being clear-eyed about, both handled below:

* The signup repo must be PRIVATE. The issue carries an email address, and a
  public repo publishes it permanently -- redacting afterwards does not help,
  since issue edit history is world-readable as well.
* Unlike the mailto flow, an issue proves the author owns a GitHub account,
  not that they own the address they typed. The domain allow-list and a
  private, lab-only repo are what stand in for that, and every reminder we
  send carries unsubscribe instructions.
"""

import argparse
import json
import logging
import re
import subprocess
import sys

from . import config, mail, subscriptions

log = logging.getLogger(__name__)

EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")

# Applied to an issue we could not act on, so the next run does not comment
# on it a second (and hundredth) time.
STUCK_LABEL = "needs-info"


def _gh(*args: str, check: bool = True) -> str:
    """Run the gh CLI and return stdout."""
    proc = subprocess.run(
        ["gh", *args], capture_output=True, text=True, timeout=120
    )
    if check and proc.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout


def open_signups(repo: str, label: str) -> list[dict]:
    out = _gh(
        "issue", "list",
        "--repo", repo,
        "--state", "open",
        "--label", label,
        "--json", "number,title,body,author,labels",
        "--limit", "100",
    )
    try:
        return json.loads(out or "[]")
    except json.JSONDecodeError as e:
        raise RuntimeError(f"could not parse gh output: {e}")


def extract_email(issue: dict) -> str | None:
    """The address to subscribe.

    The prefilled template puts it on an "Email:" line, so that line wins
    outright. People do retype these, though, so any address in the body is
    accepted as a fallback -- minus the signup mailbox itself, which can
    appear in quoted boilerplate.

    Note what is deliberately *not* excluded: the address the tracker sends
    from. Excluding it would mean whoever runs ConfTracker is the one person
    who cannot subscribe to it.
    """
    body = issue.get("body") or ""

    labelled = re.search(
        r"^[ \t>*-]*e-?mail\s*[:：]\s*(" + EMAIL_RE.pattern + ")",
        body,
        re.IGNORECASE | re.MULTILINE,
    )
    if labelled:
        return subscriptions.normalize_email(labelled.group(1))

    ours = {subscriptions.normalize_email(config.SUBSCRIBE_ADDRESS)} if config.SUBSCRIBE_ADDRESS else set()
    for match in EMAIL_RE.findall(body):
        candidate = subscriptions.normalize_email(match)
        if candidate not in ours:
            return candidate
    return None


def process(repo: str, label: str, dry_run: bool = False) -> int:
    """Handle every open signup issue. Returns the number acted on."""
    cat = subscriptions.catalog(json.loads(config.SITE_DATA.read_text()))
    store = subscriptions.load()
    issues = open_signups(repo, label)
    log.info("%d open signup issue(s) in %s", len(issues), repo)

    handled = 0
    for issue in issues:
        num = issue["number"]
        author = (issue.get("author") or {}).get("login", "?")
        labels = {l.get("name") for l in issue.get("labels") or []}

        email = extract_email(issue)
        if email is None:
            if STUCK_LABEL in labels:
                log.info("#%s: still no address, already asked; skipping", num)
                continue
            msg = (
                "I could not find an email address in this issue, so nothing "
                "has been subscribed.\n\nAdd a line like:\n\n    "
                "Email: you@illinois.edu\n\nand reopen the issue (or open a "
                "new one from the site's **remind me** button, which fills "
                "this in for you)."
            )
            log.warning("#%s by %s: no email address found", num, author)
            if not dry_run:
                _gh("issue", "comment", str(num), "--repo", repo, "--body", msg)
                _gh("issue", "edit", str(num), "--repo", repo, "--add-label", STUCK_LABEL)
            handled += 1
            continue

        if not subscriptions.domain_allowed(email):
            allowed = ", ".join(config.SUBSCRIBE_DOMAINS)
            msg = (
                f"Reminders are currently limited to {allowed} addresses, so "
                f"`{email}` has not been subscribed.\n\nThe tracker itself is "
                f"public and needs no signup: {config.SITE_URL}"
            )
            log.info("#%s: %s outside allowed domains", num, email)
            if not dry_run:
                _gh("issue", "comment", str(num), "--repo", repo, "--body", msg)
                _gh("issue", "close", str(num), "--repo", repo)
            handled += 1
            continue

        cmd = subscriptions.parse_message(issue.get("title") or "", issue.get("body") or "", cat)
        subject, body = subscriptions.apply(store, email, cmd, cat)
        log.info("#%s: %s -> %s", num, email, subject)

        if dry_run:
            print(f"--- #{num} by {author}: {issue.get('title')!r}")
            print(f"would subscribe: {email}")
            print(f"would comment:   {subject}")
            print(body)
            print()
            continue

        # Save before anything else: if GitHub or the relay is unreachable
        # next, the subscription should still stand rather than be lost.
        subscriptions.save(store)

        # Confirm to the address that was actually subscribed. GitHub will
        # notify the issue's author too, but at their *account* address --
        # so without this a typo in the Email: line is invisible until the
        # reminders that were supposed to arrive never do.
        mailed = _notify(email, subject, body)
        note = (
            f"A copy has been emailed to `{email}`. If it does not arrive, "
            "that address is wrong — open a new issue with the right one."
            if mailed
            else f"⚠️ The subscription is saved, but mail to `{email}` could "
            "not be sent. Check the address is right."
        )
        _gh(
            "issue", "comment", str(num), "--repo", repo,
            "--body", f"**{subject}**\n\n```\n{body}\n```\n\n{note}",
        )
        _gh("issue", "close", str(num), "--repo", repo)
        handled += 1

    return handled


def _notify(to: str, subject: str, body: str) -> bool:
    """Mail the subscriber what changed. False if it could not be sent."""
    try:
        mail.send(subject, body, to=to, reply_to=config.SUBSCRIBE_ADDRESS or None)
        return True
    except Exception as e:
        # Never fatal: the subscription is already saved, and the issue
        # comment says plainly that the mail did not go out.
        log.error("could not mail confirmation to %s: %s", to, e)
        return False


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    ap = argparse.ArgumentParser(description="Process ConfTracker signup issues")
    ap.add_argument(
        "--stdout", "--dry-run",
        dest="dry_run", action="store_true",
        help="show what would happen; do not comment, close, or save",
    )
    ap.add_argument("--repo", help=f"owner/name (default {config.SIGNUP_REPO or 'unset'})")
    ap.add_argument("--label", default=config.SIGNUP_LABEL)
    args = ap.parse_args()

    repo = args.repo or config.SIGNUP_REPO
    if not repo:
        log.error(
            "No signup repo configured: set CONFTRACKER_SIGNUP_REPO to a "
            "PRIVATE owner/name repository."
        )
        return 1

    try:
        n = process(repo, args.label, dry_run=args.dry_run)
    except (RuntimeError, OSError, subprocess.SubprocessError) as e:
        log.error("Signup issue poll failed: %s", e)
        return 1
    log.info("Processed %d signup issue(s)", n)
    return 0


if __name__ == "__main__":
    sys.exit(main())
