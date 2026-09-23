"""
telegram_orna.py
================
`/orna <text>` (English or Ukrainian) - single entry point that routes the
request via telegram_nlp.route_query into one of four intents:
  - "today": what materials are available today (same data as /res_today,
    independently reimplemented here rather than imported - see
    telegram_bot.py, kept untouched on purpose while /orna is proven out
    alongside the existing commands).
  - "next": when/where a specific named material becomes available (same
    data as /res_next). Falls through to codex search if the named thing
    isn't a known Material Forecast material - it might still be a real
    codex entry (a monster, an item that isn't in the shop rotation, etc).
  - "codex": a lookup of one specific named item/monster/etc by name -
    search playorna.com's codex.
  - "query": "what gives/causes/is immune to X", "mag > 250 and crit > 3%",
    "items with 'dragon' in the description", "best mag item for thieves
    and for mages" - one or more structured multi-attribute searches over
    orna_aussies' full item/monster/etc. database, not a name lookup. See
    telegram_nlp.plan_queries for how free text becomes one or more
    condition blocks (and, if genuinely ambiguous, a button-only
    clarifying question first), and orna_aussies.query_records for how a
    block is evaluated.

Both "codex" and "query" results feed into the exact same result-list/
entry rendering: orna_aussies' record ids are the same slugs playorna.com
uses, so a "query" match opens straight into a real codex page just like
a "codex" one does. Every codex page - item, class, monster, boss,
follower, raid, spell, building, dungeon - embeds a universal
`codex-bootstrap` JSON blob (facts/effects/tags/sections), so Telegram is
just a UI over that already-structured data; see
orna_codex.fetch_codex_json for where that's read. An entry view also
gets an "Assess" link to aussiescodex.com's own page for that record
when one exists (only 4 of the 9 categories have one - see
orna_aussies.has_aussies_page) - playorna's own codex has no upgrade/
assess calculator, aussiescodex does.

No LLM involved past routing+condition-parsing. Navigation never edits a
message in place except paging through one result list - every "open
this" action sends a new message instead, so Telegram's own scrollback
doubles as a browsing history with no "back" button or state stack
needed (same pattern telegram_go.py uses).
"""
from __future__ import annotations

import asyncio
import datetime
import html
import logging
import re
import uuid
from collections import defaultdict
from typing import Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import TelegramError
from telegram.ext import CallbackQueryHandler, CommandHandler, ContextTypes

from orna_aussies import build_url as build_aussies_url
from orna_aussies import decode as decode_effect_code
from orna_aussies import has_aussies_page
from orna_aussies import query_records, refetch_now, resolve_codes as resolve_effect_codes
from orna_codex import codex_search, fetch_codex_json
from orna_sheets import GUILD_NAMES, fetch_sheet_data, get_today_month_day
from telegram_go import GO_ALLOWED_USER_IDS
from telegram_nlp import OllamaError, plan_queries, route_query
import usage_stats

logger = logging.getLogger(__name__)

_RESULTS_PER_PAGE = 8

# callback_data can't carry a full url/query list (64-byte cap), so each
# rendered message's buttons reference a short-lived key into this dict
# instead. Same ponytail tradeoff as telegram_go._SESSIONS: in-memory,
# single-process, capped - fine at this volume, add persistence if it
# ever isn't.
_STATE: dict[str, dict] = {}
_STATE_MAX = 200


def _remember(state: dict) -> str:
    if len(_STATE) >= _STATE_MAX:
        _STATE.pop(next(iter(_STATE)), None)
    key = uuid.uuid4().hex[:10]
    _STATE[key] = state
    return key


# -----------------------------------------------------------------------------
# "today" / "next" - same data the existing /res_today, /res_next serve
# -----------------------------------------------------------------------------

async def _today_text() -> str:
    today = get_today_month_day()
    try:
        values = await fetch_sheet_data()
    except Exception as e:
        return f"Не вдалося отримати дані: {e}"

    tdg: dict[str, list[str]] = defaultdict(list)
    for res in values:
        if not res:
            continue
        for i, date in enumerate(res[1:]):
            if date == today and i < len(GUILD_NAMES):
                tdg[GUILD_NAMES[i]].append(res[0])

    if not tdg:
        return f"Сьогодні ({today}) немає ресурсів."
    lines = [f"Ресурси {today}"]
    for guild, materials in tdg.items():
        lines.append(f"{guild}      {', '.join(materials)}")
    return "\n".join(lines)


