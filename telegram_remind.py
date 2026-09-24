"""
telegram_remind.py
================
Hidden `/remind` command, alongside `/go` - not registered in any command
menu, gated to the same GO_ALLOWED_USER_IDS.

Schedules a one-off reminder via PTB's JobQueue. JobQueue is in-memory
only, and this repo's `/go` work involves frequent `launchctl` reloads
during development - a reminder that silently vanished on the next reload
would be worse than no reminder feature at all. So every reminder is also
persisted to reminders.json (gitignored) and re-armed on startup by
reschedule_pending(); one found overdue (bot was down when it should have
fired) fires immediately with a note rather than being lost.

Two input shapes, two different timezone stories - a live question
("user in Australia asks for a reminder in 2 hours, but the server runs
in NYC - won't 2 hours from now already be past?"): no, a DURATION
("/remind 2h ...") is computed as datetime.now() + timedelta(...) - a
plain elapsed delay, identical real-world wait regardless of where the
server or the user is, no timezone involved at all. Only an ABSOLUTE
clock time ("/remind 18:30 ...", and the guild "remind me" buttons'
fixed 00:05-on-a-date target in telegram_resources.py) is genuinely
timezone-dependent - "18:30" means nothing without knowing whose 18:30.
For that case only, the user is asked once (buttons, never free text -
see request_utc_offset) which UTC offset to use, and the answer is
persisted in usage_stats.json (same store /stats reads) keyed by user_id
so later specific-time asks - here and in telegram_resources.py's guild
buttons - never ask again.
"""
from __future__ import annotations

import json
import logging
import re
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from pathlib import Path
from typing import Awaitable, Callable, Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import TelegramError
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler,
                          filters)

from ollama_client import OllamaError, chat_json
from telegram_go import GO_ALLOWED_USER_IDS
from telegram_nlp import OLLAMA_HOST, OLLAMA_MODEL
import usage_stats

logger = logging.getLogger(__name__)

_STORE_PATH = Path(__file__).parent / "reminders.json"

_UNIT_SECONDS = {
    "s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1,
    "m": 60, "min": 60, "mins": 60, "minute": 60, "minutes": 60,
    "h": 3600, "hr": 3600, "hrs": 3600, "hour": 3600, "hours": 3600,
    "d": 86400, "day": 86400, "days": 86400,
}
_DURATION_RE = re.compile(r"^(\d+)\s*([a-z]+)\s+(.+)$", re.IGNORECASE | re.DOTALL)
_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)\s+(.+)$", re.DOTALL)

# The offset is asked as FREE TEXT ("+3", "Київ", "New York"), not picked
# from a grid. The grid was 24-27 buttons that Telegram clipped on a phone
# until the labels were cut to 3 chars (see git history) - and even correct,
# it made the user hunt through a 6-row wall for one cell, and froze a NUMBER
# that silently drifts an hour every DST change. A place instead resolves to
# an IANA zone, which stays right forever (usage_stats stores the zone and
# re-reads it live) and costs one line of UI.
#
# Free text in this codebase is not free, though: a bare MessageHandler would
# race the assess/resources ConversationHandlers, whose registration order is
# load-bearing (see CLAUDE.md). Same solution /go's "Continue" uses - a
# custom MessageFilter that matches ONLY a chat with a live pending ask, so
# for every other chat it is a guaranteed no-op that falls straight through.
_PENDING_TZ: dict = {}  # pending_id -> {"user_id", "chat_id", "on_offset", "candidate"} - in-memory, like _REMINDER_STATE
_PENDING_TZ_MAX = 200
_PENDING_TZ_INPUT: dict = {}  # chat_id -> (pending_id, expires_monotonic)
TZ_INPUT_TTL_SECONDS = 600

# "+3", "-5", "3", "utc+3", "UTC +5:30", "+5.5" - the deterministic path,
# tried before the model is ever asked. A bare number is read as an offset
# only within the real range; "Dublin 8" style input falls through to the
# location resolver instead of being misread as UTC+8.
_OFFSET_RE = re.compile(r"^\s*(?:utc|гмт|utc\s*)?\s*([+-]?\d{1,2})(?:[:.](\d{1,2}))?\s*$", re.IGNORECASE)


def _parse_offset_text(text: str) -> Optional[float]:
    """A typed UTC offset as a float, or None if it isn't one."""
    m = _OFFSET_RE.match(text)
    if not m:
        return None
    hours = int(m.group(1))
    frac = m.group(2)
    minutes = int(frac.ljust(2, "0")) if frac else 0
    if frac and len(frac) == 1:          # "+5.5" means five and a half hours
        minutes = int(frac) * 6
    if not (-12 <= hours <= 14) or not (0 <= minutes < 60):
        return None
    return hours + (minutes / 60) * (-1 if hours < 0 else 1)


