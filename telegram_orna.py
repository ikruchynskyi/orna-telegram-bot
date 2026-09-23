"""
telegram_orna.py
================
`/orna <text>` (English or Ukrainian) - a real ReAct loop over Orna's data:
the model picks a tool itself each turn, reads what it returned, and
decides the next tool call or finishes - rather than a fixed classify-
then-parse pipeline. Mirrors telegram_go.py's `/go` loop (`_advance`,
session dict, one action per turn, button-only "ask") but with /orna's own
tool set and no free-text continuation.

Tools: today/next/need (Material Forecast sheet + proof-cost reports,
same data /res_today, /res_next, and the free-text /need flow serve),
search_codex/query (playorna.com's codex + aussiescodex.com's structured
item/monster/etc. database via orna_aussies.query_records), events
(playorna.com/calendar/'s live event list, via orna_calendar), open_entry
(read one entry's full detail), ask (button-only clarifying question),
finish. Every tool that produces browsable results posts its own rich
Telegram message immediately (result-list buttons, entry detail, event
cards, proof-cost report + reminder buttons) and returns a short text
observation to the model - "finish" is always just a short closing
sentence, never where the actual data lives, so the model never has to
retype a guild/date table or a stat block from memory.

Every codex page - item, class, monster, boss, follower, raid, spell,
building, dungeon - embeds a universal `codex-bootstrap` JSON blob
(facts/effects/tags/sections); orna_codex.fetch_codex_json/codex_search
just extract and return this as-is. `sections[].entries[].url` is the
site's own cross-link graph and can point at a different category than
the current page - that's what makes drilling from an item into the
monster that drops it, then into that monster's own skills, "just work"
with the same functions recursively. Navigation (drilling into a result,
opening a section) sends new messages rather than editing in place -
Telegram's own scrollback becomes the browsing history for free - except
paging through one result list, which edits that list's keyboard.
"""
from __future__ import annotations

import asyncio
import datetime
import html
import json
import logging
import re
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import TelegramError
from telegram.ext import CallbackQueryHandler, CommandHandler, ContextTypes

from ollama_client import OllamaError, UnsupportedMultimodal, chat_json_with_fallback
from orna_aussies import build_url as build_aussies_url
from orna_aussies import decode as decode_effect_code
from orna_aussies import has_aussies_page
from orna_aussies import query_records, refetch_now, resolve_codes as resolve_effect_codes
from orna_calendar import fetch_events
from orna_codex import codex_search, fetch_codex_json
from orna_sheets import GUILD_NAMES, fetch_sheet_data, get_today_month_day
from telegram_go import GO_ALLOWED_USER_IDS, GO_MODEL, OLLAMA_API_KEY
from telegram_nlp import OLLAMA_HOST as LOCAL_OLLAMA_HOST, OLLAMA_MODEL as LOCAL_OLLAMA_MODEL
from telegram_nlp import extract_quantities, extract_resources
from telegram_resources import build_report, send_report_blocks
import usage_stats

logger = logging.getLogger(__name__)

_RESULTS_PER_PAGE = 8
MAX_STEPS = 6
# Hard wall-clock ceiling on one /orna request, regardless of what's
# happening inside it - MAX_STEPS bounds the number of turns, but each
# turn's own timeouts (chat_json_with_fallback: up to 90s cloud + up to
# 90s local fallback; each tool's own network call, all offloaded via
# asyncio.to_thread) can still add up to several minutes worst-case
# across 6 steps. This is the actual guarantee that the loop always
# replies within a bounded time no matter what any single step does -
# a hung/slow chain gets cut off here instead of the user just waiting
# indefinitely with no way to tell a slow loop from a stuck one.
LOOP_TIMEOUT_SECONDS = 180
# ponytail: fixed TTL + a hard cap, pruned opportunistically on each new
# /orna call - same tradeoff telegram_go._SESSIONS makes. No persistence,
# no real LRU; add if session volume ever outgrows one process's memory
# between bot restarts.
SESSION_TTL_SECONDS = 15 * 60
MAX_SESSIONS = 50