async def _next_text(resource_query: str) -> Optional[str]:
    """None means: not a known Material Forecast resource - caller should
    fall through to codex search instead."""
    try:
        values = await fetch_sheet_data()
    except Exception as e:
        return f"Не вдалося отримати дані: {e}"

    text = resource_query.lower().strip()
    lines = [f"Наступні гільдії і дати коли з'явиться ресурс {html.escape(resource_query)}"]
    found = False
    for res in values:
        if not res or text not in res[0].lower():
            continue
        found = True
        guild_dates = [(GUILD_NAMES[i], d) for i, d in enumerate(res[1:]) if i < len(GUILD_NAMES) and d]
        try:
            guild_dates.sort(key=lambda x: datetime.datetime.strptime(x[1], "%B %d"))
        except ValueError:
            continue
        lines.append(f"<b>{html.escape(res[0])}:</b>")
        for guild, date in guild_dates:
            lines.append(f"{guild}      {date}")

    return "\n".join(lines) if found else None


# -----------------------------------------------------------------------------
# codex search / browse
# -----------------------------------------------------------------------------

def _result_list_keyboard(entries: list[dict], key: str, page: int = 0) -> InlineKeyboardMarkup:
    start = page * _RESULTS_PER_PAGE
    chunk = entries[start:start + _RESULTS_PER_PAGE]
    rows = []
    for i, e in enumerate(chunk):
        tier = e.get("tier")
        sort_value = e.get("sort_value")
        label = e["name"]
        if sort_value is not None:
            label = f"{label} ({sort_value})"
        elif tier:
            label = f"{label} (★{tier})"
        rows.append([InlineKeyboardButton(label[:60], callback_data=f"orna|open|{key}|{start + i}")])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("« Prev", callback_data=f"orna|page|{key}|{page - 1}"))
    if start + _RESULTS_PER_PAGE < len(entries):
        nav.append(InlineKeyboardButton("Next »", callback_data=f"orna|page|{key}|{page + 1}"))
    if nav:
        rows.append(nav)
    return InlineKeyboardMarkup(rows)


def _section_keyboard_rows(sections: list[dict], key: str) -> list:
    """Button rows for an entry's cross-link sections - returns rows
    (not a wrapped InlineKeyboardMarkup) so _send_entry can append an
    "Assess" row before building the final keyboard."""
    rows = []
    for i, section in enumerate(sections):
        entries = section.get("entries") or []
        if not entries:
            continue
        rows.append([InlineKeyboardButton(f"{section['title']} ({len(entries)})"[:60], callback_data=f"orna|sec|{key}|{i}")])
    return rows


def _format_entry(detail: dict) -> str:
    lines = [f"<b>{html.escape(detail.get('name') or '?')}</b>"]
    if detail.get("description"):
        lines.append(html.escape(detail["description"]))
    for fact in detail.get("facts") or []:
        lines.append(f"{html.escape(fact.get('label', ''))}: {html.escape(fact.get('value', ''))}")
    effects = detail.get("effects") or []
    if effects:
        lines.append("")
        lines.append("<b>Ефекти:</b>")
        lines.extend(f"• {html.escape(e)}" for e in effects)
    tags = detail.get("tags") or []
    if tags:
        lines.append("")
        lines.append("Теги: " + ", ".join(html.escape(t) for t in tags))
    return "\n".join(lines)