_TZ_LOOKUP_PROMPT = (
    "Map the user text to ONE IANA timezone name (e.g. \"Europe/Kyiv\"). The text may be a city, region or "
    "country, in English or Ukrainian. For a country spanning several zones, pick its most populous. "
    "Reply ONLY with {\"timezone\":\"<IANA name>\"}, or {\"timezone\":null} if the text is not a place."
)


async def _resolve_location(text: str) -> Optional[tuple]:
    """(utc_offset, zone_name) for a free-text place, or None.

    The model only ever proposes a NAME; zoneinfo then decides whether that
    name is real and what its offset currently is. So a hallucinated zone
    can't turn into a plausible-looking wrong offset - it raises and we say
    we couldn't work it out. Same "LLM for the fuzzy part, deterministic
    lookup for the answer" split the rest of this repo uses."""
    try:
        data = await chat_json(OLLAMA_HOST, OLLAMA_MODEL,
                               [{"role": "system", "content": _TZ_LOOKUP_PROMPT},
                                {"role": "user", "content": text[:200]}])
    except OllamaError as e:
        logger.warning("remind: timezone lookup failed for %r (%s)", text[:60], e)
        return None
    zone = data.get("timezone")
    if not isinstance(zone, str) or not zone.strip():
        return None
    try:
        offset = datetime.now(ZoneInfo(zone.strip())).utcoffset()
    except Exception:
        logger.warning("remind: model proposed unknown zone %r", zone)
        return None
    if offset is None:
        return None
    return offset.total_seconds() / 3600, zone.strip()


class _PendingTzInputFilter(filters.MessageFilter):
    """Matches only a chat with a live timezone ask - see _PENDING_TZ_INPUT."""

    def filter(self, message) -> bool:
        pending = _PENDING_TZ_INPUT.get(message.chat_id)
        return bool(pending and time.monotonic() < pending[1])


_pending_tz_input_filter = _PendingTzInputFilter()


def _confirm_keyboard(pending_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Підтвердити", callback_data=f"remindtzok|{pending_id}"),
        InlineKeyboardButton("❌ Ні, ввести інше", callback_data=f"remindtzno|{pending_id}"),
    ]])


def utc_offset_to_fire_at(target_date: Optional[date], hour: int, minute: int, utc_offset: float) -> datetime:
    """Server-local naive datetime (matching every other fire_at in this
    module) at which hour:minute in a UTC{+-}offset timezone next occurs -
    computed entirely from an absolute UTC anchor, so the server's OWN
    timezone never needs to be known. target_date pins a specific date
    (the guild "remind me" buttons' restock day); None means "today or
    tomorrow in the user's zone, whichever is next" (plain /remind
    HH:MM)."""
    tz = timezone(timedelta(hours=utc_offset))
    now_utc = datetime.now(timezone.utc)
    if target_date is not None:
        target = datetime(target_date.year, target_date.month, target_date.day, hour, minute, tzinfo=tz)
    else:
        target = now_utc.astimezone(tz).replace(hour=hour, minute=minute, second=0, microsecond=0)
        if target <= now_utc:
            target += timedelta(days=1)
    delay = (target - now_utc).total_seconds()
    return datetime.now() + timedelta(seconds=max(delay, 1))


# 4 per row, and the label is the bare offset ("+2", "-11") rather than
# "UTC+2". Live report 2026-09-24: with 6-char labels packed 6 to a row,
# Telegram shrinks each button below the text width on a phone and CLIPS the
# label with no ellipsis - "UTC-10", "UTC-11" and "UTC-12" all render as
# "UTC-1". The user read that as the picker repeating itself; it was actually
# three different offsets displaying identically, and tapping any of them
# silently saved a timezone hours away from the intended one (set_user_tz
# persists it and every later reminder reuses it without asking again).
# Both halves matter: the short label halves the width needed, and 4 per row
# roughly doubles what each button gets. Correctness beats compactness here -
# a clipped timezone is silently wrong forever, an extra row is just a row.
_TZ_PER_ROW = 4


