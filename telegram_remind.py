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
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Awaitable, Callable, Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import TelegramError
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

from telegram_go import GO_ALLOWED_USER_IDS
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

# ponytail: whole-hour offsets only (-12..+14 covers every real UTC offset's
# hour component) - skips half/quarter-hour zones (India +5:30, Nepal
# +5:45, ...), which round to the nearest hour. Fine for "remind me around
# this time"; add real fractional offsets if that precision is ever
# actually reported as a problem.
_TZ_OFFSETS = list(range(-12, 15))
_PENDING_TZ: dict = {}  # short id -> {"user_id": int, "on_offset": async fn(float)} - in-memory only, same as _REMINDER_STATE/telegram_go._SESSIONS
_PENDING_TZ_MAX = 200


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


def _tz_keyboard(pending_id: str) -> InlineKeyboardMarkup:
    buttons = [InlineKeyboardButton(f"UTC{o:+d}", callback_data=f"remindtz|{pending_id}|{o}") for o in _TZ_OFFSETS]
    rows = [buttons[i:i + 6] for i in range(0, len(buttons), 6)]
    return InlineKeyboardMarkup(rows)


async def request_utc_offset(message, user_id: int, on_offset: Callable[[float], Awaitable[None]]) -> None:
    """Ask the user (buttons only, never free text - same reasoning as
    every other "ask" flow in this codebase: a free-text timezone reply
    is just one more unreliable thing to parse) which UTC offset to use
    for a specific-clock-time reminder, then resume via on_offset(offset)
    once picked. Only needed the first time - see usage_stats.get_user_tz/
    set_user_tz for the persisted answer that skips this on every later
    specific-time ask."""
    if len(_PENDING_TZ) >= _PENDING_TZ_MAX:
        _PENDING_TZ.pop(next(iter(_PENDING_TZ)), None)
    pending_id = uuid.uuid4().hex[:10]
    _PENDING_TZ[pending_id] = {"user_id": user_id, "on_offset": on_offset}
    await message.reply_text(
        "Щоб встановити нагадування на конкретний час, оберіть свій часовий пояс (UTC):",
        reply_markup=_tz_keyboard(pending_id),
    )


async def handle_tz_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    parts = (query.data or "").split("|")
    if len(parts) != 3 or parts[0] != "remindtz":
        return
    pending_id, offset_str = parts[1], parts[2]
    pending = _PENDING_TZ.pop(pending_id, None)
    if pending is None:
        await query.answer("Ця сесія застаріла — спробуйте ще раз.", show_alert=True)
        return
    try:
        offset = float(offset_str)
    except ValueError:
        return
    usage_stats.set_user_tz(pending["user_id"], offset)
    try:
        await query.edit_message_text(f"✅ Часовий пояс UTC{offset:+g} збережено.")
    except TelegramError:
        pass  # e.g. "message not modified" on a double-tap race - harmless
    await pending["on_offset"](offset)


def build_tz_callback_handler() -> CallbackQueryHandler:
    return CallbackQueryHandler(handle_tz_button, pattern=r"^remindtz\|")


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
            "/remind list\n/remind cancel <id>"
        )
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