async def _run_codex_search(message, query: str, lang: str = "en") -> None:
    try:
        data = await asyncio.to_thread(codex_search, query, lang)
    except Exception as e:
        logger.warning("orna: codex search failed for %r", query, exc_info=True)
        await message.reply_text(f"Пошук у кодексі не вдався: {e}")
        return

    results = data.get("results") or []

    # a trailing number is never part of a real codex name (seen live:
    # "solarite 12345" -> 0 results, plain "Solarite" -> 2) - likely a
    # stray quantity/typo tacked onto an otherwise-valid name. Strip it and
    # retry before giving up, same "harmless if unneeded" logic as the
    # space-collapse retry below.
    if not results:
        stripped = re.sub(r"\s+\d+\s*$", "", query).strip()
        if stripped and stripped != query:
            try:
                retry_data = await asyncio.to_thread(codex_search, stripped, lang)
            except Exception:
                retry_data = {}
            retry_results = retry_data.get("results") or []
            if retry_results:
                logger.info("orna: %r found nothing, %r (number stripped) did - using that", query, stripped)
                query, results = stripped, retry_results

    # route_query's translation step non-deterministically splits some
    # compound item names into two words (seen live: "rainsong" ->
    # "Rain Song" on one call, "Rainsong" on the next, same input) - the
    # codex's own search doesn't tolerate that inserted space. Rather than
    # fight an inherently non-deterministic model quirk with more prompt
    # engineering, retry once with the space collapsed before giving up;
    # harmless when the space was already correct, since that case already
    # returned results and never reaches here.
    if not results and " " in query:
        collapsed = query.replace(" ", "")
        try:
            retry_data = await asyncio.to_thread(codex_search, collapsed, lang)
        except Exception:
            retry_data = {}
        retry_results = retry_data.get("results") or []
        if retry_results:
            logger.info("orna: %r found nothing, %r did - using that", query, collapsed)
            query, results = collapsed, retry_results

    # route_query sends anything not obviously a stat/effect/attribute query
    # down this name-lookup path, but some of those are really a description
    # substring (e.g. "strange sword" only appears in "Bladeless"'s
    # description, not its name) - a plain name search here dead-ends, so
    # fall back to a description-text query_records search before giving up.
    if not results:
        try:
            desc_matches = await asyncio.to_thread(
                query_records, [{"kind": "text", "field": "description", "value": query}],
            )
        except Exception:
            desc_matches = []
        if desc_matches:
            logger.info("orna: %r found nothing by name, found %d by description", query, len(desc_matches))
            desc_entries = [{"name": m.name, "url": f"/codex/{m.category}/{m.id}/", "tier": m.tier} for m in desc_matches]
            key = _remember({"entries": desc_entries, "lang": lang})
            await message.reply_text(
                f"🔎 <b>{html.escape(query)}</b> (за описом) — {len(desc_entries)} результат(и)",
                parse_mode="HTML",
                reply_markup=_result_list_keyboard(desc_entries, key),
            )
            return

    if not results:
        await message.reply_text(f"У кодексі нічого не знайдено за запитом: {html.escape(query)}", parse_mode="HTML")
        return

    key = _remember({"entries": results, "lang": lang})
    await message.reply_text(
        f"🔎 <b>{html.escape(query)}</b> — {len(results)} результат(и)",
        parse_mode="HTML",
        reply_markup=_result_list_keyboard(results, key),
    )


_FIELD_LABELS = {"immunities": "імунітет до", "causes": "спричиняє", "gives": "дає", "cures": "лікує"}


def _describe_condition(cond: dict) -> str:
    """Human-readable label for one parsed condition, for the results
    message header - e.g. "magic > 250" or "immune to Stunned"."""
    kind = cond.get("kind")
    field = cond.get("field") or ""
    value = cond.get("value", "")
    if kind == "effect":
        codes = resolve_effect_codes(str(value))
        label = decode_effect_code(codes[0]) if codes else str(value)
        return f"{_FIELD_LABELS.get(field, field)} {label}".strip()
    if kind == "stat":
        return f"{field} {cond.get('cmp', '>')} {value}"
    if kind == "text":
        return f'"{value}" in {field or "name/description"}'
    if kind == "attr":
        return f"{field} {cond.get('cmp', '=')} {value}"
    if kind == "ability":
        return f"дає спелл {value}".strip() if value else "дає бонусний спелл"
    return str(cond)


def _fallback_plan(text: str) -> dict:
    return {"needs_clarification": False, "question": "", "options": [], "queries": [
        {"label": "", "conditions": [{"kind": "text", "field": "", "value": text}],
         "combinator": "and", "category": "", "sort_by": "", "sort_dir": "desc"},
    ]}


