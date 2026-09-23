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
"""
from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

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


def _load() -> dict:
    if _STORE_PATH.exists():
        try:
            return json.loads(_STORE_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            logger.warning("remind: couldn't read %s, starting fresh", _STORE_PATH, exc_info=True)
    return {}


def _save(store: dict) -> None:
    _STORE_PATH.write_text(json.dumps(store, indent=2))


def _parse(text: str) -> tuple[datetime, str] | None:
    m = _DURATION_RE.match(text)
    if m:
        amount, unit, message = m.groups()
        seconds = _UNIT_SECONDS.get(unit.lower())
        if seconds:
            return datetime.now() + timedelta(seconds=int(amount) * seconds), message.strip()

    m = _TIME_RE.match(text)
    if m:
        hour, minute, message = m.groups()
        now = datetime.now()
        fire_at = now.replace(hour=int(hour), minute=int(minute), second=0, microsecond=0)
        if fire_at <= now:
            fire_at += timedelta(days=1)
        return fire_at, message.strip()

    return None


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


async def handle_remind(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    usage_stats.record_command("remind")
    message = update.effective_message
    if not message:
        return

    user = update.effective_user
    if GO_ALLOWED_USER_IDS and (not user or user.id not in GO_ALLOWED_USER_IDS):
        logger.warning("remind: rejected user_id=%s", user.id if user else None)
        return

    args = " ".join(context.args).strip()
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

    parsed = _parse(args)
    if not parsed:
        await message.reply_text(
            "Couldn't parse that. Try: /remind 20m check the oven, or /remind 18:30 call mom"
        )
        return
    fire_at, text = parsed
    if not text:
        await message.reply_text("Give it something to remind you about, e.g. /remind 20m check the oven")
        return

    reminder_id = schedule_reminder(context.application, message.chat_id, text, fire_at)
    await message.reply_text(f"⏰ [{reminder_id}] Will remind you at {fire_at.strftime('%Y-%m-%d %H:%M')}: {text}")


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