# callback_data can't carry a full url/query list (64-byte cap), so each
# rendered message's buttons reference a short-lived key into this dict
# instead - unrelated to _ORNA_SESSIONS below (that's loop state; this is
# result-list/section browsing state), same split telegram_go.py doesn't
# need since it only ever has one kind of session.
_STATE: dict[str, dict] = {}
_STATE_MAX = 200


def _remember(state: dict) -> str:
    if len(_STATE) >= _STATE_MAX:
        _STATE.pop(next(iter(_STATE)), None)
    key = uuid.uuid4().hex[:10]
    _STATE[key] = state
    return key


def _capabilities_text() -> str:
    """Fixed, deterministic reply for a meta "what can you do" ask -
    deliberately NOT model-generated prose (same reasoning as every other
    structured-over-freeform choice in this codebase). The system prompt
    tells the model to copy this verbatim into finish() rather than write
    its own. Only mentions genuinely public commands - /go and its hidden
    siblings stay unlisted here same as everywhere else."""
    return (
        "Я вмію відповідати на питання про Orna:\n"
        "• /orna <назва> — знайти предмет/боса/клас/спел у кодексі "
        "(напр. /orna balor sword)\n"
        "• /orna <питання> — пошук за характеристиками чи ефектами "
        '(напр. "mag > 250", "що дає імунітет до оглушення", '
        '"шоломи для мага, крім зброї")\n'
        "• /orna що сьогодні — ресурси, доступні сьогодні\n"
        "• /orna <ресурс> — коли з'явиться ресурс\n"
        "• /orna коли наступний івент — календар подій гри\n"
        "• /res_today, /res_next — те саме окремими командами\n"
        "• /remind <час> <текст> — поставити нагадування (це окрема команда, "
        "не /orna)"
    )


_REMINDER_NUDGE = (
    "Це /orna — нагадування я тут не ставлю. Скористайтесь командою /remind, "
    "наприклад: /remind 18:00 купити пруфи."
)


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


async def _run_today_tool(message) -> str:
    text = await _today_text()
    await message.reply_text(text)
    return "sent today's resources to the user"


async def _run_next_tool(message, material: str) -> str:
    if not material:
        return "next needs a material name in action_input"
    result = await _next_text(material)
    if result is None:
        return f"{material!r} is not a known Material Forecast resource - try search_codex or query instead"
    await message.reply_text(result, parse_mode="HTML", disable_web_page_preview=True)
    return f"sent next-appearance dates for {material} to the user"


async def _run_need_tool(message, text: str) -> str:
    """"need": a quantity was given for one or more materials (e.g.
    "треба 1000 балоріту") - reuses the exact same extraction + report
    pipeline the free-text /need flow (telegram_resources.py) already
    has, rather than next()'s plain date lookup with no proof-cost math.
    Always posts something itself (a report, a fallback next() reply, or
    falls through to search_codex) - never silently drops the request."""
    if not text:
        return "need needs the original request text in action_input"
    try:
        sheet_values = await fetch_sheet_data()
    except Exception as e:
        await message.reply_text(f"Не вдалося отримати дані: {e}")
        return f"failed to fetch sheet data: {e}"

    known = [row[0] for row in sheet_values if row]
    try:
        resources = await extract_resources(text, known)
    except OllamaError:
        resources = []
    if not resources:
        # Not a recognized Material Forecast material at all - might still
        # be a real codex entry, same reasoning next() already uses.
        return await _run_codex_search(message, text)

    try:
        quantities = await extract_quantities(text, resources)
    except OllamaError:
        quantities = {}

    summary_parts = []
    if quantities:
        blocks, bundles = await build_report(quantities, sheet_values)
        await send_report_blocks(message, blocks, bundles)
        summary_parts.append("sent proof-cost report for: " + ", ".join(f"{n} x{q}" for n, q in quantities.items()))

    missing = [r for r in resources if r not in quantities]
    for name in missing:
        # Couldn't pin a quantity to this one - fall back to a plain
        # "when does it appear" lookup instead of silently dropping it.
        result = await _next_text(name)
        if result:
            await message.reply_text(result, parse_mode="HTML", disable_web_page_preview=True)
            summary_parts.append(f"sent next-appearance for {name} (no quantity given)")

    return "; ".join(summary_parts) if summary_parts else "no recognizable material+quantity found"


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
    """Button rows for a list of {"title", "entries"} sections - returns
    rows (not a wrapped InlineKeyboardMarkup) so callers can append extra
    rows (e.g. an "Assess" button) before building the final keyboard.
    Reused for both a codex entry's own cross-link sections AND an
    events() card's roster categories (Raids/Bosses/Followers/...) -
    structurally the same shape (a title + a list of entries), so no
    separate rendering path was needed for the calendar feature."""
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