async def _execute_queries(message, queries: list) -> None:
    """Run each parsed query block and send its own results message -
    lets a single /orna ask cover several independent searches at once
    (e.g. "best mag item for thieves and for mages" -> two messages),
    while a normal single-block ask behaves exactly as before."""
    for q in queries:
        conditions = q.get("conditions") or []
        sort_by = q.get("sort_by") or None
        try:
            matches = await asyncio.to_thread(
                query_records, conditions, q.get("combinator", "and"), q.get("category") or None,
                50, sort_by, q.get("sort_dir", "desc"),
            )
        except Exception as e:
            logger.warning("orna: query search failed for %r", q, exc_info=True)
            await message.reply_text(f"Пошук не вдався: {e}")
            continue

        joiner = " AND " if q.get("combinator", "and") == "and" else " OR "
        summary = joiner.join(_describe_condition(c) for c in conditions) if conditions else ""
        if sort_by:
            rank_label = f"{'найбільший' if q.get('sort_dir', 'desc') == 'desc' else 'найменший'} {sort_by}"
            summary = f"{summary} — {rank_label}" if summary else rank_label
        label = q.get("label")
        if label:
            summary = f"{label}: {summary}" if summary else label

        if not matches:
            text = f"🔎 <b>{html.escape(summary)}</b> — нічого не знайдено." if summary else "Нічого не знайдено за цим запитом."
            await message.reply_text(text, parse_mode="HTML")
            continue

        # playorna urls, not aussiescodex - tapping a result should show the
        # full stats/facts/sections in chat via _send_entry, same as a name
        # search; the aussiescodex "Assess" link lives on that entry view.
        entries = [{"name": m.name, "url": f"/codex/{m.category}/{m.id}/", "tier": m.tier,
                    "sort_value": m.sort_value} for m in matches]
        suffix = " (показано перші 50)" if len(entries) >= 50 else ""

        key = _remember({"entries": entries, "lang": "en"})
        await message.reply_text(
            f"🔎 <b>{html.escape(summary)}</b> — {len(entries)} результат(и){suffix}",
            parse_mode="HTML",
            reply_markup=_result_list_keyboard(entries, key),
        )


async def _run_query_search(message, text: str) -> None:
    try:
        plan = await plan_queries(text)
    except OllamaError:
        plan = _fallback_plan(text)

    if plan.get("needs_clarification"):
        options = plan["options"]
        key = _remember({"kind": "clarify", "text": text, "options": options})
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton(opt[:30], callback_data=f"orna|clarify|{key}|{i}")
            for i, opt in enumerate(options)
        ]])
        await message.reply_text(plan.get("question") or "Уточніть, будь ласка:", reply_markup=keyboard)
        return

    await _execute_queries(message, plan.get("queries") or [])


async def _send_entry(message, entry_ref: dict, lang: str) -> None:
    url = entry_ref.get("url")
    if not url:
        await message.reply_text("У цього запису немає посилання на сторінку кодексу.")
        return
    try:
        data = await asyncio.to_thread(fetch_codex_json, url, lang)
    except Exception as e:
        logger.warning("orna: failed to fetch codex page %s", url, exc_info=True)
        await message.reply_text(f"Не вдалося завантажити сторінку кодексу: {e}")
        return

    detail = data.get("detail")
    if not detail:
        await message.reply_text("Сторінку кодексу не вдалося розпізнати.")
        return

    sprite = detail.get("sprite")
    if sprite:
        try:
            await message.reply_photo(sprite)
        except TelegramError:
            logger.warning("orna: failed to send entry sprite %s", sprite, exc_info=True)

    sections = detail.get("sections") or []
    key = _remember({"sections": sections, "lang": lang})
    rows = _section_keyboard_rows(sections, key)

    # playorna urls are always "/codex/<category>/<id>/" - reuse that to
    # link an "Assess" button to aussiescodex.com's calculator for the
    # same record, when it has a page (only 4 of 9 categories do).
    parts = [p for p in url.split("/") if p]
    if len(parts) >= 3 and parts[0] == "codex" and has_aussies_page(parts[1]):
        rows.append([InlineKeyboardButton("📊 Assess", url=build_aussies_url(parts[1], parts[2]))])

    await message.reply_text(
        _format_entry(detail),
        parse_mode="HTML",
        disable_web_page_preview=True,
        reply_markup=InlineKeyboardMarkup(rows) if rows else None,
    )


# -----------------------------------------------------------------------------
# handlers
# -----------------------------------------------------------------------------