async def request_utc_offset(message, user_id: int, on_offset: Callable[[float], Awaitable[None]]) -> None:
    """Ask the user for their timezone as free text - an offset ("+3") or a
    place ("Київ", "New York") - then resume via on_offset(offset) once they
    confirm what we worked out. Only needed the first time; see
    usage_stats.get_user_tz/set_user_tz for the persisted answer that skips
    this on every later specific-time ask."""
    if len(_PENDING_TZ) >= _PENDING_TZ_MAX:
        _PENDING_TZ.pop(next(iter(_PENDING_TZ)), None)
    pending_id = uuid.uuid4().hex[:10]
    _PENDING_TZ[pending_id] = {"user_id": user_id, "chat_id": message.chat_id,
                               "on_offset": on_offset, "candidate": None}
    _PENDING_TZ_INPUT[message.chat_id] = (pending_id, time.monotonic() + TZ_INPUT_TTL_SECONDS)
    await message.reply_text(
        "Вкажіть свій часовий пояс — напишіть місто чи країну (наприклад «Київ») "
        "або зсув від UTC (наприклад «+3»):"
    )


async def handle_tz_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """The typed answer to request_utc_offset. Only ever reached for a chat
    with a live pending ask (_pending_tz_input_filter), so it can never take
    text away from the assess/resources conversations."""
    message = update.effective_message
    if not message or not message.text:
        return
    pending_entry = _PENDING_TZ_INPUT.get(message.chat_id)
    if pending_entry is None:
        return  # the filter already checked, but stay defensive
    pending_id, _expires = pending_entry
    pending = _PENDING_TZ.get(pending_id)
    if pending is None:
        _PENDING_TZ_INPUT.pop(message.chat_id, None)
        return

    text = message.text.strip()
    offset = _parse_offset_text(text)
    if offset is not None:
        zone, shown = None, f"UTC{offset:+g}"
    else:
        resolved = await _resolve_location(text)
        if resolved is None:
            await message.reply_text(
                "Не вдалося визначити часовий пояс. Спробуйте назву міста "
                "(наприклад «Київ», «Warsaw») або зсув (наприклад «+3»)."
            )
            return
        offset, zone = resolved
        shown = f"{zone} (зараз UTC{offset:+g})"

    pending["candidate"] = (offset, zone)
    await message.reply_text(f"Ваш часовий пояс: <b>{shown}</b>. Вірно?",
                             parse_mode="HTML", reply_markup=_confirm_keyboard(pending_id))


async def handle_tz_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """✅/❌ on the resolved timezone. Either way the keyboard goes away, so
    the message can't be tapped twice and doesn't linger as live UI."""
    query = update.callback_query
    await query.answer()
    parts = (query.data or "").split("|")
    if len(parts) != 2 or parts[0] not in ("remindtzok", "remindtzno"):
        return
    pending_id = parts[1]
    pending = _PENDING_TZ.get(pending_id)
    if pending is None:
        await _drop_keyboard(query, "Ця сесія застаріла — спробуйте ще раз.")
        return
    user = update.effective_user
    if not user or user.id != pending["user_id"]:
        await query.answer("Це чужий вибір.", show_alert=True)
        return

    if parts[0] == "remindtzno":
        await _drop_keyboard(query, "Гаразд — напишіть місто/країну або зсув від UTC ще раз:")
        _PENDING_TZ_INPUT[pending["chat_id"]] = (pending_id, time.monotonic() + TZ_INPUT_TTL_SECONDS)
        return

    candidate = pending.get("candidate")
    if candidate is None:
        await _drop_keyboard(query, "Спочатку вкажіть часовий пояс.")
        return
    offset, zone = candidate
    _PENDING_TZ.pop(pending_id, None)
    _PENDING_TZ_INPUT.pop(pending["chat_id"], None)
    usage_stats.set_user_tz(pending["user_id"], offset, zone)
    saved = f"{zone} (UTC{offset:+g})" if zone else f"UTC{offset:+g}"
    await _drop_keyboard(query, f"✅ Часовий пояс збережено: {saved}")
    await pending["on_offset"](offset)


async def _drop_keyboard(query, text: str) -> None:
    """Replace a prompt with plain text, removing its buttons - "hide the UI
    after the choice" for every exit from the confirm step."""
    try:
        await query.edit_message_text(text)
    except TelegramError:
        pass  # e.g. "message not modified" on a double-tap race - harmless