async def _run_codex_search(message, query: str, lang: str = "en") -> str:
    if not query:
        return "search_codex needs a name in action_input"
    try:
        data = await asyncio.to_thread(codex_search, query, lang)
    except Exception as e:
        logger.warning("orna: codex search failed for %r", query, exc_info=True)
        await message.reply_text(f"Пошук у кодексі не вдався: {e}")
        return f"codex search failed: {e}"

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

    # A translated request can non-deterministically split a compound item
    # name into two words (seen live: "rainsong" -> "Rain Song" on one
    # call, "Rainsong" on the next) - the codex's own search doesn't
    # tolerate that inserted space. Retry once with the space collapsed
    # before giving up; harmless when the space was already correct, since
    # that case already returned results and never reaches here.
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

    # A name search dead-ends on a request that's really a description
    # substring (e.g. "strange sword" only appears in "Bladeless"'s
    # description, not its name) - fall back to a description-text
    # query_records search before giving up.
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
            names = "; ".join(f"{e['name']} ({e['url']})" for e in desc_entries[:5])
            return f"{len(desc_entries)} matches by description for {query!r}: {names}"

    if not results:
        await message.reply_text(f"У кодексі нічого не знайдено за запитом: {html.escape(query)}", parse_mode="HTML")
        return f"0 results for {query!r}"

    key = _remember({"entries": results, "lang": lang})
    await message.reply_text(
        f"🔎 <b>{html.escape(query)}</b> — {len(results)} результат(и)",
        parse_mode="HTML",
        reply_markup=_result_list_keyboard(results, key),
    )
    names = "; ".join(f"{r.get('name', '?')} ({r.get('url', '')})" for r in results[:5])
    return f"{len(results)} results for {query!r}: {names}"


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


async def _run_query_tool(message, conditions: list, combinator: str, category: str, sort_by: str, sort_dir: str) -> str:
    conditions = [c for c in conditions if isinstance(c, dict)] if isinstance(conditions, list) else []
    if not conditions and not sort_by:
        return "query needs at least one condition, or a sort_by for a ranking ask"

    try:
        matches = await asyncio.to_thread(
            query_records, conditions, combinator if combinator in ("and", "or") else "and",
            category or None, 50, sort_by or None, sort_dir if sort_dir in ("asc", "desc") else "desc",
        )
    except Exception as e:
        logger.warning("orna: query tool failed for %r", conditions, exc_info=True)
        await message.reply_text(f"Пошук не вдався: {e}")
        return f"query failed: {e}"

    joiner = " AND " if combinator != "or" else " OR "
    summary = joiner.join(_describe_condition(c) for c in conditions) if conditions else ""
    if sort_by:
        rank_label = f"{'найбільший' if sort_dir != 'asc' else 'найменший'} {sort_by}"
        summary = f"{summary} — {rank_label}" if summary else rank_label
    if category:
        summary = f"[{category}] {summary}" if summary else f"[{category}]"

    if not matches:
        text = f"🔎 <b>{html.escape(summary)}</b> — нічого не знайдено." if summary else "Нічого не знайдено за цим запитом."
        await message.reply_text(text, parse_mode="HTML")
        return f"0 matches for: {summary or conditions}"

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
    names = "; ".join(f"{e['name']} ({e['url']})" for e in entries[:5])
    return f"{len(matches)} matches for {summary}: {names}"