async def handle_orna(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    usage_stats.record_command("orna")
    message = update.effective_message
    if not message:
        return

    text = " ".join(context.args).strip()
    if not text:
        await message.reply_text(
            "Використання: /orna <запит>\n"
            "Приклади: /orna adamantine, /orna що сьогодні є, /orna balor sword, "
            "/orna what gives immunity to stunned"
        )
        return

    try:
        routed = await route_query(text)
    except OllamaError:
        routed = {"intent": "codex", "query": text}
    intent, query = routed.get("intent", "codex"), routed.get("query") or text

    if intent == "today":
        await message.reply_text(await _today_text())
        return

    if intent == "next":
        result = await _next_text(query)
        if result is not None:
            await message.reply_text(result, parse_mode="HTML", disable_web_page_preview=True)
            return
        # Not a known Material Forecast resource - it might still be a real
        # codex entry, so don't just dead-end here.

    if intent == "query":
        await _run_query_search(message, query)
        return

    await _run_codex_search(message, query)


async def orna_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    parts = (query.data or "").split("|")
    if len(parts) < 4 or parts[0] != "orna":
        return
    kind, key, arg = parts[1], parts[2], parts[3]
    state = _STATE.get(key)
    if state is None:
        await query.message.reply_text("Ця сесія кодексу застаріла — спробуйте /orna ще раз.")
        return

    if kind == "open":
        idx = int(arg) if arg.isdigit() else -1
        entries = state.get("entries") or []
        if not (0 <= idx < len(entries)):
            return
        await _send_entry(query.message, entries[idx], state.get("lang", "en"))
        return

    if kind == "page":
        page = int(arg) if arg.isdigit() else 0
        entries = state.get("entries") or []
        try:
            await query.edit_message_reply_markup(reply_markup=_result_list_keyboard(entries, key, page))
        except TelegramError:
            pass  # e.g. "message not modified" if double-tapped - harmless
        return

    if kind == "sec":
        idx = int(arg) if arg.isdigit() else -1
        sections = state.get("sections") or []
        if not (0 <= idx < len(sections)):
            return
        entries = sections[idx].get("entries") or []
        key2 = _remember({"entries": entries, "lang": state.get("lang", "en")})
        await query.message.reply_text(
            f"<b>{html.escape(sections[idx]['title'])}</b>",
            parse_mode="HTML",
            reply_markup=_result_list_keyboard(entries, key2),
        )
        return

    if kind == "clarify":
        idx = int(arg) if arg.isdigit() else -1
        options = state.get("options") or []
        if not (0 <= idx < len(options)):
            return
        choice = options[idx]
        try:
            await query.edit_message_text(f"{query.message.text}\n\n→ {choice}", reply_markup=None)
        except TelegramError:
            pass  # e.g. double-tapped - harmless, the search below still runs
        original_text = state.get("text", "")
        # clarified=True: plan_queries won't ask a second time (see its
        # docstring) - this can never loop back into another clarify button.
        try:
            plan = await plan_queries(f"{original_text} ({choice})", clarified=True)
        except OllamaError:
            plan = _fallback_plan(original_text)
        await _execute_queries(query.message, plan.get("queries") or [])
        return


async def handle_update_codex(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Hidden maintenance command: force-refetch aussiescodex's codex.json/
    translations.en.json right now, ignoring the 1-week TTL. Gated by the
    same allowlist as /go (GO_ALLOWED_USER_IDS) - not something a regular
    guild member needs, and hammering aussiescodex's API on demand isn't
    something to leave wide open."""
    usage_stats.record_command("update_codex")
    message = update.effective_message
    if not message:
        return

    user = update.effective_user
    if GO_ALLOWED_USER_IDS and (not user or user.id not in GO_ALLOWED_USER_IDS):
        logger.warning("update_codex: rejected user_id=%s", user.id if user else None)
        return

    await message.reply_text("Оновлюю codex.json / translations.en.json з aussiescodex.com...")
    try:
        stats = await asyncio.to_thread(refetch_now)
    except Exception as e:
        logger.warning("update_codex: refetch failed", exc_info=True)
        await message.reply_text(f"Не вдалося оновити: {e}")
        return

    lines = ["✅ Кодекс оновлено:"]
    for cat, count in stats["categories"].items():
        lines.append(f"  {cat}: {count}")
    lines.append(f"stats: {stats['stats_vocab']}, status: {stats['status_vocab']}")
    await message.reply_text("\n".join(lines))


def build_orna_handler() -> CommandHandler:
    return CommandHandler("orna", handle_orna)


def build_orna_callback_handler() -> CallbackQueryHandler:
    return CallbackQueryHandler(orna_callback, pattern=r"^orna\|")


def build_update_codex_handler() -> CommandHandler:
    return CommandHandler("update_codex", handle_update_codex)
