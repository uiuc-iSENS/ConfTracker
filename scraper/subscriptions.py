"""Who wants reminders about what, and the command language they use to say so.

The site is static (GitHub Pages), so there is no server to POST a signup
form to. Instead the "remind me" buttons open a prefilled email -- the whole
subscription is in the subject line -- and scraper.inbox feeds those subjects
through here. A request arriving from an address is proof enough that the
sender owns it, so there is no confirmation round-trip to build, no inbound
port to open, and no third-party form service holding lab email addresses.

The command language is deliberately forgiving, because the request is a
human-editable email and people will edit it:

    subscribe mobicom sensys        one or more venues
    subscribe all                   every venue we track
    subscribe tier1 tag:sensing     by lab tier or topic tag
    subscribe mobicom tracks:all    include workshops/posters/demos
    subscribe all days:30,7,1       pick your own reminder lead times
    unsubscribe mobicom             drop one selection
    unsubscribe                     remove the address entirely
    pause / resume                  keep the selection, stop the mail
    list                            reply with the current subscription
    help                            reply with this syntax
"""

import email.utils
import json
import logging
import re
from datetime import datetime, timezone

from . import config

log = logging.getLogger(__name__)

VERBS = ("subscribe", "unsubscribe", "list", "status", "pause", "resume", "help")

# Anything a subscriber can point at. "*" is every venue.
EVERYTHING = "*"

MAX_LEAD_DAYS = 365
MAX_SELECTORS = 60  # a subscription, not a scrape target list


# ---------- store ----------

def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load() -> dict:
    """The subscriber store, or an empty one if it does not exist yet."""
    try:
        store = json.loads(config.SUBSCRIBERS.read_text())
    except FileNotFoundError:
        return {"version": 1, "subscribers": {}}
    except (OSError, json.JSONDecodeError) as e:
        # Never silently start from empty: that would quietly unsubscribe
        # everyone and the next save would overwrite the only copy.
        raise RuntimeError(f"subscriber store at {config.SUBSCRIBERS} is unreadable: {e}")
    store.setdefault("subscribers", {})
    return store


def save(store: dict) -> None:
    """Write the store back, atomically -- a torn write loses subscribers."""
    # 0700 on the directory as well as 0600 on the file: the repo checkout
    # itself is group-writable on the lab machine, and a readable directory
    # would let anyone list (or replace) the subscriber file regardless of
    # its own permissions.
    config.SUBSCRIBERS.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = config.SUBSCRIBERS.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(store, indent=2, sort_keys=True))
    # Restrict before the rename, not after: the file holds personal data on
    # a shared lab machine, and a window where it is world-readable is still
    # a window.
    try:
        tmp.chmod(0o600)
    except OSError as e:
        log.warning("Could not restrict permissions on the subscriber store: %s", e)
    tmp.replace(config.SUBSCRIBERS)


def normalize_email(addr: str) -> str:
    """The address alone, lowercased -- 'Ming <MT55@Illinois.edu>' included.

    This is the key everything else hangs off, so it has to survive a header
    with a display name in it; otherwise the same person subscribing twice
    from two mail clients becomes two subscribers, and only one of them can
    unsubscribe.
    """
    parsed = email.utils.parseaddr(addr or "")[1]
    return (parsed or addr or "").strip().strip("<>").lower()


def domain_allowed(addr: str) -> bool:
    """Policy gate on who may sign up (not an ownership check -- see above)."""
    if not config.SUBSCRIBE_DOMAINS:
        return True  # signups open to anyone
    domain = normalize_email(addr).rpartition("@")[2]
    return any(
        domain == d or domain.endswith("." + d) for d in config.SUBSCRIBE_DOMAINS
    )


def _blank(email: str) -> dict:
    return {
        "email": email,
        "created_at": _now(),
        "updated_at": _now(),
        "selectors": [],
        "tracks": "main",
        "lead_days": list(config.DEFAULT_LEAD_DAYS),
        "paused": False,
        "sent": {},
    }


# ---------- catalogue of what can be subscribed to ----------

def venue_key(title: str) -> str:
    """'UbiComp/ISWC' -> 'ubicomp-iswc'. Stable id for a venue in a command."""
    return re.sub(r"[^a-z0-9]+", "-", (title or "").lower()).strip("-")