async def _run_events_tool(message, keyword: str) -> str:
    try:
        events = await asyncio.to_thread(fetch_events)
    except Exception as e:
        logger.warning("orna: events fetch failed", exc_info=True)
        return f"events fetch failed: {e}"
    if not events:
        return "No events currently listed on the calendar."

    shown = events
    needle = keyword.strip().lower()
    if needle:
        filtered = [e for e in events if needle in e["name"].lower() or needle in e["description"].lower()]
        if filtered:
            shown = filtered
        # else: keyword matched nothing - fall back to the full list rather
        # than dead-ending, same "harmless retry" pattern as _run_codex_search.

    summaries = []
    for e in shown:
        live_tag = " 🔴 LIVE" if e["live"] else ""
        text = (f"<b>{html.escape(e['name'])}</b>{live_tag}\n"
                f"{html.escape(e['starts'])} – {html.escape(e['ends'])}\n\n"
                f"{html.escape(e['description'])}")
        sections = [{"title": cat, "entries": items} for cat, items in e["roster"].items()]
        rows = []
        if sections:
            key = _remember({"sections": sections, "lang": "en"})
            rows = _section_keyboard_rows(sections, key)
        await message.reply_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(rows) if rows else None)
        summaries.append(f"{e['name']} ({e['starts']} to {e['ends']}, live={e['live']}): {e['description']}")
    return "\n".join(summaries)


async def _send_entry(message, entry_ref: dict, lang: str) -> Optional[dict]:
    """Posts the full rendered entry (sprite, facts/effects/tags, cross-
    link section buttons, Assess link) and returns its `detail` dict so a
    caller (open_entry's tool wrapper) can build a text digest from it -
    the button-driven "open" callback ignores the return value, same as
    before this returned nothing."""
    url = entry_ref.get("url")
    if not url:
        await message.reply_text("У цього запису немає посилання на сторінку кодексу.")
        return None
    try:
        data = await asyncio.to_thread(fetch_codex_json, url, lang)
    except Exception as e:
        logger.warning("orna: failed to fetch codex page %s", url, exc_info=True)
        await message.reply_text(f"Не вдалося завантажити сторінку кодексу: {e}")
        return None

    detail = data.get("detail")
    if not detail:
        await message.reply_text("Сторінку кодексу не вдалося розпізнати.")
        return None

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
    return detail


async def _run_open_entry_tool(message, url: str) -> str:
    if not url:
        return "open_entry needs a url in action_input (from a previous observation)"
    detail = await _send_entry(message, {"url": url}, "en")
    if not detail:
        return f"couldn't open {url}"
    facts = "; ".join(f"{f.get('label')}: {f.get('value')}" for f in (detail.get("facts") or [])[:8])
    digest = f"{detail.get('name')}: {facts}"
    effects = detail.get("effects") or []
    if effects:
        digest += " | effects: " + ", ".join(effects)
    return digest


# -----------------------------------------------------------------------------
# the ReAct loop
# -----------------------------------------------------------------------------

