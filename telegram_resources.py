"""
telegram_resources.py
======================
Natural-language flow: a user names one or more Orna materials they need
(in plain English or Ukrainian — no slash command required), the bot asks
how many of each, then replies with:

  - every guild that will sell each material, and when (next occurrence)
  - how many of that guild's proofs are needed to buy the requested amount
    (math ported from OrnaCodex's ProofView.vue — see orna_proofs.py)
  - a "🔔 remind me" button per guild/day, so the bot itself pings the user
    right when that guild's rotation lands instead of relying on them to
    come back and check (see schedule_reminder in telegram_remind.py -
    replaced an earlier "add to Google Calendar" link with this, since a
    reminder actually delivered by the bot beats a link out to a separate
    app the user has to remember to check)

A plain "/need <resources>" command is also accepted as a shortcut into the
same flow, for when free-text detection isn't wanted.

Pipeline per free-text message:
  1. Ask the local Ollama model (telegram_nlp.extract_resources) which known
     materials the message refers to. No match -> stay silent (it's
     probably not a resource request at all).
  2. Ask the user how many of each they need -> AWAITING_QUANTITIES.
  3. Ask the model (telegram_nlp.extract_quantities) to parse the reply.
     Materials still missing a quantity are asked about again.
  4. Look up each material's tier/rarity in the codex (orna_codex), compute
     guild-proof costs, and reply.

Register with:
    from telegram_resources import build_resource_conversation, build_reminder_callback_handler
    application.add_handler(build_resource_conversation())
    application.add_handler(build_reminder_callback_handler())
"""
from __future__ import annotations

import asyncio
import datetime
import html
import logging
import uuid
from collections import defaultdict
from typing import Dict, List, NamedTuple, Optional, Tuple

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import TelegramError
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from orna_codex import fetch_material_meta
from orna_proofs import GUILD_PROOFS, base_exchange_rate, proofs_needed
from orna_sheets import GUILD_NAMES, fetch_sheet_data
from telegram_nlp import OllamaError, extract_quantities, extract_resources
from telegram_remind import schedule_reminder

logger = logging.getLogger(__name__)


class MaterialNeed(NamedTuple):
    name: str
    qty: int
    proofs: int


class ReminderBundle(NamedTuple):
    """Everything landing at one guild on one day - a reminder covers the
    whole bundle (one shop visit), not one material at a time."""
    guild: str
    occurrence: datetime.date
    materials: List[MaterialNeed]


# callback_data can't carry a whole bundle list, so a report message's
# "remind me" buttons reference a short-lived key into this dict instead -
# same pattern as telegram_orna._STATE / telegram_go._SESSIONS. "scheduled"
# guards against a double-tap creating two identical reminders.
_REMINDER_STATE: Dict[str, dict] = {}
_REMINDER_STATE_MAX = 200


def _remember_bundles(bundles: List[ReminderBundle]) -> str:
    if len(_REMINDER_STATE) >= _REMINDER_STATE_MAX:
        _REMINDER_STATE.pop(next(iter(_REMINDER_STATE)), None)
    key = uuid.uuid4().hex[:10]
    _REMINDER_STATE[key] = {"bundles": bundles, "scheduled": set()}
    return key

# ConversationHandler state: waiting for the user to reply with quantities.
AWAITING_QUANTITIES = 1

# context.user_data key holding {"resources": [...], "collected": {...}, "sheet": [...]}
# while a request is in progress.
_PENDING_KEY = "pending_resource_request"

CONVERSATION_TIMEOUT = 600  # seconds; drop a stale request rather than let it linger


def _next_occurrence(date_str: str, today: datetime.date) -> Optional[datetime.date]:
    """'September 15' -> the next calendar date that matches, on or after today."""
    try:
        parsed = datetime.datetime.strptime(date_str, "%B %d")
    except ValueError:
        return None
    candidate = parsed.replace(year=today.year).date()
    if candidate < today:
        candidate = candidate.replace(year=today.year + 1)
    return candidate


