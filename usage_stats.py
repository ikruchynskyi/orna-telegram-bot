"""
usage_stats.py
================
Lightweight usage counters: how many times each slash command was
invoked (overall and per user), how many LLM calls were made per model
(tagged local/cloud), and - per user - a capped log of their most recent
questions. Persisted to usage_stats.json (gitignored) so counts survive
this bot's frequent launchctl reloads - same reasoning as
telegram_remind.py's reminders.json.

Only slash commands are counted as "questions to the bot" today, not
the free-text conversation entry points in telegram_assess.py/
telegram_resources.py - those cover most real usage already, and adding
the rest is a straightforward follow-up if the totals here turn out to
undercount noticeably.
"""
from __future__ import annotations

import json
import logging
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

_STORE_PATH = Path(__file__).parent / "usage_stats.json"

MAX_LOG_PER_USER = 40
# Bug reports kept per user. The 11th drops the oldest, so a single user
# cannot flood the store, and the cap is per-user rather than global so one
# noisy reporter can't push everyone else's reports out.
MAX_REPORTS_PER_USER = 10

_commands: Counter = Counter()
_llm_calls: Counter = Counter()  # keyed by "model (local|cloud)"
_orna_tools: Counter = Counter()  # keyed by /orna ReAct loop action name
_user_commands: Dict[str, Counter] = defaultdict(Counter)  # user_id str -> Counter[command]
_user_names: Dict[str, str] = {}  # user_id str -> last-seen display name
_user_log: Dict[str, List[dict]] = defaultdict(list)  # user_id str -> [{command,text,ts}, ...], newest last
_user_reports: Dict[str, List[dict]] = defaultdict(list)  # user_id str -> [{text,ts}, ...], newest last
_user_tz: Dict[str, float] = {}  # user_id str -> UTC offset in hours, from telegram_remind.request_utc_offset
# user_id str -> IANA zone name ("Europe/Kyiv"), when the user gave a PLACE
# rather than a bare offset. Preferred over _user_tz whenever present: a
# stored NUMBER freezes at whatever the offset was on the day it was picked,
# so a user in any DST region silently drifts an hour twice a year with
# nothing to signal it. A zone is re-evaluated on every read instead.
_user_zone: Dict[str, str] = {}
# user_id str -> {"reason", "ts", "by"}. A MODERATION decision, not a
# statistic: persisted like everything else here so it survives the frequent
# launchctl reloads, and deliberately NOT cleared by reset() - see its
# docstring. Enforced in one place, telegram_bot's pre-dispatch guard.
_banned: Dict[str, dict] = {}
_since: str = datetime.now(timezone.utc).isoformat(timespec="seconds")


def _load() -> None:
    global _since
    if not _STORE_PATH.exists():
        return
    try:
        data = json.loads(_STORE_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        logger.warning("usage_stats: couldn't read %s, starting fresh", _STORE_PATH, exc_info=True)
        return
    _commands.update(data.get("commands", {}))
    _llm_calls.update(data.get("llm_calls", {}))
    _orna_tools.update(data.get("orna_tools", {}))
    for uid, counts in data.get("user_commands", {}).items():
        _user_commands[uid].update(counts)
    _user_names.update(data.get("user_names", {}))
    for uid, log in data.get("user_log", {}).items():
        _user_log[uid] = log
    for uid, reports in (data.get("user_reports") or {}).items():
        _user_reports[uid] = reports
    _user_tz.update(data.get("user_tz", {}))
    _user_zone.update(data.get("user_zone", {}))
    _banned.update(data.get("banned", {}))
    _since = data.get("since", _since)


def _save() -> None:
    # Atomic write (temp file + os.replace, same directory) - same reasoning
    # as telegram_remind.py's _save: a plain write_text left truncated by a
    # crash/reload mid-write would make _load() silently start fresh,
    # losing all history instead of just failing to record one entry.
    try:
        tmp = _STORE_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({
            "since": _since,
            "commands": dict(_commands),
            "llm_calls": dict(_llm_calls),
            "orna_tools": dict(_orna_tools),
            "user_commands": {uid: dict(c) for uid, c in _user_commands.items()},
            "user_names": _user_names,
            "user_log": _user_log,
            "user_reports": _user_reports,
            "user_tz": _user_tz,
            "user_zone": _user_zone,
            "banned": _banned,
        }))
        tmp.replace(_STORE_PATH)
    except OSError:
        logger.warning("usage_stats: couldn't write %s", _STORE_PATH, exc_info=True)