_TOOLS_TEXT = (
    "- today(): no input. Materials available today in the guild shops (Material Forecast sheet). Posts the list.\n"
    "- next(action_input=<material name, English>): when/where a SPECIFIC named crafting material next appears, "
    "no quantity involved. If it's not a known Material Forecast resource, try search_codex or query instead - "
    "it might still be a real codex entry (a monster, a non-shop item, ...).\n"
    "- need(action_input=<the relevant original wording, quantity + material - do NOT translate this one, the "
    "extraction step handles Ukrainian directly>): the user gave an actual QUANTITY of one or more materials "
    '(e.g. "треба 1000 балоріту", "need 500 mythril and 200 adamantine") - runs the full guild-availability + '
    "proof-cost + \"remind me\" report, richer than next().\n"
    "- search_codex(action_input=<name, English>): look up ONE specific item/monster/boss/class/spell/building/"
    "dungeon/follower/raid by NAME alone. Only for a plain name/set-fragment lookup with NO attribute/class/slot "
    'restriction attached - a name PLUS a restriction (e.g. "Last Martyr items for mage") is a query() call '
    "instead (a text condition on the name plus an attr condition), not search_codex.\n"
    "- query(args={...}): search the full item/monster/boss/class/spell/building/dungeon/follower/raid database "
    "by attributes - see the condition rules below.\n"
    "- events(action_input=<keyword, or empty>): the current/near-term event calendar (double-orns weekends, EXP "
    "events, raids, ...). Posts each matching event (dates + description) and returns a short summary - read the "
    "description text yourself to judge a match (wording varies a lot: \"earn 25% more orns\" and \"double orns, "
    'gold, and experience" both mean "gives more orns"). Leave action_input empty to see everything currently '
    "listed; only pass a keyword once you already know roughly what you're narrowing to.\n"
    "- open_entry(action_input=<url from a previous observation, e.g. \"/codex/items/foo/\">): fetch and show the "
    "user one specific entry's full detail, and read a digest of it yourself - use this only if you need to "
    "confirm an exact stat/fact before answering, not for browsing (query/search_codex already show a full "
    "result list with buttons the user can open themselves).\n"
    "- ask(action_input=<question>, options=[2-4 short choices]): a clarifying question. The user can only TAP a "
    "button, never type free text - always give options. Only when a specific missing detail would materially "
    'change the results and there\'s no reasonable default (e.g. "good gear for my class" names no class). Most '
    "requests do NOT need this. Never ask twice in the same conversation.\n"
    "- finish(action_input=<short closing text>): end the turn. The actual results (search hits, reports, event "
    "cards) are ALREADY shown to the user by whichever tool produced them - finish is just a short closing "
    'sentence (e.g. "Ось варіанти для обох слотів."), or, for the two fixed-reply cases below, the exact fixed '
    "text. Don't call finish before you have enough information.\n"
)