def catalog(data: dict) -> list[dict]:
    """Subscribable venues, from a loaded docs/data.json."""
    out = []
    for conf in data.get("conferences") or []:
        isens = conf.get("isens") or {}
        out.append(
            {
                "key": venue_key(conf.get("title", "")),
                "title": conf.get("title", ""),
                "tier": str(isens.get("tier") or 3),
                "tags": [str(t).lower() for t in isens.get("tags") or []],
            }
        )
    return [c for c in out if c["key"]]


def resolve(token: str, cat: list[dict]) -> str | None:
    """One typed word -> a selector, or None if it matches nothing.

    Accepts what people actually type: the venue acronym in any case, a tier
    as `tier1` or `tier:1`, a topic as `tag:sensing` or bare `sensing`, and a
    unique prefix ('ubicomp' for UbiComp/ISWC).
    """
    token = token.strip().lower().rstrip(",;")
    if not token:
        return None
    if token in ("all", "everything", "*", "any"):
        return EVERYTHING

    m = re.fullmatch(r"tier[:\-]?([123])", token)
    if m:
        return f"tier:{m.group(1)}"

    known_tags = {t for c in cat for t in c["tags"]}
    if token.startswith("tag:"):
        tag = token[4:]
        return f"tag:{tag}" if tag in known_tags else None
    if token in known_tags:
        return f"tag:{token}"

    keys = {c["key"] for c in cat}
    if token.startswith("venue:"):
        token = token[6:]
    if token in keys:
        return f"venue:{token}"
    # A unique prefix is enough; an ambiguous one is not resolved rather than
    # guessed, and comes back to the sender as unrecognised.
    hits = sorted(k for k in keys if k.startswith(token))
    if len(hits) == 1:
        return f"venue:{hits[0]}"
    # 'iswc' should still find 'ubicomp-iswc'.
    hits = sorted(k for k in keys if token in k.split("-"))
    return f"venue:{hits[0]}" if len(hits) == 1 else None


def describe(selector: str, cat: list[dict]) -> str:
    if selector == EVERYTHING:
        return "every venue"
    kind, _, value = selector.partition(":")
    if kind == "tier":
        return f"tier {value}"
    if kind == "tag":
        return f"#{value}"
    title = next((c["title"] for c in cat if c["key"] == value), value)
    return title


def matches(sub: dict, conf: dict) -> bool:
    """Does this conference fall under the subscriber's selectors?"""
    key = venue_key(conf.get("title", ""))
    isens = conf.get("isens") or {}
    tier = str(isens.get("tier") or 3)
    tags = {str(t).lower() for t in isens.get("tags") or []}
    for selector in sub.get("selectors") or []:
        if selector == EVERYTHING:
            return True
        kind, _, value = selector.partition(":")
        if kind == "venue" and value == key:
            return True
        if kind == "tier" and value == tier:
            return True
        if kind == "tag" and value in tags:
            return True
    return False


# ---------- command parsing ----------

class Command:
    def __init__(self):
        self.verb: str | None = None
        self.selectors: list[str] = []
        self.tracks: str | None = None
        self.lead_days: list[int] | None = None
        self.unknown: list[str] = []


def _clean(text: str) -> str:
    """Strip the decoration a mail client adds around the command."""
    text = re.sub(r"^\s*(re|fwd|fw)\s*:\s*", "", text or "", flags=re.I)
    return text.replace(" ", " ").strip()


def parse(text: str, cat: list[dict]) -> Command:
    cmd = Command()
    for word in _clean(text).split():
        low = word.lower().strip(".,;:")
        if cmd.verb is None:
            # The verb has to come first; anything before it is noise from a
            # mail client ("Fwd:", a name) and is skipped rather than
            # reported as unrecognised.
            if low in VERBS:
                cmd.verb = "status" if low == "list" else low
            continue

        if low.startswith("tracks:"):
            value = low[7:]
            if value in ("all", "main"):
                cmd.tracks = value
            else:
                cmd.unknown.append(word)
            continue

        if low.startswith("days:"):
            days = []
            for part in low[5:].split(","):
                if part.isdigit() and 0 < int(part) <= MAX_LEAD_DAYS:
                    days.append(int(part))
            if days:
                # Descending, deduped: reminders go out longest-lead first.
                cmd.lead_days = sorted(set(days), reverse=True)
            else:
                cmd.unknown.append(word)
            continue

        selector = resolve(low, cat)
        if selector is None:
            cmd.unknown.append(word)
        elif selector not in cmd.selectors:
            cmd.selectors.append(selector)
    return cmd