async def handle_tz_edit_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Re-ask for someone who saved the wrong timezone. The re-pick only
    updates what's stored - there's no pending action to resume, unlike the
    first ask."""
    query = update.callback_query
    await query.answer()
    parts = (query.data or "").split("|")
    if len(parts) != 2 or parts[0] != "remindtzedit":
        return
    user = update.effective_user
    if not user or str(user.id) != parts[1]:
        await query.answer("Це чужий вибір.", show_alert=True)
        return

    async def _noop(_offset: float) -> None:
        return

    await request_utc_offset(query.message, user.id, _noop)


def build_tz_callback_handler() -> CallbackQueryHandler:
    return CallbackQueryHandler(handle_tz_confirm, pattern=r"^remindtz(?:ok|no)\|")


def build_tz_edit_callback_handler() -> CallbackQueryHandler:
    return CallbackQueryHandler(handle_tz_edit_button, pattern=r'^remindtzedit\|')


def build_tz_input_handler() -> MessageHandler:
    """Registered BEFORE the assess/resources conversations - the filter makes
    it a no-op for any chat without a live timezone ask."""
    return MessageHandler(_pending_tz_input_filter & filters.TEXT & ~filters.COMMAND, handle_tz_input)



def _load() -> dict:
    if _STORE_PATH.exists():
        try:
            return json.loads(_STORE_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            logger.warning("remind: couldn't read %s, starting fresh", _STORE_PATH, exc_info=True)
    return {}


def _save(store: dict) -> None:
    # Atomic write: a crash or launchctl reload landing mid-write on a plain
    # write_text (this repo reloads often, per its own docs) can leave a
    # truncated file - _load()'s except JSONDecodeError then silently starts
    # fresh, dropping every pending reminder. Writing to a temp file in the
    # same directory and os.replace()-ing over the target is atomic on POSIX.
    tmp = _STORE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(store, indent=2))
    tmp.replace(_STORE_PATH)


def _parse_duration(text: str) -> tuple[datetime, str] | None:
    """"/remind 2h <text>" etc - a plain elapsed delay, no timezone
    involved (see module docstring)."""
    m = _DURATION_RE.match(text)
    if not m:
        return None
    amount, unit, message = m.groups()
    seconds = _UNIT_SECONDS.get(unit.lower())
    if not seconds:
        return None
    return datetime.now() + timedelta(seconds=int(amount) * seconds), message.strip()


def _parse_absolute(text: str) -> tuple[int, int, str] | None:
    """"/remind 18:30 <text>" - an hour/minute in the USER's own local
    time, not resolvable to an actual fire_at without their UTC offset
    (see handle_remind's use of utc_offset_to_fire_at)."""
    m = _TIME_RE.match(text)
    if not m:
        return None
    hour, minute, message = m.groups()
    return int(hour), int(minute), message.strip()


async def _fire(context: ContextTypes.DEFAULT_TYPE) -> None:
    reminder_id, chat_id, text = context.job.data
    store = _load()
    store.pop(reminder_id, None)
    _save(store)
    await context.bot.send_message(chat_id, f"⏰ Reminder: {text}")


def _schedule(app: Application, reminder_id: str, chat_id: int, text: str, fire_at: datetime) -> None:
    delay = max((fire_at - datetime.now()).total_seconds(), 1)
    app.job_queue.run_once(_fire, when=delay, data=(reminder_id, chat_id, text), name=reminder_id)


def schedule_reminder(app: Application, chat_id: int, text: str, fire_at: datetime) -> str:
    """Public, UNGATED entry point for other modules to schedule a
    reminder without going through the /remind command (which stays
    GO_ALLOWED_USER_IDS-gated) - e.g. a "remind me when this resource
    lands" button on the resource report, which needs to work for every
    guild member, not just the admin allowlist. Same persistence /
    reschedule-on-restart guarantees as a /remind-created one."""
    reminder_id = uuid.uuid4().hex[:8]
    store = _load()
    store[reminder_id] = {"chat_id": chat_id, "text": text, "fire_at": fire_at.isoformat()}
    _save(store)
    _schedule(app, reminder_id, chat_id, text, fire_at)
    return reminder_id


async def _finish_remind(app: Application, message, fire_at: datetime, text: str) -> None:
    reminder_id = schedule_reminder(app, message.chat_id, text, fire_at)
    await message.reply_text(f"⏰ [{reminder_id}] Will remind you at {fire_at.strftime('%Y-%m-%d %H:%M')}: {text}")


async def handle_remind(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message:
        return

    user = update.effective_user
    if GO_ALLOWED_USER_IDS and (not user or user.id not in GO_ALLOWED_USER_IDS):
        logger.warning("remind: rejected user_id=%s", user.id if user else None)
        return

    args = " ".join(context.args).strip()
    usage_stats.record_command_for(update, "remind", args)
    if not args:
        await message.reply_text(
            "Usage:\n/remind 20m <text>\n/remind 2h <text>\n/remind 18:30 <text>\n"
            "/remind list\n/remind cancel <id>\n/remind tz"
        )
        return

    if args.lower() == "tz":
        # An already-saved timezone is otherwise unreachable: the "change"
        # button only exists on the confirmation message, which scrolls away.
        current = usage_stats.get_user_zone(user.id) or usage_stats.get_user_tz(user.id)
        await message.reply_text(f"Поточний часовий пояс: {current if current is not None else 'не вказано'}")

        async def _noop(_offset: float) -> None:
            return

        await request_utc_offset(message, user.id, _noop)
        return

    if args.lower() == "list":
        store = _load()
        mine = {rid: r for rid, r in store.items() if r["chat_id"] == message.chat_id}
        if not mine:
            await message.reply_text("No pending reminders.")
            return
        lines = [
            f"{rid}: {r['text']} — {datetime.fromisoformat(r['fire_at']).strftime('%Y-%m-%d %H:%M')}"
            for rid, r in mine.items()
        ]
        await message.reply_text("\n".join(lines))
        return

    if args.lower().startswith("cancel"):
        reminder_id = args[len("cancel"):].strip()
        store = _load()
        if reminder_id in store and store[reminder_id]["chat_id"] == message.chat_id:
            store.pop(reminder_id)
            _save(store)
            for job in context.job_queue.get_jobs_by_name(reminder_id):
                job.schedule_removal()
            await message.reply_text("Cancelled.")
        else:
            await message.reply_text("No such reminder id. /remind list to see pending ones.")
        return

    duration = _parse_duration(args)
    if duration:
        fire_at, text = duration
        if not text:
            await message.reply_text("Give it something to remind you about, e.g. /remind 20m check the oven")
            return
        await _finish_remind(context.application, message, fire_at, text)
        return

    absolute = _parse_absolute(args)
    if absolute:
        hour, minute, text = absolute
        if not text:
            await message.reply_text("Give it something to remind you about, e.g. /remind 18:30 call mom")
            return
        # An absolute clock time is only meaningful once we know WHOSE
        # clock - ask once (buttons), then remember it (usage_stats,
        # same store /stats reads) so this doesn't ask again.
        user_id = user.id if user else message.chat_id
        offset = usage_stats.get_user_tz(user_id)
        if offset is None:
            async def _on_offset(offset: float, hour=hour, minute=minute, text=text) -> None:
                fire_at = utc_offset_to_fire_at(None, hour, minute, offset)
                await _finish_remind(context.application, message, fire_at, text)

            await request_utc_offset(message, user_id, _on_offset)
            return
        fire_at = utc_offset_to_fire_at(None, hour, minute, offset)
        await _finish_remind(context.application, message, fire_at, text)
        return

    await message.reply_text(
        "Couldn't parse that. Try: /remind 20m check the oven, or /remind 18:30 call mom"
    )


def build_remind_handler() -> CommandHandler:
    return CommandHandler("remind", handle_remind)


def reschedule_pending(app: Application) -> None:
    """Call once at startup: re-arm reminders that survived a restart.
    One found already overdue (bot was down past its fire time) fires
    almost immediately instead of being silently dropped."""
    store = _load()
    now = datetime.now()
    for reminder_id, r in store.items():
        fire_at = datetime.fromisoformat(r["fire_at"])
        text = r["text"] if fire_at > now else f"(delayed - bot was offline) {r['text']}"
        _schedule(app, reminder_id, r["chat_id"], text, fire_at)
    if store:
        logger.info("remind: rescheduled %d pending reminder(s)", len(store))


def _demo() -> None:
    """Pinned regression checks for utc_offset_to_fire_at - run via
    `python3 telegram_remind.py`."""
    d = date(2026, 10, 1)
    fire_plus12 = utc_offset_to_fire_at(d, 0, 5, 12.0)
    fire_plus0 = utc_offset_to_fire_at(d, 0, 5, 0.0)
    # A UTC+12 user's local 00:05 on a given date happens 12h before a
    # UTC+0 user's local 00:05 on the SAME date, in absolute terms.
    assert fire_plus12 < fire_plus0
    assert abs((fire_plus0 - fire_plus12).total_seconds() - 12 * 3600) < 2

    # No target_date: next occurrence of hour:minute in the user's own
    # zone - an hour already past for them today should roll to tomorrow,
    # i.e. ~23h away (24h minus the 1h already elapsed since that time).
    user_offset = 3.0
    user_now = datetime.now(timezone.utc) + timedelta(hours=user_offset)
    past_hour = (user_now - timedelta(hours=1)).hour
    fire = utc_offset_to_fire_at(None, past_hour, user_now.minute, user_offset)
    delay = (fire - datetime.now()).total_seconds()
    assert 22.9 * 3600 < delay < 23.1 * 3600, f"expected ~23h (tomorrow), got {delay}s"

    print("telegram_remind._demo: all checks passed")


if __name__ == "__main__":
    _demo()