_CONDITION_RULES = (
    "query's args: {\"conditions\": [<condition>, ...], \"combinator\": \"and\"|\"or\", \"category\": \"<one of "
    'items, monsters, bosses, raids, followers, classes, spells, buildings, dungeons, or empty for all>", '
    '"sort_by": "<stat field or empty>", "sort_dir": "asc"|"desc"}. ONE query call = one filter - if the request '
    "names several separate things to look up (different slots, different classes, different items), call query "
    "multiple times, once per thing (see the worked example below) - never cram unrelated asks into one call's "
    "conditions.\n"
    "Each condition is one of:\n"
    '  {"kind":"stat","field":"<snake_case stat name>","cmp":">|<|>=|<=|=","value":<number, may be negative>} - '
    "field is not a fixed list: attack/magic/defense/resistance/dexterity/ward/foresight/crit/crit_chance/"
    "crit_damage(a SEPARATE stat from crit - how much extra damage a crit does, never conflate them)/"
    "follower_stats/summon_stats/view_distance/gold_bonus/exp_bonus/... - infer the snake_case name from the "
    "wording. A NUMBER/threshold on a stat is ALWAYS kind:\"stat\" even worded as \"gives\"/\"has\" (\"magic over "
    '220" is stat, not effect). Negative values are fine ("defense < 0").\n'
    '  {"kind":"effect","field":"immunities|causes|gives|cures|","value":"<effect name>"} - immune to / causes on '
    "an enemy / grants (self-or-team buff, including a follower's bond proc) / cures a NAMED status (e.g. "
    '"stunned", "T Mag 3", "Def Down") - never a bare number. Tier shorthand ("T Mag ++", "Mag ↑↑", "T Mag 3", '
    '"Def III") is ALWAYS effect - copy it into value EXACTLY as written, never invent or drop the tier.\n'
    '  {"kind":"text","field":"description|name|","value":"<substring>"}\n'
    '  {"kind":"attr","field":"<flat field>","cmp":"=|!=|>|<|>=|<=","value":<text, number, or true/false>} - '
    "tier, rarity, useable_by (magic_users/melee_classes/thief_classes/warrior_classes/"
    'valhallan_summoner_classes/all_classes - "mage"/"thief" etc as a substring is fine), place (the BODY SLOT: '
    'head/torso/legs/weapon/off-hand/accessory/material - use for "goes on legs/head", never "type"), type '
    "(weapon SUBTYPE only, e.g. daggers/axes_&_hammers), item_type (the broad equipment slot: armor/weapon/"
    'off-hand/field), family, element, events, tags, price, and boolean exotic/new/hidden ("true"/"false"). Use '
    'cmp:"!=" for exclusion language ("not"/"except"/"excluding").\n'
    '  {"kind":"ability","value":"<spell/skill name, or empty for any>"} - the record ITSELF grants a spell/skill '
    "when equipped/bonded. For an ITEM this lives in stats[\"+spell\"]/[\"+skill\"] or an ability cross-link; for "
    "a FOLLOWER this is a bestial_bond tier's ABILITY entry - either way, use kind:\"ability\" whenever a "
    'SPECIFIC SPELL/SKILL NAME is named (or "gives a bonus/extra spell" with no name given), NEVER kind:"effect" '
    "- \"effect\" is only for buff/debuff status codes (Up/Down/a tier number/an ailment), never a spell's own "
    'name. E.g. "which follower gives earth sigil" -> category:"followers", kind:"ability", value:"earth sigil" '
    "(Earth Sigil is a SPELL, not a status effect - this exact phrasing was previously misread as an effect and "
    "found nothing).\n"
    '"combinator": "and" (default) or "or". "sort_by"/"sort_dir": for a ranking ask ("biggest mag item", "weakest '
    'defense follower") instead of (or together with) a plain filter - sort_dir "desc" for biggest/highest/best, '
    '"asc" for smallest/lowest/worst; conditions may be empty for a pure-ranking ask.'
)

_MULTI_PART_EXAMPLE = (
    "MULTI-PART REQUESTS: when a request names several separate things to look up, call query (or search_codex) "
    "once PER thing, observe each result, then finish once with a short overall wrap-up - never force unrelated "
    'asks into one call. Worked example: "I need two separate items. Legs and head. For mage. Mag stat should be '
    'more than 50." is TWO lookups, not one:\n'
    '  1. query(args={"conditions":[{"kind":"attr","field":"place","cmp":"=","value":"legs"},'
    '{"kind":"attr","field":"useable_by","cmp":"=","value":"magic"},{"kind":"stat","field":"magic","cmp":">",'
    '"value":50}],"combinator":"and"})\n'
    '  2. same again with the place condition\'s value changed to "head"\n'
    '  3. finish(action_input="Ось варіанти для обох слотів.")'
)


