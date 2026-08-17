"""Read the signup mailbox and turn what is in it into subscriptions.

This is the half of the reminder system that a static site cannot do on its
own. GitHub Pages has nowhere to POST a form, so the "remind me" buttons open
a prefilled email instead, and this polls that mailbox over IMAP, applies each
command, and replies with what it did.

    python -m scraper.inbox --stdout   # read and show, change nothing
    python -m scraper.inbox            # apply and acknowledge

Nothing here trusts the message beyond the address it came from: a command is
a fixed vocabulary of verbs and known venue names, and anything else in the
subject is reported back to the sender as ignored rather than acted on.
"""

import argparse
import email
import email.utils
import imaplib
import json
import logging
import sys
from email.header import decode_header, make_header

from . import config, mail, subscriptions

log = logging.getLogger(__name__)

# Headers that mark a message as machine-generated. Acting on these is how a
# mail loop starts: our own acknowledgement bounces, the bounce looks like a
# command, we reply to the bounce.
def _is_automated(msg) -> bool:
    auto = (msg.get("Auto-Submitted") or "no").strip().lower()
    if auto != "no":
        return True
    if (msg.get("Precedence") or "").strip().lower() in ("bulk", "list", "junk", "auto_reply"):
        return True
    return any(
        msg.get(h)
        for h in ("List-Id", "List-Unsubscribe", "X-Autoreply", "X-Autorespond")
    )


def _header(msg, name: str) -> str:
    raw = msg.get(name)
    if not raw:
        return ""
    try:
        return str(make_header(decode_header(raw)))
    except Exception:  # a malformed header must not stop the poll
        return str(raw)


def _body_text(msg) -> str:
    """First text/plain part, decoded as best we can."""
    parts = [msg] if not msg.is_multipart() else msg.walk()
    for part in parts:
        if part.get_content_type() != "text/plain":
            continue
        if "attachment" in (part.get("Content-Disposition") or ""):
            continue
        payload = part.get_payload(decode=True)
        if payload is None:
            continue
        charset = part.get_content_charset() or "utf-8"
        try:
            return payload.decode(charset, errors="replace")
        except LookupError:
            return payload.decode("utf-8", errors="replace")
    return ""


def _decline_body(sender: str) -> str:
    domains = ", ".join(config.SUBSCRIBE_DOMAINS)
    return (
        f"ConfTracker reminders are currently limited to {domains} addresses,\n"
        f"so {sender} has not been subscribed.\n\n"
        f"The tracker itself is public and needs no signup: {config.SITE_URL}\n"
    )


def poll(dry_run: bool = False, limit: int | None = None) -> int:
    """Process unread signup mail. Returns the number of messages acted on."""
    if not (config.IMAP_USER and config.IMAP_PASSWORD):
        raise RuntimeError(
            "No signup mailbox configured: set CONFTRACKER_IMAP_USER and "
            "CONFTRACKER_IMAP_PASSWORD (in .env) to the address the site "
            "advertises for reminder signups."
        )

    limit = limit or config.MAX_INBOX_MESSAGES
    # What can be subscribed to is whatever the site currently publishes.
    cat = subscriptions.catalog(json.loads(config.SITE_DATA.read_text()))
    store = subscriptions.load()

    ourselves = {
        subscriptions.normalize_email(a)
        for a in (config.SUBSCRIBE_ADDRESS, config.IMAP_USER, config.REPORT_FROM)
        if a
    }

    handled = 0
    with imaplib.IMAP4_SSL(config.IMAP_HOST, config.IMAP_PORT) as imap:
        imap.login(config.IMAP_USER, config.IMAP_PASSWORD)
        imap.select(config.IMAP_MAILBOX, readonly=dry_run)
        ok, result = imap.search(None, "UNSEEN")
        if ok != "OK":
            raise RuntimeError(f"IMAP search failed: {result!r}")
        ids = result[0].split()
        if len(ids) > limit:
            log.warning(
                "%d unread messages, processing the oldest %d this run",
                len(ids),
                limit,
            )
            ids = ids[:limit]
        log.info("%d unread message(s) in %s", len(ids), config.IMAP_MAILBOX)

        for num in ids:
            try:
                handled += _process_message(
                    imap, num, store, cat, ourselves, dry_run
                )
            except imaplib.IMAP4.abort:
                # The connection is gone; the rest of the run cannot work.
                raise
            except (imaplib.IMAP4.error, OSError, RuntimeError) as e:
                # One unreadable or malformed message must not cost every
                # message behind it its turn.
                log.error(
                    "message %s: skipped after an error: %s",
                    num.decode(errors="replace"), e,
                )

    return handled