async def _start_flow(message, context: ContextTypes.DEFAULT_TYPE, text: str) -> int:
    """Shared entry logic for both the free-text handler and /need."""
    try:
        sheet_values = await fetch_sheet_data()
    except Exception:
        logger.exception("Failed to fetch sheet data for resource request")
        return ConversationHandler.END

    known = [row[0] for row in sheet_values if row]

    try:
        resources = await extract_resources(text, known)
    except OllamaError:
        logger.exception("Ollama unreachable while extracting resources")
        await message.reply_text(
            "Не вдалося обробити запит: локальна модель (Ollama) недоступна."
        )
        return ConversationHandler.END

    if not resources:
        # Not (recognisably) a resource request — stay silent so we don't
        # answer every unrelated message in the chat.
        return ConversationHandler.END

    context.user_data[_PENDING_KEY] = {
        "resources": resources,
        "collected": {},
        "sheet": sheet_values,
    }
    prompt = "Скільки одиниць кожного ресурсу вам потрібно?\n" + "\n".join(
        f"• {r}" for r in resources
    )
    prompt += "\n\nНапишіть у відповідь, наприклад: «Adamantine 500, Mythril 200». /cancel — скасувати."
    await message.reply_text(prompt)
    return AWAITING_QUANTITIES


async def handle_free_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    message = update.effective_message
    text = (message.text or "").strip()
    if not text:
        return ConversationHandler.END
    return await _start_flow(message, context, text)


async def handle_need_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    message = update.effective_message
    text = " ".join(context.args).strip()
    if not text:
        await message.reply_text(
            "Вкажіть потрібні ресурси. Приклад: /need Adamantine, Mythril"
        )
        return ConversationHandler.END
    return await _start_flow(message, context, text)