def parse_message(subject: str, body: str, cat: list[dict]) -> Command:
    """Read the command from the subject, falling back to the body.

    The buttons on the site put everything in the subject, but people reply
    to a reminder and type "unsubscribe" in the body, and that should work.
    """
    cmd = parse(subject, cat)
    if cmd.verb is not None:
        return cmd
    for line in (body or "").splitlines()[:20]:
        line = line.strip()
        # Stop at a quoted reply, or "unsubscribe" in the footer we sent them
        # would read as a command every time somebody replies to say thanks.
        if not line or line.startswith((">", "--", "On ", "From:")):
            break
        cmd = parse(line, cat)
        if cmd.verb is not None:
            return cmd
    return Command()


# ---------- applying a command ----------

def apply(store: dict, sender: str, cmd: Command, cat: list[dict]) -> tuple[str, str]:
    """Run a parsed command against the store. Returns (subject, body) to reply.

    The caller saves the store; nothing here writes to disk.
    """
    email = normalize_email(sender)
    subs = store.setdefault("subscribers", {})
    sub = subs.get(email)

    if cmd.verb == "help" or cmd.verb is None:
        return "ConfTracker reminders -- how to subscribe", _help_body(email, sub, cat)

    if cmd.verb == "status":
        return "Your ConfTracker reminder subscription", _status_body(email, sub, cat)

    if cmd.verb in ("pause", "resume"):
        if sub is None:
            return (
                "You have no ConfTracker reminders set up",
                "There is nothing to "
                f"{cmd.verb}: {email} is not subscribed.\n\n" + _syntax(),
            )
        sub["paused"] = cmd.verb == "pause"
        sub["updated_at"] = _now()
        state = "paused" if sub["paused"] else "resumed"
        return (
            f"ConfTracker reminders {state}",
            f"Reminders are {state} for {email}. Your selection is unchanged:\n\n"
            + _status_body(email, sub, cat),
        )

    if cmd.verb == "unsubscribe":
        if sub is None:
            return (
                "You have no ConfTracker reminders set up",
                f"{email} was not subscribed, so there was nothing to remove.",
            )
        if not cmd.selectors:
            subs.pop(email, None)
            return (
                "Unsubscribed from ConfTracker reminders",
                f"Removed {email} completely -- no further reminder mail will "
                "be sent, and the address is deleted from the subscriber "
                "list.\n\nTo start again, " + channel_hint() + "\n",
            )
        if EVERYTHING in sub["selectors"] and EVERYTHING not in cmd.selectors:
            # "every venue except MobiCom" is not expressible, and quietly
            # doing nothing would look like the request was honoured.
            return (
                "ConfTracker reminders unchanged",
                f"{email} is subscribed to every venue, so there is no single "
                "venue to remove.\n\nEither name the venues you do want, "
                "which replaces the selection:\n\n"
                "  subscribe mobicom sensys ubicomp\n\n"
                "or send 'unsubscribe' with nothing after it to stop "
                "reminders altogether.\n",
            )
        kept = [s for s in sub["selectors"] if s not in cmd.selectors]
        dropped = [s for s in sub["selectors"] if s in cmd.selectors]
        if not kept:
            subs.pop(email, None)
            return (
                "Unsubscribed from ConfTracker reminders",
                "That was everything you had selected, so "
                f"{email} has been removed from the list entirely.",
            )
        sub["selectors"] = kept
        sub["updated_at"] = _now()
        gone = ", ".join(describe(s, cat) for s in dropped) or "nothing"
        return (
            "ConfTracker reminder subscription updated",
            f"Stopped reminders for: {gone}\n\n" + _status_body(email, sub, cat),
        )

    # subscribe
    if sub is None:
        sub = _blank(email)
        subs[email] = sub
        new = True
    else:
        new = False

    # Naming specific venues while subscribed to everything is a request to
    # narrow down, not a no-op: merging would leave "*" in place and appear
    # to ignore the message.
    if (
        sub["selectors"] == [EVERYTHING]
        and cmd.selectors
        and EVERYTHING not in cmd.selectors
    ):
        sub["selectors"] = []

    for selector in cmd.selectors:
        if selector not in sub["selectors"]:
            sub["selectors"].append(selector)
    # "subscribe all" makes every narrower selector redundant.
    if EVERYTHING in sub["selectors"]:
        sub["selectors"] = [EVERYTHING]
    del sub["selectors"][MAX_SELECTORS:]

    if cmd.tracks:
        sub["tracks"] = cmd.tracks
    if cmd.lead_days:
        sub["lead_days"] = cmd.lead_days
    sub["paused"] = False
    sub["updated_at"] = _now()

    if not sub["selectors"]:
        # "subscribe" with nothing usable after it: do not guess at "all".
        subs.pop(email, None)
        return (
            "ConfTracker reminders -- which venues?",
            "Your message did not name any venue we track"
            + (f" (unrecognised: {' '.join(cmd.unknown)})" if cmd.unknown else "")
            + ".\n\nNothing has been subscribed. Reply with one of these:\n\n"
            + _syntax()
            + "\n"
            + _venue_list(cat),
        )

    head = "You are subscribed to ConfTracker reminders" if new else (
        "ConfTracker reminder subscription updated"
    )
    body = _status_body(email, sub, cat)
    if cmd.unknown:
        body += (
            "\nNot recognised, and therefore ignored: "
            + " ".join(cmd.unknown)
            + "\nMail 'help' for the syntax, or 'list' to see this again.\n"
        )
    return head, body