def _orna_system_prompt() -> str:
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M %A")
    actions = '"today"|"next"|"need"|"search_codex"|"query"|"events"|"open_entry"|"ask"|"finish"'
    return (
        'You are a ReAct agent answering /orna requests about the mobile RPG "Orna" for a Telegram bot used by '
        f'its guild - requests come in English or Ukrainian. Current date/time: {now} (server local time) - use '
        'this for "today"/"next event"/other relative dates. Reply text (ask/finish action_input) is in the SAME '
        "language the user wrote in; tool arguments (action_input for other tools, query conditions) are always "
        "in ENGLISH regardless of the request's language, since the underlying data is English.\n\n"
        f"You have these tools - each turn, pick exactly ONE:\n{_TOOLS_TEXT}\n"
        f"{_CONDITION_RULES}\n\n"
        f"{_MULTI_PART_EXAMPLE}\n\n"
        "FIXED REPLIES - copy verbatim into finish()'s action_input, do not paraphrase or write your own version:\n"
        f"- a meta \"what can you do\"/\"help\"/\"допоможи\" ask with no real Orna subject: {_capabilities_text()!r}\n"
        f'- a message shaped like a reminder request ("нагадай мені...", "remind me to..."): {_REMINDER_NUDGE!r}\n\n'
        "Each turn, reply with strict JSON only, no other text: "
        f'{{"thought":"<brief reasoning>","action":{actions},"action_input":"<string, unused for query/today>",'
        '"args":{"...only for action \\"query\\", see above..."},"options":["<opt1>","<opt2>"]}. "options" is only '
        "used with action \"ask\". Don't call finish before you have enough information, don't ask more than "
        "once, and don't repeat a tool call you've already made with the same input."
    )


@dataclass
class OrnaSession:
    messages: list
    steps_left: int
    created: float = field(default_factory=time.monotonic)
    ask_options: list = field(default_factory=list)


_ORNA_SESSIONS: dict[str, OrnaSession] = {}


def _new_orna_session(messages: list, steps_left: int) -> str:
    now = time.monotonic()
    for sid in [s for s, sess in _ORNA_SESSIONS.items() if now - sess.created > SESSION_TTL_SECONDS]:
        _ORNA_SESSIONS.pop(sid, None)
    if len(_ORNA_SESSIONS) >= MAX_SESSIONS:
        oldest = min(_ORNA_SESSIONS, key=lambda s: _ORNA_SESSIONS[s].created)
        _ORNA_SESSIONS.pop(oldest, None)
    sid = uuid.uuid4().hex[:10]
    _ORNA_SESSIONS[sid] = OrnaSession(messages=messages, steps_left=steps_left)
    return sid


async def _run_tool(message, action: str, action_input: str, args: dict) -> str:
    """Dispatch one tool call. Wrapped in a broad except so a bug in any
    single tool ends that step with an observation the model can react to,
    instead of killing the whole loop (defense in depth alongside
    telegram_bot.py's global error handler - the loop itself should never
    need that safety net to produce a reply)."""
    try:
        if action == "today":
            return await _run_today_tool(message)
        if action == "next":
            return await _run_next_tool(message, action_input)
        if action == "need":
            return await _run_need_tool(message, action_input)
        if action == "search_codex":
            return await _run_codex_search(message, action_input)
        if action == "query":
            return await _run_query_tool(
                message, args.get("conditions") or [], str(args.get("combinator") or "and"),
                str(args.get("category") or ""), str(args.get("sort_by") or ""), str(args.get("sort_dir") or "desc"),
            )
        if action == "events":
            return await _run_events_tool(message, action_input)
        if action == "open_entry":
            return await _run_open_entry_tool(message, action_input)
    except Exception as e:
        logger.warning("orna: tool %r failed", action, exc_info=True)
        return f"{action} failed: {e}"
    return f"unknown action {action!r}; valid actions are today, next, need, search_codex, query, events, open_entry, ask, finish."