_load()


def _display_name(username: Optional[str], first_name: Optional[str]) -> str:
    if username:
        return f"@{username}"
    return first_name or "?"


def record_command(
    name: str,
    user_id: Optional[int] = None,
    username: Optional[str] = None,
    first_name: Optional[str] = None,
    text: str = "",
) -> None:
    """Call once at the top of a slash command's handler. `user_id` +
    `username`/`first_name` come straight from update.effective_user;
    `text` is whatever the user actually typed after the command (so
    "last 40 questions" shows real content, not just the command name
    repeated 40 times)."""
    _commands[name] += 1
    if user_id is not None:
        uid = str(user_id)
        _user_commands[uid][name] += 1
        _user_names[uid] = _display_name(username, first_name)
        log = _user_log[uid]
        log.append({
            "command": name,
            "text": text[:200],
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        })
        del log[:-MAX_LOG_PER_USER]
    _save()


def record_command_for(update, name: str, text: str = "") -> None:
    """Convenience wrapper: pulls user_id/username/first_name straight
    out of a telegram.Update, so call sites don't each have to repeat
    "user.id if user else None" three times over."""
    user = getattr(update, "effective_user", None)
    record_command(
        name,
        user.id if user else None,
        getattr(user, "username", None),
        getattr(user, "first_name", None),
        text,
    )


