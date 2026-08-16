"""Add, change and remove reminder subscribers by hand.

Reading a signup mailbox automatically needs credentials that Google will
only issue behind 2FA, so subscriptions are maintained by whoever runs the
tracker instead. The site's "remind me" buttons open a mail to the
maintainer, and the subject line of that mail is *already* a valid command
here -- so handling a signup is copying one line:

    someone mails:  subscribe mobicom tracks:all
    you run:        scripts/subscribe.sh alice@illinois.edu subscribe mobicom tracks:all

Everything else about reminders is unchanged: the same store, the same
command language, the same daily send. Only the way a subscription arrives
is different, and setting CONFTRACKER_IMAP_USER later switches it back to
reading the mailbox without touching any of this.

    scripts/subscribe.sh --list                          who is subscribed
    scripts/subscribe.sh alice@illinois.edu subscribe tier1 tracks:all
    scripts/subscribe.sh alice@illinois.edu unsubscribe
    scripts/subscribe.sh alice@illinois.edu pause
"""

import argparse
import json
import logging
import sys

from . import config, mail, subscriptions

log = logging.getLogger(__name__)


def _print_list(store: dict, cat: list[dict]) -> None:
    people = store.get("subscribers", {})
    if not people:
        print("No subscribers yet.")
        return
    print(f"{len(people)} subscriber(s):\n")
    for email, sub in sorted(people.items()):
        what = ", ".join(subscriptions.describe(s, cat) for s in sub.get("selectors") or [])
        flags = []
        if sub.get("tracks") == "all":
            flags.append("workshops/posters included")
        if sub.get("paused"):
            flags.append("PAUSED")
        last = max((sub.get("sent") or {}).values(), default=None)
        print(f"  {email}")
        print(f"      venues:    {what or 'nothing'}")
        print(f"      reminders: {', '.join(str(d) for d in sub.get('lead_days') or [])} day(s) before"
              + (f"   [{'; '.join(flags)}]" if flags else ""))
        print(f"      last sent: {last or 'never'}")
        print()


def main() -> int:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    ap = argparse.ArgumentParser(
        description="Manage ConfTracker reminder subscribers by hand",
        epilog="Command words are the same ones the site's mailto links "
               "produce: subscribe / unsubscribe / pause / resume / list.",
    )
    ap.add_argument("email", nargs="?", help="the subscriber's address")
    ap.add_argument(
        "command",
        nargs=argparse.REMAINDER,
        help="e.g. subscribe tier1 tracks:all days:30,7,1",
    )
    ap.add_argument("--list", action="store_true", help="show every subscriber and exit")
    ap.add_argument(
        "--notify",
        action="store_true",
        help="also mail the subscriber what changed (needs a working relay)",
    )
    ap.add_argument(
        "--force",
        action="store_true",
        help=f"accept an address outside {config.SUBSCRIBE_DOMAINS or ['any domain']}",
    )
    args = ap.parse_args()

    try:
        data = json.loads(config.SITE_DATA.read_text())
    except (OSError, json.JSONDecodeError) as e:
        print(f"Cannot read {config.SITE_DATA}: {e}", file=sys.stderr)
        return 1
    cat = subscriptions.catalog(data)

    try:
        store = subscriptions.load()
    except RuntimeError as e:
        print(str(e), file=sys.stderr)
        return 1

    if args.list:
        _print_list(store, cat)
        return 0

    if not args.email or not args.command:
        ap.print_usage(sys.stderr)
        print("\nNeed an address and a command, or --list.", file=sys.stderr)
        return 2

    email = subscriptions.normalize_email(args.email)
    if "@" not in email:
        print(f"{args.email!r} does not look like an email address.", file=sys.stderr)
        return 2
    if not subscriptions.domain_allowed(email) and not args.force:
        allowed = ", ".join(config.SUBSCRIBE_DOMAINS)
        print(
            f"{email} is outside the allowed domains ({allowed}).\n"
            "Add it anyway with --force, or widen CONFTRACKER_SUBSCRIBE_DOMAINS.",
            file=sys.stderr,
        )
        return 2

    cmd = subscriptions.parse(" ".join(args.command), cat)
    if cmd.verb is None:
        print(
            "Command must start with one of: "
            + ", ".join(subscriptions.VERBS)
            + f"\nGot: {' '.join(args.command)!r}",
            file=sys.stderr,
        )
        return 2

    subject, body = subscriptions.apply(store, email, cmd, cat)
    subscriptions.save(store)

    print(f"{email}: {subject}\n")
    print(body)

    if args.notify:
        try:
            mail.send(subject, body, to=email, reply_to=config.SUBSCRIBE_ADDRESS or None)
            print(f"(mailed to {email})")
        except Exception as e:
            # The change is already saved; a failed courtesy mail is not a
            # reason to make the caller think it did not happen.
            print(f"WARNING: change saved, but could not mail {email}: {e}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