async def handle_quantities(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    message = update.effective_message
    text = (message.text or "").strip()
    pending = context.user_data.get(_PENDING_KEY)
    if not pending or not text:
        return ConversationHandler.END

    pending_resources: List[str] = pending["resources"]

    try:
        new_qtys = await extract_quantities(text, pending_resources)
    except OllamaError:
        logger.exception("Ollama unreachable while extracting quantities")
        await message.reply_text(
            "Не вдалося обробити відповідь: локальна модель (Ollama) недоступна. Спробуйте ще раз."
        )
        return AWAITING_QUANTITIES

    if not new_qtys:
        await message.reply_text(
            "Не вдалося розпізнати кількість. Спробуйте ще раз, наприклад: «500, 200» "
            "або «Adamantine 500, Mythril 200». /cancel — скасувати."
        )
        return AWAITING_QUANTITIES

    collected: Dict[str, int] = pending.setdefault("collected", {})
    collected.update(new_qtys)

    missing = [r for r in pending_resources if r not in collected]
    if missing:
        pending["resources"] = missing
        context.user_data[_PENDING_KEY] = pending
        await message.reply_text(
            "Записав. А скільки потрібно:\n" + "\n".join(f"• {r}" for r in missing)
        )
        return AWAITING_QUANTITIES

    sheet_values = pending["sheet"]
    context.user_data.pop(_PENDING_KEY, None)

    await message.reply_text("Рахую…")
    blocks, bundles = await build_report(collected, sheet_values)
    await send_report_blocks(message, blocks, bundles)
    return ConversationHandler.END


# Comfortably under Telegram's 4096-char message cap, leaving room for the
# HTML markup itself (anchor tags, bold) which counts toward that limit.
_MAX_MESSAGE_LEN = 3500


def _pack_chunks(units: List[str], limit: int, sep: str = "\n\n") -> List[str]:
    """Greedily pack `units` into <= `limit`-char strings joined by `sep`.

    A unit longer than `limit` on its own is recursively split on newlines
    (each report line is self-contained — an <a>/<b> tag never spans more
    than one line) so one oversized block still sends instead of failing.
    """
    chunks: List[str] = []
    current: List[str] = []
    current_len = 0
    for unit in units:
        if len(unit) > limit:
            if current:
                chunks.append(sep.join(current))
                current, current_len = [], 0
            parts = unit.split("\n")
            if len(parts) > 1:
                chunks.extend(_pack_chunks(parts, limit, sep="\n"))
            else:
                # Can't split further (a single line on its own is over the
                # limit) — send it as-is rather than recurse forever.
                chunks.append(unit)
            continue
        added = len(unit) + (len(sep) if current else 0)
        if current and current_len + added > limit:
            chunks.append(sep.join(current))
            current, current_len = [], 0
            added = len(unit)
        current.append(unit)
        current_len += added
    if current:
        chunks.append(sep.join(current))
    return chunks


async def send_report_blocks(
    message, blocks: List[str], bundles: Optional[List[ReminderBundle]] = None
) -> None:
    """Send report blocks (one per material) as as few HTML messages as fit,
    then - if `bundles` is given and non-empty - one more message with a
    "🔔 remind me" button per (guild, date) bundle.

    Chunking normally happens on block boundaries only, so an <a>/<b> tag
    never gets split across messages; a single oversized block (many guilds
    x many bundled materials) falls back to splitting on its own lines
    rather than failing to send at all.

    Buttons live in one separate consolidated message rather than inline
    per report row, on purpose: report blocks get packed several-per-message
    to fit Telegram's length cap (see _pack_chunks), so a button "belonging"
    to one row of a multi-block message has no single well-defined message
    to attach to. One follow-up panel sidesteps that entirely and reads
    better anyway - "here's what you can get reminders for" in one place.
    """
    for chunk_text in _pack_chunks(blocks, _MAX_MESSAGE_LEN):
        await message.reply_text(
            chunk_text, parse_mode="HTML", disable_web_page_preview=True
        )

    if bundles:
        key = _remember_bundles(bundles)
        rows = [
            [InlineKeyboardButton(_bundle_label(b), callback_data=f"needrem|{key}|{i}")]
            for i, b in enumerate(bundles)
        ]
        await message.reply_text(
            "Поставити нагадування, коли ресурс зʼявиться в гільдії:",
            reply_markup=InlineKeyboardMarkup(rows),
        )


async def build_report(
    quantities: Dict[str, int], sheet_values: List[List[str]]
) -> Tuple[List[str], List[ReminderBundle]]:
    """
    Build one HTML report block per requested material: tier/rarity, and
    every guild that sells it with the next date, days away, and proofs
    needed. Also returns one ReminderBundle per (guild, date) - materials
    landing at the same guild on the same day are bundled together, since
    that's one shop visit worth one reminder, not one per material -
    for the caller to offer as "🔔 remind me" buttons via
    send_report_blocks.
    """
    today = datetime.date.today()
    by_name = {row[0]: row for row in sheet_values if row}

    # Pass 1: work out every material's guild/date/proof rows first — a
    # later material might land on the same guild+date as an earlier one,
    # and bundles need to be complete before returning them.
    Row = Tuple[str, str, str, Optional[int], Optional[str], datetime.date]  # guild, date_str, when, proofs, currency, occurrence
    per_material: List[Tuple[str, List[str], List[Row]]] = []
    bundles: Dict[Tuple[str, datetime.date], List[MaterialNeed]] = defaultdict(list)

    for name, qty in quantities.items():
        row = by_name.get(name)
        meta = await asyncio.to_thread(fetch_material_meta, name)

        header = [f"<b>{html.escape(name)}</b> — потрібно {qty}"]

        if row is None:
            header.append("  ресурс не знайдено в таблиці прогнозу")
            per_material.append((name, header, []))
            continue

        guild_dates: List[Tuple[str, str, datetime.date]] = []
        for i, date_str in enumerate(row[1:]):
            if i >= len(GUILD_NAMES) or not date_str:
                continue
            occurrence = _next_occurrence(date_str, today)
            if occurrence is None:
                continue
            guild_dates.append((GUILD_NAMES[i], date_str, occurrence))
        guild_dates.sort(key=lambda g: g[2])

        if not guild_dates:
            header.append("  не з'являється в жодній гільдії")
            per_material.append((name, header, []))
            continue

        base_rate = None
        if meta is not None:
            base_rate = base_exchange_rate(meta.tier, meta.rarity)
            header.append(f"  рівень ★{meta.tier}, {meta.rarity}; база 100:{base_rate}")
        else:
            header.append("  рівень/рідкість невідомі — кількість доказів порахувати не вдалось")

        material_rows: List[Row] = []
        for guild, date_str, occurrence in guild_dates:
            days = (occurrence - today).days
            when = f"за {days} дн." if days > 0 else "сьогодні"
            if base_rate is not None:
                proofs = proofs_needed(qty, guild, base_rate)
                currency = GUILD_PROOFS[guild].currency
                bundles[(guild, occurrence)].append(MaterialNeed(name, qty, proofs))
                material_rows.append((guild, date_str, when, proofs, currency, occurrence))
            else:
                material_rows.append((guild, date_str, when, None, None, occurrence))
        per_material.append((name, header, material_rows))

    # Pass 2: render (plain text now - no more per-row calendar link).
    blocks: List[str] = []
    for name, header, material_rows in per_material:
        lines = list(header)
        for guild, date_str, when, proofs, currency, occurrence in material_rows:
            date_esc = html.escape(date_str)
            padded_date = f"{date_esc:<14}"
            if proofs is not None:
                lines.append(
                    f"    {guild:<12}{padded_date}{when:<10}{proofs} × {html.escape(currency)}"
                )
            else:
                lines.append(f"    {guild:<12}{padded_date}{when}")
        blocks.append("\n".join(lines))

    reminder_bundles = [
        ReminderBundle(guild=key[0], occurrence=key[1], materials=materials)
        for key, materials in bundles.items()
    ]
    return blocks, reminder_bundles


def _bundle_fire_at(occurrence: datetime.date) -> datetime.datetime:
    """Fire just after midnight, server-local time, on the occurrence date.
    Neither the exact daily shop-reset time nor the user's own timezone is
    known (same ambiguity the old Google Calendar all-day-event design
    accepted) - this is the closest deterministic approximation of "the day
    it becomes available" without either piece of information."""
    return datetime.datetime.combine(occurrence, datetime.time(0, 5))


def _bundle_text(bundle: ReminderBundle) -> str:
    items = ", ".join(f"{m.qty}x {m.name}" for m in bundle.materials)
    return f"🎁 Гільдія {bundle.guild}: {items}"


def _bundle_when(occurrence: datetime.date) -> str:
    days = (occurrence - datetime.date.today()).days
    return "сьогодні" if days <= 0 else f"за {days} дн."


def _bundle_label(bundle: ReminderBundle) -> str:
    """Button text for one bundle - needs the material name(s), not just
    the guild, or asking about several materials at once produces a wall
    of buttons that all just say the same handful of guild names with no
    way to tell which is which (live report: 20 buttons, 10 guild names
    each appearing twice, one per material - useless without the name)."""
    names = ", ".join(m.name for m in bundle.materials)
    label = f"🔔 {bundle.guild} — {names} — {_bundle_when(bundle.occurrence)}"
    return label if len(label) <= 64 else label[:63] + "…"


async def handle_reminder_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    parts = (query.data or "").split("|")
    if len(parts) != 3 or parts[0] != "needrem":
        return
    key, idx_str = parts[1], parts[2]
    state = _REMINDER_STATE.get(key)
    if state is None:
        await query.answer("Ця сесія застаріла — сформуйте звіт ще раз.", show_alert=True)
        return
    idx = int(idx_str) if idx_str.isdigit() else -1
    bundles: List[ReminderBundle] = state["bundles"]
    if not (0 <= idx < len(bundles)):
        return
    if idx in state["scheduled"]:
        await query.answer("Вже встановлено.", show_alert=True)
        return

    bundle = bundles[idx]
    schedule_reminder(
        context.application, query.message.chat_id, _bundle_text(bundle), _bundle_fire_at(bundle.occurrence),
    )
    state["scheduled"].add(idx)
    await query.answer("✅ Нагадування встановлено!")

    # Remove the tapped button rather than leaving it there - there's no
    # separate confirmation message, so the button disappearing (a change
    # that stays visible in the chat, unlike the toast above which is gone
    # the moment it's dismissed) IS the confirmation.
    remaining_rows = [
        [InlineKeyboardButton(_bundle_label(b), callback_data=f"needrem|{key}|{i}")]
        for i, b in enumerate(bundles) if i not in state["scheduled"]
    ]
    try:
        await query.edit_message_reply_markup(
            reply_markup=InlineKeyboardMarkup(remaining_rows) if remaining_rows else None
        )
    except TelegramError:
        pass  # e.g. "message not modified" on a double-tap race - harmless


def build_reminder_callback_handler() -> CallbackQueryHandler:
    return CallbackQueryHandler(handle_reminder_button, pattern=r"^needrem\|")


async def cancel_resource_request(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop(_PENDING_KEY, None)
    message = update.effective_message
    if message:
        await message.reply_text("Скасовано.")
    return ConversationHandler.END


async def resource_request_timeout(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop(_PENDING_KEY, None)
    if update and update.effective_message:
        await update.effective_message.reply_text(
            "Час очікування вичерпано. Напишіть потрібні ресурси ще раз."
        )
    return ConversationHandler.END


def build_resource_conversation() -> ConversationHandler:
    """
    Assemble the ConversationHandler driving the natural-language resource
    flow. Registered after build_assess_conversation() so an active
    screenshot-assessment conversation keeps priority over its own chat.
    """
    return ConversationHandler(
        entry_points=[
            CommandHandler("need", handle_need_command),
            MessageHandler(filters.TEXT & ~filters.COMMAND, handle_free_text),
        ],
        states={
            AWAITING_QUANTITIES: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_quantities),
            ],
            ConversationHandler.TIMEOUT: [
                MessageHandler(filters.ALL, resource_request_timeout),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel_resource_request),
        ],
        conversation_timeout=CONVERSATION_TIMEOUT,
    )