def record_report(user_id, username: Optional[str], first_name: Optional[str], text: str) -> dict:
    """Store one bug report and return it (with the reporter's display name),
    so the caller can forward it to the admins without re-deriving that."""
    uid = str(user_id)
    entry = {
        "text": text[:1000],
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    _user_names[uid] = _display_name(username, first_name)
    reports = _user_reports[uid]
    reports.append(entry)
    del reports[:-MAX_REPORTS_PER_USER]   # 11th drops the 1st
    _save()
    return {**entry, "user_id": uid, "display_name": _user_names[uid], "count": len(reports)}


def all_reports() -> list:
    """Every stored report, newest first, as {user_id, display_name, text, ts}."""
    out = []
    for uid, reports in _user_reports.items():
        # Newest-first WITHIN a user before the sort: timestamps have
        # second resolution, so several reports typed in the same second
        # compare equal and a stable sort would otherwise leave them
        # oldest-first inside an otherwise newest-first list.
        for entry in reversed(reports):
            out.append({"user_id": uid, "display_name": _user_names.get(uid, uid), **entry})
    out.sort(key=lambda r: r["ts"], reverse=True)
    return out


def reset(include_reports: bool = False) -> dict:
    """Wipe the usage statistics and start counting from now.

    Deliberately does NOT touch two things that live in the same store but
    are not statistics:
      * saved timezones (_user_tz/_user_zone) - clearing them would silently
        make every member re-pick their zone before their next reminder
        could be scheduled, which is a worse outcome than stale counters;
      * bug reports, unless `include_reports` - an unread report is work
        waiting to be done, not a number;
      * bans (_banned) - a moderation decision, never a statistic. Clearing
        the counters must not quietly readmit every spammer who was blocked.
    Returns what was cleared, so the caller can say so rather than just
    claiming success."""
    global _since
    cleared = {
        "commands": sum(_commands.values()),
        "llm_calls": sum(_llm_calls.values()),
        "orna_tools": sum(_orna_tools.values()),
        "users": len(_user_commands),
        "reports": sum(len(v) for v in _user_reports.values()) if include_reports else 0,
    }
    _commands.clear()
    _llm_calls.clear()
    _orna_tools.clear()
    _user_commands.clear()
    _user_log.clear()
    if include_reports:
        _user_reports.clear()
    # Keep _user_names: it is the id -> display-name map that makes a future
    # /stats users readable, and it is not a counter.
    _since = datetime.now(timezone.utc).isoformat(timespec="seconds")
    _save()
    return cleared


def record_llm_call(model: str, backend: str) -> None:
    """Call once per LLM API call actually made. `backend` is "local" or
    "cloud" - the two are meaningfully different (cost, latency,
    capability), and the raw model name alone doesn't say which, so
    that's part of the counter key rather than something you'd have to
    already know to interpret it."""
    _llm_calls[f"{model} ({backend})"] += 1
    _save()


def record_tool_call(action: str) -> None:
    """Call once per /orna ReAct loop tool dispatch (including "ask"/
    "finish", and a dedicated "_step_budget_exhausted" entry when the loop
    gives up without finishing) - the loop is new and more complex than
    what it replaced, and this is the at-a-glance signal for whether it's
    behaving (which tools actually get used, how often it needs to ask,
    how often it runs out of steps) instead of manually digging through
    logs after every live report."""
    _orna_tools[action] += 1
    _save()


def get_user_tz(user_id) -> Optional[float]:
    """UTC offset in hours (e.g. -5.0, 11.0) the user previously chose via
    telegram_remind.request_utc_offset, when a reminder needed to resolve
    an ABSOLUTE clock time ("/remind 18:30 ...", a guild "remind me"
    button) to an actual moment - None if never asked/answered. A
    DURATION-based reminder ("/remind 2h ...") never calls this at all,
    since a relative delay needs no timezone."""
    zone = _user_zone.get(str(user_id))
    if zone:
        try:
            from zoneinfo import ZoneInfo
            off = datetime.now(ZoneInfo(zone)).utcoffset()
            if off is not None:
                return off.total_seconds() / 3600
        except Exception:  # unknown/renamed zone in a future tzdata - fall through
            logger.warning("usage_stats: stored zone %r no longer resolves, using saved offset", zone)
    v = _user_tz.get(str(user_id))
    return float(v) if v is not None else None


def get_user_zone(user_id) -> Optional[str]:
    """The IANA zone name the user's offset came from, if they gave a place."""
    return _user_zone.get(str(user_id))


def set_user_tz(user_id, utc_offset: float, zone: Optional[str] = None) -> None:
    """Record the user's timezone. `zone` (an IANA name) is stored alongside
    the offset whenever we have one, and wins on read - see _user_zone."""
    _user_tz[str(user_id)] = float(utc_offset)
    if zone:
        _user_zone[str(user_id)] = zone
    else:
        _user_zone.pop(str(user_id), None)
    _save()


def snapshot() -> Dict:
    return {
        "since": _since,
        "commands": dict(_commands),
        "llm_calls": dict(_llm_calls),
        "orna_tools": dict(_orna_tools),
        "user_count": len(_user_commands),
    }


def user_summary() -> List[tuple]:
    """[(user_id, display_name, total_commands), ...] sorted by total desc."""
    return sorted(
        (
            (uid, _user_names.get(uid, uid), sum(counter.values()))
            for uid, counter in _user_commands.items()
        ),
        key=lambda t: -t[2],
    )


def user_detail(user_id) -> Optional[Dict]:
    uid = str(user_id)
    if uid not in _user_commands:
        return None
    return {
        "user_id": uid,
        "display_name": _user_names.get(uid, uid),
        "commands": dict(_user_commands[uid]),
        "recent": list(_user_log.get(uid, [])),
    }


def is_banned(user_id) -> bool:
    """Hot path: called once per incoming update, so a plain dict lookup."""
    return str(user_id) in _banned


def ban(user_id, reason: str = "", by=None) -> bool:
    """Block `user_id`. False if they were already banned (so the caller can
    say "already banned" rather than claiming it did something)."""
    uid = str(user_id)
    if uid in _banned:
        return False
    _banned[uid] = {"reason": reason.strip(),
                    "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "by": str(by) if by is not None else ""}
    _save()
    return True


def unban(user_id) -> bool:
    """Unblock `user_id`. False if they were not banned."""
    uid = str(user_id)
    if _banned.pop(uid, None) is None:
        return False
    _save()
    return True


def banned_users() -> List[tuple]:
    """[(user_id, display_name_or_empty, info)], newest ban first."""
    rows = [(uid, _user_names.get(uid, ""), info) for uid, info in _banned.items()]
    rows.sort(key=lambda r: r[2].get("ts", ""), reverse=True)
    return rows


def find_user(query: str) -> Optional[str]:
    """Resolve a /stats argument (a numeric id, or a "@username"/bare
    username) to a known user_id, or None if nothing matches."""
    query = query.strip().lstrip("@")
    if query in _user_commands:
        return query
    for uid, name in _user_names.items():
        if name.lstrip("@").lower() == query.lower():
            return uid
    return None