def _process_message(imap, num, store, cat, ourselves, dry_run) -> int:
    """Handle one unread message. Returns 1 if it was acted on, else 0."""
    # PEEK so that a crash mid-message leaves it unread and retriable.
    ok, fetched = imap.fetch(num, "(BODY.PEEK[])")
    if ok != "OK" or not fetched or not isinstance(fetched[0], tuple):
        log.warning("could not fetch message %s", num.decode(errors="replace"))
        return 0
    msg = email.message_from_bytes(fetched[0][1])

    subject = _header(msg, "Subject")
    sender = subscriptions.normalize_email(
        email.utils.parseaddr(msg.get("From", ""))[1]
    )

    # Nothing goes out for these two, so flagging them is the whole action
    # and a failure to flag costs only a re-read on the next poll.
    if not sender or sender in ourselves:
        log.info("skipping message from %r (no sender, or ourselves)", sender)
        _mark_seen(imap, num, dry_run)
        return 0
    if _is_automated(msg):
        log.info("skipping automated message from %s (%r)", sender, subject)
        _mark_seen(imap, num, dry_run)
        return 0

    if not subscriptions.domain_allowed(sender):
        log.info("declining signup from %s (outside allowed domains)", sender)
        if dry_run:
            print(f"--- decline {sender}: {subject!r}")
            return 1
        # Flag first: a decline we cannot flag is a decline this poll would
        # send again in half an hour, and again after that.
        if not _mark_seen(imap, num, dry_run):
            return 0
        _reply(
            sender,
            "ConfTracker reminders are not open to this address",
            _decline_body(sender),
        )
        return 1

    cmd = subscriptions.parse_message(subject, _body_text(msg), cat)
    reply_subject, reply_body = subscriptions.apply(store, sender, cmd, cat)
    log.info("%s: %s -> %s", sender, subject or "(no subject)", reply_subject)

    if dry_run:
        print(f"--- from: {sender}   subject: {subject!r}")
        print(f"would reply: {reply_subject}")
        print(reply_body)
        print()
        return 0

    # Save before anything else: if the flag or the reply fails next, the
    # subscription should still stand rather than be silently lost.
    subscriptions.save(store)

    # Then flag, and only acknowledge once it stuck. The command is already
    # applied and saved by this point, so a message we cannot flag costs the
    # sender an acknowledgement -- weighed against another copy of that
    # acknowledgement every half hour for as long as the flag keeps failing.
    if not _mark_seen(imap, num, dry_run):
        return 0
    _reply(sender, reply_subject, reply_body)
    return 1


def _mark_seen(imap, num: bytes, dry_run: bool) -> bool:
    """Flag the message read. False if the server did not take it.

    Returning something the caller has to look at is the point. imaplib
    raises on a BAD response but hands back a plain ('NO', ...) otherwise,
    which is easy to drop on the floor -- and a STORE that keeps being
    refused leaves the message unread forever, so every poll sees it as new.
    Left unread on a dry run too, and on a crash, which is why the fetch
    above peeks instead of reading.
    """
    if dry_run:
        return False
    typ, data = imap.store(num, "+FLAGS", "\\Seen")
    if typ != "OK":
        log.error(
            "could not flag message %s as read (%s %r); leaving it untouched "
            "rather than answering it again next poll",
            num.decode(errors="replace"), typ, data,
        )
        return False
    return True


def _reply(to: str, subject: str, body: str) -> None:
    try:
        mail.send(subject, body, to=to, reply_to=config.SUBSCRIBE_ADDRESS)
    except Exception as e:
        # The command has already been applied; a failed acknowledgement is
        # worth a log line, not a lost subscription.
        log.error("could not acknowledge %s: %s", to, e)


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    ap = argparse.ArgumentParser(description="Process ConfTracker signup mail")
    ap.add_argument(
        "--stdout",
        "--dry-run",
        dest="dry_run",
        action="store_true",
        help="show what would happen; do not reply, save, or mark as read",
    )
    ap.add_argument("--limit", type=int, help="max messages this run")
    args = ap.parse_args()

    try:
        n = poll(dry_run=args.dry_run, limit=args.limit)
    except (RuntimeError, imaplib.IMAP4.error, OSError) as e:
        log.error("Signup mailbox poll failed: %s", e)
        return 1
    log.info("Processed %d signup message(s)", n)
    return 0


if __name__ == "__main__":
    sys.exit(main())