# ---------- reply bodies ----------

def channel_hint() -> str:
    """Where a reader should send a command, in the channel actually running.

    The same reply text goes out whether signups arrive as GitHub issues or
    as email, and telling someone to mail an address that nobody reads is
    worse than telling them nothing.
    """
    if config.SIGNUP_REPO:
        return (
            "open an issue with the command as its title:\n"
            f"  https://github.com/{config.SIGNUP_REPO}/issues/new"
        )
    if config.SUBSCRIBE_ADDRESS:
        return f"mail {config.SUBSCRIBE_ADDRESS} with the command in the subject line"
    return "ask whoever runs the tracker"


def _syntax() -> str:
    return (
        "  subscribe mobicom sensys      one or more venues\n"
        "  subscribe all                 every venue tracked\n"
        "  subscribe tier1               everything at a lab tier\n"
        "  subscribe tag:sensing         everything with a topic tag\n"
        "  subscribe all tracks:all      include workshops/posters/demos\n"
        "  subscribe all days:30,7,1     choose your own lead times\n"
        "  unsubscribe mobicom           drop one selection\n"
        "  unsubscribe                   remove your address entirely\n"
        "  pause / resume                keep the selection, hold the mail\n"
        "  list                          show your current subscription\n"
        "\nTo change any of it, " + channel_hint() + "\n"
    )


def _venue_list(cat: list[dict]) -> str:
    names = " ".join(sorted(c["key"] for c in cat))
    return f"Venues you can name:\n  {names}\n"


def _status_body(email: str, sub: dict | None, cat: list[dict]) -> str:
    if sub is None:
        return (
            f"{email} is not subscribed to any ConfTracker reminders.\n\n"
            + _syntax()
            + "\n"
            + _venue_list(cat)
        )
    what = ", ".join(describe(s, cat) for s in sub["selectors"]) or "nothing"
    tracks = (
        "paper deadlines only"
        if sub.get("tracks") == "main"
        else "paper deadlines plus workshops, posters and demos"
    )
    days = ", ".join(str(d) for d in sub.get("lead_days") or [])
    lines = [
        f"Subscription for {email}:",
        "",
        f"  venues:     {what}",
        f"  covering:   {tracks}",
        f"  reminders:  {days} day(s) before each deadline",
        f"  status:     {'PAUSED -- no mail until you send resume' if sub.get('paused') else 'active'}",
        "",
        f"Site: {config.SITE_URL}",
        "",
        "The full set of commands:",
        "",
        _syntax(),
    ]
    return "\n".join(lines)


def _help_body(email: str, sub: dict | None, cat: list[dict]) -> str:
    return (
        "ConfTracker mails you before the deadlines you care about.\n\n"
        + _syntax()
        + "\n"
        + _venue_list(cat)
        + "\n"
        + _status_body(email, sub, cat)
    )