async def _advance(sid: str, message) -> None:
    """Wraps _advance_inner in a hard wall-clock deadline - see
    LOOP_TIMEOUT_SECONDS. No matter what happens inside (a hung call, a
    pathologically slow chain of fallbacks, anything), this guarantees a
    reply within a bounded time instead of the request just going quiet."""
    try:
        await asyncio.wait_for(_advance_inner(sid, message), timeout=LOOP_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        logger.warning("orna: loop exceeded %ss, cut off (sid=%s)", LOOP_TIMEOUT_SECONDS, sid)
        try:
            await message.reply_text("Запит триває надто довго — спробуйте ще раз або сформулюйте простіше.")
        except Exception:
            logger.warning("orna: failed to notify user about loop timeout", exc_info=True)


async def _advance_inner(sid: str, message) -> None:
    session = _ORNA_SESSIONS.get(sid)
    if session is None:
        return

    while session.steps_left > 0:
        session.steps_left -= 1
        try:
            step = await chat_json_with_fallback(
                GO_MODEL, LOCAL_OLLAMA_HOST, LOCAL_OLLAMA_MODEL, session.messages, api_key=OLLAMA_API_KEY,
            )
        except (OllamaError, UnsupportedMultimodal) as e:
            logger.warning("orna: model call failed", exc_info=True)
            await message.reply_text(f"Не вдалося обробити запит: {e}")
            return

        action = step.get("action")
        action_input = str(step.get("action_input") or "").strip()
        args = step.get("args") if isinstance(step.get("args"), dict) else {}

        if action == "finish" or not action:
            await message.reply_text(action_input or "Не вдалося сформувати відповідь.")
            return

        if action == "ask":
            options = [str(o).strip() for o in (step.get("options") or []) if str(o).strip()][:4]
            if not options:
                session.messages.append({
                    "role": "user",
                    "content": 'Observation: "ask" needs 2-4 short "options" to tap - '
                               "there's no free-text reply channel here. Retry with options, or finish.",
                })
                continue
            session.ask_options = options
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton(opt[:30], callback_data=f"orna|ask|{sid}|{i}")
                for i, opt in enumerate(options)
            ]])
            await message.reply_text(action_input or "Уточніть, будь ласка:", reply_markup=keyboard)
            return

        session.messages.append({"role": "assistant", "content": json.dumps(step)})
        observation = await _run_tool(message, action, action_input, args)
        session.messages.append({"role": "user", "content": f"Observation: {observation}"})

    await message.reply_text("Не вдалося сформувати відповідь за відведену кількість кроків — спробуйте уточнити запит.")


# -----------------------------------------------------------------------------
# handlers
# -----------------------------------------------------------------------------

async def handle_orna(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message:
        return

    text = " ".join(context.args).strip()
    usage_stats.record_command_for(update, "orna", text)
    if not text:
        await message.reply_text(
            "Використання: /orna <запит>\n"
            "Приклади: /orna adamantine, /orna що сьогодні є, /orna balor sword, "
            "/orna what gives immunity to stunned, /orna коли наступний івент"
        )
        return

    messages = [
        {"role": "system", "content": _orna_system_prompt()},
        {"role": "user", "content": text},
    ]
    sid = _new_orna_session(messages, MAX_STEPS)
    await _advance(sid, message)


async def orna_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    parts = (query.data or "").split("|")
    if len(parts) < 4 or parts[0] != "orna":
        return
    kind, key, arg = parts[1], parts[2], parts[3]

    if kind == "ask":
        session = _ORNA_SESSIONS.get(key)
        if session is None:
            await query.message.reply_text("Ця сесія застаріла — спробуйте /orna ще раз.")
            return
        idx = int(arg) if arg.isdigit() else -1
        if not (0 <= idx < len(session.ask_options)):
            return
        choice = session.ask_options[idx]
        try:
            await query.edit_message_text(f"{query.message.text}\n\n→ {choice}", reply_markup=None)
        except TelegramError:
            pass  # e.g. double-tapped - harmless, the loop resume below still runs
        session.messages.append({"role": "user", "content": f'Observation: user chose "{choice}".'})
        await _advance(key, query.message)
        return

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


async def handle_update_codex(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Hidden maintenance command: force-refetch aussiescodex's codex.json/
    translations.en.json right now, ignoring the 1-week TTL. Gated by the
    same allowlist as /go (GO_ALLOWED_USER_IDS) - not something a regular
    guild member needs, and hammering aussiescodex's API on demand isn't
    something to leave wide open."""
    usage_stats.record_command_for(update, "update_codex")
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
