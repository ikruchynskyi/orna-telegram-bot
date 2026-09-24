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
(read one entry's full detail), calculate, assess (name+quality stat
projection), compare (N items' assessed stats, diffed), build_optimize
(native multi-slot stacking-bonus optimizer), towers (live Wild Tower of
Olympia floor heights, orna_towers.py), class_guide (long-form community
class/build guides, orna_guides.py), knowledge_search/web_search, ask
(button-only clarifying question), finish. Every tool that produces
browsable results posts its own rich
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
import httpx
import datetime
import html
import io
import json
import logging
import re
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, Optional

from PIL import Image
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import TelegramError
from telegram.ext import CallbackQueryHandler, CommandHandler, ContextTypes

from ollama_client import OllamaError, UnsupportedMultimodal, chat_json, chat_json_with_fallback
from orna_aussies import build_url as build_aussies_url
from orna_aussies import decode as decode_effect_code
from orna_aussies import display_name
from orna_aussies import has_aussies_page
from orna_aussies import query_records, refetch_now, resolve_codes as resolve_effect_codes
from orna_aussies import _codex as _aussies_codex
from orna_aussies import _parse_number as _aussies_parse_number
from orna_calendar import CALENDAR_URL_UK, fetch_events
import orna_guides
import orna_knowledge
import orna_towers
from orna_assess import (
    AssessInput, CodexEntry, QUALITY_CODE_BONUS_KEYS, get_assess_result, get_quality_bonus, get_quality_code,
)
from orna_codex import clear_cache as clear_codex_cache, codex_search, fetch_codex_json
from telegram_assess import _format_response
from orna_sheets import GUILD_NAMES, fetch_sheet_data, get_today_month_day
from telegram_go import (
    GO_ALLOWED_USER_IDS, GO_MODEL, OLLAMA_API_KEY, TAVILY_API_KEY, _calculate, _reply_markdown, _tavily_search,
)
from telegram_nlp import OLLAMA_HOST as LOCAL_OLLAMA_HOST, OLLAMA_MODEL as LOCAL_OLLAMA_MODEL
from telegram_nlp import extract_quantities, extract_resources
from telegram_resources import build_report, pre_table, send_report_blocks
import usage_stats

logger = logging.getLogger(__name__)

_RESULTS_PER_PAGE = 8
# 6 was enough before web_search existed (a multi-part query is usually
# 2-3 steps), but a "how do I beat X" strategy question now genuinely
# needs codex-miss + retry + open_entry + knowledge_search/web_search
# (+ maybe a refined retry) + finish. 16 gives real headroom for that
# chain plus a genuinely multi-part request on top of it.
MAX_STEPS = 16
# Only the first CLOUD_STEPS turns are allowed to try the cloud model at
# all (still falling back to local mid-turn if the cloud call itself
# fails, same as always) - turns past that go straight to local, no cloud
# attempt. A request still running this long is already the unusual case;
# spending more cloud quota/cost on it isn't worth it when local can
# still finish the reasoning for free.
CLOUD_STEPS = 8
# Shorter than ollama_client.DEFAULT_TIMEOUT's 90s read timeout (which
# /go still uses, unchanged, via its own default call) - /orna's loop has
# a hard step budget where a slow/hanging call is pure waste (it can
# always fall back to local, or retry, or just move on), unlike /go's
# single-shot-per-turn use where waiting out a genuinely-slow-but-working
# cloud response is more worth it. Live incident: one step spent ~2.5
# minutes (a full 90s cloud timeout, then a slow local response) before
# giving up - this halves the worst case per attempt.
STEP_MODEL_TIMEOUT = httpx.Timeout(connect=10.0, read=45.0, write=20.0, pool=10.0)
# Hard wall-clock ceiling on one /orna request, regardless of what's
# happening inside it - MAX_STEPS bounds the number of turns, but each
# turn's own timeouts (chat_json_with_fallback: up to 90s cloud + up to
# 90s local fallback; each tool's own network call, all offloaded via
# asyncio.to_thread) can still add up to several minutes worst-case
# across MAX_STEPS steps. This is the actual guarantee that the loop
# always replies within a bounded time no matter what any single step
# does - a hung/slow chain gets cut off here instead of the user just
# waiting indefinitely with no way to tell a slow loop from a stuck one.
# Bumped alongside MAX_STEPS 8->16 to keep giving a legitimately-slow (not
# hung) full-length run enough real time to finish rather than getting
# cut off mid-reasoning.
LOOP_TIMEOUT_SECONDS = 300
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
        "• /orna яка зараз висота веж Олімпії — стан 5 диких веж просто зараз\n"
        "• /orna порівняй X і Y — порівняння речей за прокачаними характеристиками\n"
        "• /orna найкращий орн-бонус по слотах для мага — оптимальний білд по слотах\n"
        "• /orna що по білду summoner/thief/deity/gilgamesh/beowulf/swash/heretic — гайди спільноти по класах\n"
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

    table = pre_table([[guild, ", ".join(materials)] for guild, materials in tdg.items()])
    return f"<b>Ресурси {html.escape(today)}</b>\n{table}"


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
        guild_dates = [(GUILD_NAMES[i], d) for i, d in enumerate(res[1:]) if i < len(GUILD_NAMES) and d]
        try:
            guild_dates.sort(key=lambda x: datetime.datetime.strptime(x[1], "%B %d"))
        except ValueError:
            continue
        # Only mark found once we actually have a table to show - setting it
        # on the name match alone meant an all-unparseable-dates row returned
        # a header-only, non-None string, so callers treated that as a
        # successful answer instead of falling through to codex search.
        found = True
        lines.append(f"<b>{html.escape(res[0])}:</b>")
        lines.append(pre_table([[g, d] for g, d in guild_dates]))

    return "\n".join(lines) if found else None


async def _run_today_tool(message) -> str:
    text = await _today_text()
    await message.reply_text(text, parse_mode="HTML")
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


def _pack_buttons(buttons: list, per_row: int = 2) -> list:
    """Groups a flat button list into rows of `per_row` - Telegram divides
    a row's width evenly among its buttons, so several per row (instead
    of the old one-button-per-row layout) makes each button take a
    proportional share of the message width instead of spanning it full
    width. Live report: on a wide (desktop) client, a short label like
    "Gives (1)" in a full-width button looked oversized/clunky - a real
    Telegram Bot API constraint worth knowing here: there is no "text
    link that fires a callback", only an actual InlineKeyboardButton can
    carry callback_data, a plain link can only open a URL - so a
    tighter grid, not links, is the fix."""
    return [buttons[i:i + per_row] for i in range(0, len(buttons), per_row)]


def _section_buttons(sections: list[dict], key: str) -> list:
    """Flat button list (not rows - see _pack_buttons) for a list of
    {"title", "entries"} sections. Reused for both a codex entry's own
    cross-link sections AND an events() card's roster categories (Raids/
    Bosses/Followers/...) - structurally the same shape (a title + a
    list of entries), so no separate rendering path was needed for the
    calendar feature."""
    buttons = []
    for i, section in enumerate(sections):
        entries = section.get("entries") or []
        if not entries:
            continue
        buttons.append(InlineKeyboardButton(f"{section['title']} ({len(entries)})"[:60], callback_data=f"orna|sec|{key}|{i}"))
    return buttons


def _format_entry(detail: dict) -> str:
    lines = [f"<b>{html.escape(detail.get('name') or '?')}</b>"]
    if detail.get("description"):
        lines.append(html.escape(detail["description"]))
    # Stats/facts as a monospace <pre> table (label | value), the same
    # aligned "pretty table" look /res_today uses (pre_table), instead of
    # ragged "Label: value" lines that don't line up.
    facts = [[fact.get("label", ""), fact.get("value", "")] for fact in (detail.get("facts") or [])]
    if facts:
        lines.append(pre_table(facts))
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

    # Two more transliteration slips, seen live together on one request
    # ("клятий ортаніт" -> "Cursed Ortannite"/then "Ortannite", both 0
    # results - the real name is "Ortanite"): (1) a doubled letter from an
    # inexact Ukrainian->English transliteration, and (2) a leading word
    # that's colloquial emphasis ("клятий"/"damn/cursed") rather than a
    # real item-name modifier, mistranslated as if it were one. Try both,
    # and both together (drop the leading word, THEN dedupe) - harmless if
    # unneeded, same as every retry above.
    if not results:
        candidates = []
        deduped = re.sub(r"(.)\1+", r"\1", query)
        if deduped != query:
            candidates.append(deduped)
        if " " in query:
            _, _, rest = query.partition(" ")
            if rest:
                candidates.append(rest)
                rest_deduped = re.sub(r"(.)\1+", r"\1", rest)
                if rest_deduped != rest:
                    candidates.append(rest_deduped)
        tried = {query}
        for candidate in candidates:
            if candidate in tried:
                continue
            tried.add(candidate)
            try:
                retry_data = await asyncio.to_thread(codex_search, candidate, lang)
            except Exception:
                retry_data = {}
            retry_results = retry_data.get("results") or []
            if retry_results:
                logger.info("orna: %r found nothing, %r did - using that", query, candidate)
                query, results = candidate, retry_results
                break

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

    # Deliberately no reply_text here on a genuine dead end: this is an
    # intermediate step the model can still recover from (retry a
    # different name, fall back to query()) - posting "nothing found" as
    # its own chat message for every failed attempt along the way is
    # exactly the noise reported live (several dead-end messages plus a
    # step-budget-exhausted message, for a request that should have been
    # one clean answer). Only a genuinely final "nothing anywhere" belongs
    # in the user's chat, and that's finish()'s job once the model gives up.
    if not results:
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
        # No reply_text here - same reasoning as _run_codex_search's dead
        # end: this may just be one step in the model retrying with
        # different fields/category, and every dead end posting its own
        # "nothing found" message is the noise reported live.
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
    # Include sort_value (the actual ranked number, e.g. an orn_bonus %)
    # directly in the observation text when a sort was requested - without
    # this the model could only see it in the posted message's button
    # labels, which aren't part of its own context, forcing an unnecessary
    # open_entry just to re-read a number it already had (live-verified
    # waste: burned 2 extra loop steps doing exactly that on a multi-slot
    # bonus-calculation request).
    def _fmt(e):
        if sort_by and e.get("sort_value") is not None:
            return f"{e['name']} [{sort_by}={e['sort_value']}] ({e['url']})"
        return f"{e['name']} ({e['url']})"
    names = "; ".join(_fmt(e) for e in entries[:5])
    return f"{len(matches)} matches for {summary}: {names}"


_CALENDAR_LINK_HTML = f'<a href="{CALENDAR_URL_UK}">📅 Переглянути календар подій</a>'


async def _run_events_tool(message, keyword: str) -> str:
    try:
        # upcoming_only=True (fetch_events' default) already drops anything
        # whose own end date has passed - deterministic, done in
        # orna_calendar.py, not left to the model's own date reasoning
        # (live bug: it showed two already-ended events, then separately
        # claimed no such event existed - a confusing, self-contradicting
        # answer from doing date-judgment and bonus-wording-judgment in
        # one fuzzy step).
        events = await asyncio.to_thread(fetch_events)
    except Exception as e:
        logger.warning("orna: events fetch failed", exc_info=True)
        return f"events fetch failed: {e}"

    needle = keyword.strip().lower()
    shown = [e for e in events if needle in e["name"].lower() or needle in e["description"].lower()] if needle else events

    if not shown:
        # Honest "no match" instead of silently substituting a different
        # (possibly irrelevant, possibly already-ended) list - that
        # substitution is what produced the live confusing answer. The
        # calendar link is always the fallback the user can check directly,
        # not the model's synthesis of it.
        await message.reply_text(
            f"Наразі немає активних чи найближчих подій за цим запитом.\n{_CALENDAR_LINK_HTML}",
            parse_mode="HTML", disable_web_page_preview=True,
        )
        return "no current/upcoming event matches; told the user nothing matches and linked the calendar"

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
            rows = _pack_buttons(_section_buttons(sections, key))
        await message.reply_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(rows) if rows else None)
        summaries.append(f"{e['name']} ({e['starts']} to {e['ends']}, live={e['live']}): {e['description']}")
    await message.reply_text(_CALENDAR_LINK_HTML, parse_mode="HTML", disable_web_page_preview=True)
    return "\n".join(summaries)


async def _run_towers_tool(message) -> str:
    """Current floor of all 5 "Wild Towers of Olympia" - pure
    deterministic math (orna_towers.py, ported line-for-line from
    OrnaCodex's own tower.ts and cross-checked against the original TS
    run under Node before deploying - see orna_towers._demo), not looked
    up from any data source at all. No args needed - cheap enough to
    always report all 5 and let the model read whichever one the request
    actually asked about."""
    now = datetime.datetime.now(datetime.timezone.utc)
    floors = orna_towers.get_tower_floors(now)

    NAME_W = 11
    table_rows = ["Вежа".ljust(NAME_W) + "Поверх"]
    for tf in floors:
        label = "МАКС" if tf.floor >= 50 else str(tf.floor)
        table_rows.append(tf.kind.capitalize().ljust(NAME_W) + label)
    table_text = "\n".join(table_rows)
    lines = ["🗼 <b>Вежі Олімпії зараз:</b>", f"<pre>{html.escape(table_text)}</pre>"]

    upcoming = orna_towers.get_tower_floors_in_next_days(now, 1)
    if upcoming:
        nxt = upcoming[0]
        delta_min = int((nxt["time"] - now).total_seconds() // 60)
        lines.append(f"Наступна зміна поверхів: {nxt['time'].strftime('%Y-%m-%d %H:%M')} UTC (за {delta_min} хв)")

    await message.reply_text("\n".join(lines), parse_mode="HTML")
    summary = "; ".join(f"{tf.kind}={tf.floor}" for tf in floors)
    return f"posted current tower floors (out of 50, 50=cleared/at the top): {summary}"


_SPRITE_TARGET_PX = 200


def _upscale_sprite_sync(raw: bytes, target: int = _SPRITE_TARGET_PX) -> bytes:
    """playorna's own sprite images are tiny pixel-art icons (16-24px -
    the same size the in-game inventory slot icon uses) - even the
    site's own "entry-icon" detail-page class, which LOOKS bigger,
    renders that exact same tiny source stretched via plain HTML
    width/height (verified directly against playorna's production JS
    bundle: `<img src="detail.sprite" width="128">` - no separate
    higher-resolution image exists anywhere on their site for this).
    Handing Telegram a URL lets ITS OWN scaler blur a 16px source across
    a much larger bubble (confirmed visually - a live screenshot showed
    a soft, blurry icon). Fetching it ourselves and upscaling by an
    INTEGER factor with nearest-neighbor (no interpolation) instead
    keeps every source pixel a crisp, distinct square - the correct way
    to enlarge small pixel art, not smooth-blur it into mush."""
    img = Image.open(io.BytesIO(raw)).convert("RGBA")
    factor = max(1, target // max(img.size))
    if factor > 1:
        img = img.resize((img.width * factor, img.height * factor), Image.Resampling.NEAREST)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


async def _fetch_upscaled_sprite(url: str):
    """Returns upscaled PNG bytes for Telegram to send directly, or None
    on any failure (caller falls back to the raw URL - a slightly blurry
    icon beats no icon at all)."""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url)
            resp.raise_for_status()
        return await asyncio.to_thread(_upscale_sprite_sync, resp.content)
    except Exception:
        logger.warning("orna: sprite upscale failed for %s", url, exc_info=True)
        return None


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
            upscaled = await _fetch_upscaled_sprite(sprite)
            await message.reply_photo(upscaled if upscaled else sprite)
        except TelegramError:
            logger.warning("orna: failed to send entry sprite %s", sprite, exc_info=True)

    sections = detail.get("sections") or []
    key = _remember({"sections": sections, "lang": lang})
    buttons = _section_buttons(sections, key)

    # playorna urls are always "/codex/<category>/<id>/" - reuse that to
    # link an "Aussie Codex" button to aussiescodex.com's calculator for the
    # same record, when it has a page (only 4 of 9 categories do).
    parts = [p for p in url.split("/") if p]
    if len(parts) >= 3 and parts[0] == "codex" and has_aussies_page(parts[1]):
        buttons.append(InlineKeyboardButton("📊 Aussie Codex", url=build_aussies_url(parts[1], parts[2])))

    rows = _pack_buttons(buttons)

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
    # Cross-link sections (e.g. an item's "Dropped by" monsters, a
    # monster's "Skills") are the site's own site-wide link graph - surface
    # their entry names here too, not just as buttons, so a question like
    # "which monster drops X" is answerable from this ONE call instead of
    # the model flailing with description-text searches (live bug: it
    # tried query()'ing monsters' descriptions for the item's name instead
    # of just reading the item's own "Dropped by" section).
    for section in detail.get("sections") or []:
        entries = section.get("entries") or []
        if not entries:
            continue
        names = ", ".join(e.get("name", "?") for e in entries[:10])
        if len(entries) > 10:
            names += f" (+{len(entries) - 10} more)"
        digest += f" | {section.get('title', '?')}: {names}"
    return digest


_QUALITY_NAME_TO_PERCENT = {
    "broken": 50, "poor": 90, "regular": 100, "normal": 100,
    "superior": 101, "famed": 120, "legendary": 140, "ornate": 171,
}
_FORGED_LEVELS = {"masterforged": 11, "demonforged": 12, "godforged": 13}


def _parse_quality_spec(spec: str) -> Optional[tuple]:
    """<quality%, level> from a free-text quality spec - a percentage
    ("185", "185%") or a named tier. Masterforged/Demonforged/Godforged
    are really upgrade LEVELS 11/12/13 in Orna's own mechanics (past
    level 10, orna_assess.get_quality_code derives the quality bucket from
    LEVEL, not quality% - see that function), not a quality percentage, so
    those map to a level instead; quality defaults to 100% for them (a
    forged item assumed pushed to the tier's floor, not some arbitrary
    higher %). The 7 percentage-tier names (Broken..Ornate) use that
    tier's own LOWER bound as a representative % (see get_quality_code's
    own thresholds) since there's no single canonical "the" percentage for
    a bare name - the caller notes this assumption in the reply rather
    than leaving it silent. None if the spec isn't parseable at all."""
    text = spec.strip().lower().rstrip("%").strip()
    if text in _FORGED_LEVELS:
        return 100, _FORGED_LEVELS[text]
    if text in _QUALITY_NAME_TO_PERCENT:
        return _QUALITY_NAME_TO_PERCENT[text], 1
    try:
        return int(round(float(text))), 1
    except ValueError:
        return None


def _aussies_record_to_codex_entry(record: dict) -> CodexEntry:
    """Build a CodexEntry (orna_assess's input shape) directly from an
    aussiescodex.com codex.json record, rather than from
    orna_codex.lookup_by_name's playorna-HTML-scraped one. Live bug this
    fixes: playorna renders some stats (e.g. an item's own Orn Bonus) as a
    free-text "effect" bullet rather than a page "fact" dt/dd pair, so
    orna_codex.py's scraper structurally can't capture them (verified:
    Dark Mage Hood's Orn Bonus never reaches its scraped CodexEntry.stats
    at all) - aussies' stats dict has every stat, bonus stats included, as
    one flat, reliable structure, the same one query()/knowledge_search
    already trust all session.

    Flags are derived the same way orna_codex.parse_codex_html derives
    them, just from aussies' own clean enum-like place/item_type/rarity
    fields instead of regex-matching scraped page text - more reliable
    for everything except is_two_handed, which aussies doesn't expose as
    a flat field at all.
    ponytail: is_two_handed always False here - only affects a celestial
    TWO-HANDED weapon's adornment-slot count (a narrow case), not any
    stat projection. Add a real check (e.g. via fetch_codex_json's page
    facts) if that specific gap is ever reported.
    """
    stats: Dict[str, float] = {}
    for key, raw in (record.get("stats") or {}).items():
        v = _aussies_parse_number(raw)
        if v is not None:
            stats[key] = int(v) if key == "adornment_slots" else v

    place = (record.get("place") or "").lower()
    item_type = (record.get("item_type") or "").lower()
    rarity = (record.get("rarity") or "").lower()
    is_adornment = "adornment" in item_type or "adornment" in place
    is_accessory = place == "accessory"
    is_weapon_like = place in ("weapon", "off-hand")
    is_celestial_weapon = rarity == "celestial" and is_weapon_like
    # Only real equippable gear slots are upgradable. "material" (and
    # "augment_(...)") are valid `place` values that are NOT gear - the old
    # `bool(place) and not is_adornment` let a crafting material (place=
    # "material", e.g. elstone) through as is_upgradable=True, so
    # get_assess_result returned levels=13 and assess/compare posted a
    # nonsense "upgrades to lv 13" table with an all-zero adornment row
    # instead of hitting _run_assess_tool's "not assessable" (levels==0) guard.
    is_equippable = place in ("head", "torso", "legs", "weapon", "off-hand", "accessory") and not is_adornment
    is_upgradable = is_equippable and not is_accessory
    has_scaling_slots = is_upgradable and "adornment_slots" in stats
    boss_scaling = -1 if is_celestial_weapon else (1 if is_upgradable else 0)

    return CodexEntry(
        name=display_name(record["category"], record["id"]),
        stats=stats,
        is_adornment=is_adornment,
        is_accessory=is_accessory,
        is_celestial_weapon=is_celestial_weapon,
        is_two_handed=False,
        is_upgradable=is_upgradable,
        has_scaling_slots=has_scaling_slots,
        boss_scaling=boss_scaling,
    )


async def _resolve_aussies_entry(item_name: str):
    """Resolve a free-text item name to (CodexEntry, playorna source url)
    - the exact codex_search-for-the-name + aussiescodex-for-the-stats
    lookup _run_assess_tool needed, factored out so compare()/
    build_optimize() can reuse it instead of a third copy. On any failure
    returns (None, <error text>) instead of raising, so a caller can
    return that text directly as its tool observation."""
    try:
        data = await asyncio.to_thread(codex_search, item_name, "en")
    except Exception as e:
        logger.warning("orna: entry lookup failed for %r", item_name, exc_info=True)
        return None, f"lookup failed for {item_name!r}: {e}"
    results = data.get("results") or []
    if not results:
        return None, f"no codex entry found for {item_name!r} - try search_codex first to confirm the exact name"
    url = results[0].get("url", "")
    parts = [p for p in url.split("/") if p]
    if len(parts) < 3 or parts[0] != "codex":
        return None, f"couldn't resolve a codex id for {item_name!r}"
    category, record_id = parts[1], parts[2]
    # _aussies_codex() can trigger a synchronous network fetch on a cache
    # miss (see orna_aussies._fetch_json) - asyncio.to_thread keeps that
    # off the event loop, same as every other tool's data access in this
    # file; a raw blocking call here would stall the ENTIRE bot for every
    # chat, not just this request (confirmed live incident, see
    # concurrent_updates' own docstring in telegram_bot.py for why that
    # alone isn't sufficient protection against a truly blocking call).
    codex = await asyncio.to_thread(_aussies_codex)
    record = codex["main"].get(category, {}).get(record_id)
    if record is None:
        return None, f"{results[0].get('name', item_name)!r} has no aussiescodex data (category {category!r})"
    entry = _aussies_record_to_codex_entry(record)
    source_url = f"https://playorna.com{url}" if url.startswith("/") else url
    return entry, source_url


async def _run_assess_tool(message, item_name: str, quality_spec: str) -> str:
    """Same projection pipeline the screenshot-upload /assess flow uses
    (orna_assess.get_assess_result) rendered the same way
    (telegram_assess._format_response) - the difference is twofold: where
    quality comes from (OCR'd observed stats there, an explicit name/%
    given in the request here), and where STATS come from (aussies'
    codex.json here - see _aussies_record_to_codex_entry - rather than
    orna_codex.py's playorna-HTML scrape, which structurally misses some
    bonus stats). Live ask: "/orna assess arisen aaru robe Legendary" or
    "...185%" should return the same kind of stat table a screenshot
    upload does. Name resolution still goes through codex_search
    (playorna's own ranked search, already proven reliable everywhere
    else this session) since aussies' own name matching is a plain,
    ambiguity-prone substring."""
    if not item_name or not quality_spec:
        return "assess needs both an item name and a quality (name or %) in args"
    parsed = _parse_quality_spec(quality_spec)
    if parsed is None:
        return (f"couldn't parse quality {quality_spec!r} - use a percentage (e.g. \"185\") or a quality name "
                "(broken/poor/regular/superior/famed/legendary/ornate/masterforged/demonforged/godforged)")
    quality, level = parsed

    entry, source_or_error = await _resolve_aussies_entry(item_name)
    if entry is None:
        return source_or_error
    source_url = source_or_error

    inp = AssessInput(entry=entry, level=level, boss_scaling=entry.boss_scaling, quality=quality, stats={})
    result = get_assess_result(inp, is_quality_calc=True)
    if result is None or result.levels == 0:
        return f"{entry.name} isn't assessable (likely a non-scaling material or useable, not upgradable gear)"

    reply = _format_response(entry, result, source_url, {}, level)

    # get_assess_result always derives the shown "(Quality Name)" label
    # via get_quality_code(quality, 1) - level hardcoded to 1 regardless
    # of inp.level (existing behavior in the ported assess module, not
    # something to change here) - so a forged-tier ask (level 11-13) always
    # displays as "(Regular)"/etc instead of "(Masterforged)"/"(Godforged)"
    # even though the projection itself is correct. Make what was actually
    # requested unambiguous rather than relying on a label known to be
    # misleading for exactly this case.
    requested = quality_spec.strip().lower().rstrip("%")
    if requested in _QUALITY_NAME_TO_PERCENT or requested in _FORGED_LEVELS:
        reply = reply.replace("Quality:", f"Запитана якість: {quality_spec.strip().title()}\nQuality:", 1)

    # Bonus-type stats (orn/exp/gold/luck bonus, ...) aren't part of the
    # core upgrade table _format_response renders (that's the 10 combat
    # stats only) but scale with quality too, via the same official
    # formula - append them if the item actually has any, since that's
    # exactly the number a "best build" bonus calculation needs.
    quality_code = get_quality_code(quality, level)
    bonus_lines = []
    for key in sorted(QUALITY_CODE_BONUS_KEYS & entry.stats.keys()):
        base = entry.stats.get(key)
        if not isinstance(base, (int, float)) or isinstance(base, bool):
            continue
        scaled = get_quality_bonus(base, quality, quality_code, entry.is_adornment, key)
        bonus_lines.append(f"{key.replace('_', ' ')}: {base:g}% base → {scaled:g}% at this quality")
    if bonus_lines:
        reply += "\n\n<b>Бонус-статистики (поза основною таблицею):</b>\n" + "\n".join(f"• {l}" for l in bonus_lines)

    await message.reply_text(reply, parse_mode="HTML", disable_web_page_preview=True)
    return f"posted assessment for {entry.name} at quality={quality}% level={level}"


async def _run_compare_tool(message, item_names: list, quality_spec: str) -> str:
    """Compare 2+ items' FULLY-UPGRADED, quality-scaled stats side by
    side, diffed against the first item - not raw base stats (barely
    comparable pre-upgrade for gear). Same "assess at max quality/level,
    diff against the first entry" design OrnaCodex's own Compare feature
    uses (src/stores/compare.ts, found surveying that repo for ideas
    worth adopting) - reuses _resolve_aussies_entry + orna_assess.
    get_assess_result, the exact pipeline assess() already uses, just run
    once per item instead of once. Defaults to quality 200%/level 13
    (OrnaCodex's own compare default: effectively "fully forged") since a
    comparison is normally about a build's ceiling, not one specific
    quality - pass quality_spec to compare at a specific one instead."""
    # A model sometimes emits a bare string instead of a JSON array; without
    # this, `for n in "Ring"` iterates characters ('R','i',...) and compares
    # nonsense. Wrap a lone string into a one-item list (which then trips the
    # "need at least 2" guard cleanly).
    if isinstance(item_names, str):
        item_names = [item_names]
    names = [str(n).strip() for n in (item_names or []) if str(n).strip()][:6]
    if len(names) < 2:
        return "compare needs at least 2 item names in args"

    quality, level = 200, 13
    if quality_spec:
        parsed = _parse_quality_spec(quality_spec)
        if parsed is None:
            return f"couldn't parse quality {quality_spec!r} - use a percentage or a quality name"
        quality, level = parsed

    rows = []
    for name in names:
        entry, source_or_error = await _resolve_aussies_entry(name)
        if entry is None:
            return source_or_error
        item_level = level if entry.is_upgradable else 1
        inp = AssessInput(entry=entry, level=item_level, boss_scaling=entry.boss_scaling, quality=quality, stats={})
        result = get_assess_result(inp, is_quality_calc=True)
        if result is not None and result.levels > 0:
            stats = {k: row.values[-1] for k, row in result.stats.items() if row.values}
        else:
            # Not an upgradable/scaling item (a material, a flat-stat
            # accessory, ...) - still worth comparing on its raw stats
            # rather than showing an empty row.
            stats = dict(entry.stats)
        rows.append((entry.name, stats))

    all_keys: list = []
    for _, stats in rows:
        for k in stats:
            if k not in all_keys:
                all_keys.append(k)
    if not all_keys:
        return "none of these items have any comparable stats"

    base_name, base_stats = rows[0]
    lines = [f"⚖️ <b>Порівняння</b> (якість {quality}%, рівень {level}):"]
    for i, (name, _) in enumerate(rows):
        lines.append(f"{i + 1}. {html.escape(name)}")

    # Fixed-width columns in a <pre> block (same convention telegram_assess.
    # _format_response uses for its own stat table) instead of plain
    # comma/pipe-joined text - full item names go above as a numbered list
    # since they vary too much in length to fit a narrow column without
    # cramped truncation; the table itself just references them by number.
    NAME_W, VAL_W = 14, 13
    table_rows = [["Stat".ljust(NAME_W)] + [f"#{i + 1}".rjust(VAL_W) for i in range(len(rows))]]
    for key in all_keys:
        base_val = base_stats.get(key, 0.0)
        cells = [key.replace("_", " ")[:NAME_W].ljust(NAME_W), f"{base_val:g}".rjust(VAL_W)]
        for _, stats in rows[1:]:
            v = stats.get(key, 0.0)
            diff = v - base_val
            sign = "+" if diff >= 0 else ""
            cells.append(f"{v:g}({sign}{diff:g})".rjust(VAL_W))
        table_rows.append(cells)
    # Space-joined even though each cell is already padded to its column
    # width (same belt-and-suspenders convention telegram_assess._format_
    # response uses) - a value+diff string that runs slightly over VAL_W
    # (a big stat, a big diff) still gets a visible gap instead of
    # butting straight into the next column with no separation at all.
    table_text = "\n".join(" ".join(c) for c in table_rows)
    lines.append(f"<pre>{html.escape(table_text)}</pre>")

    await message.reply_text("\n".join(lines), parse_mode="HTML")
    names_summary = ", ".join(n for n, _ in rows)
    return f"posted comparison of {names_summary} at quality={quality}% level={level}"


async def _run_build_optimize_tool(message, slots: list, stat: str, useable_by: str, quality_spec: str) -> str:
    """Native multi-slot optimizer for STACKING BONUS STATS (orn_bonus/
    exp_bonus/gold_bonus/luck_bonus/...) - deterministic Python instead
    of the model orchestrating several query()+calculate() calls itself
    the way _AGGREGATE_RULE used to require: that prompt-engineered
    recipe needed real hardening (a dedicated calculate() tool, an
    explicit worked example) just to get "max orn bonus across every
    slot" to work reliably, and still cost several loop steps every time.
    Reuses orna_aussies.query_records for the per-slot lookup and
    orna_assess.get_quality_bonus for the SAME official scaling formula
    _run_assess_tool's own bonus-stats section already uses - one
    implementation of that formula, not two. Only meaningful for
    QUALITY_CODE_BONUS_KEYS stats - a raw combat stat like attack/magic
    doesn't stack across slots the same way (query's own sort_by, or
    compare(), are the tools for that)."""
    stat = (stat or "").strip().lower().replace(" ", "_").replace("-", "_")
    if stat not in QUALITY_CODE_BONUS_KEYS:
        return (f"build_optimize is for stacking bonus stats only - one of {sorted(QUALITY_CODE_BONUS_KEYS)}, "
                f"got {stat!r}. For a raw combat stat, use query with sort_by instead.")

    # Wrap a lone string ("head") the way compare() does, so a non-array
    # arg isn't iterated character-by-character into an all-empty result.
    if isinstance(slots, str):
        slots = [slots]
    slot_list = [str(s).strip().lower() for s in (slots or []) if str(s).strip()] or \
        ["head", "weapon", "off-hand", "torso", "legs", "accessory", "accessory"]

    quality, level = 100, 1
    if quality_spec:
        parsed = _parse_quality_spec(quality_spec)
        if parsed is None:
            return f"couldn't parse quality {quality_spec!r} - use a percentage or a quality name"
        quality, level = parsed
    quality_code = get_quality_code(quality, level)

    try:
        codex = await asyncio.to_thread(_aussies_codex)
    except Exception as e:
        return f"build_optimize failed to load codex data: {e}"

    used_keys = set()
    rows = []  # (slot, name_or_None, base, scaled)
    for slot in slot_list:
        conditions = [{"kind": "attr", "field": "place", "cmp": "=", "value": slot}]
        if useable_by:
            conditions.append({"kind": "attr", "field": "useable_by", "cmp": "=", "value": useable_by})
        try:
            matches = await asyncio.to_thread(query_records, conditions, "and", None, 10, stat, "desc")
        except Exception as e:
            return f"build_optimize lookup failed for slot {slot!r}: {e}"
        chosen = next((m for m in matches if (m.category, m.id) not in used_keys), None)
        if chosen is None:
            rows.append((slot, None, 0.0, 0.0))
            continue
        used_keys.add((chosen.category, chosen.id))
        record = codex["main"].get(chosen.category, {}).get(chosen.id)
        entry = _aussies_record_to_codex_entry(record) if record else None
        base = (entry.stats.get(stat) if entry else None) or 0.0
        scaled = get_quality_bonus(base, quality, quality_code, entry.is_adornment if entry else False, stat)
        rows.append((slot, chosen.name, base, scaled))

    multiplier = 1.0
    SLOT_W, NAME_W, VAL_W = 10, 20, 8
    table_rows = [["Slot".ljust(SLOT_W), "Item".ljust(NAME_W), "База".rjust(VAL_W), "Якість".rjust(VAL_W)]]
    for slot, name, base, scaled in rows:
        if name is None:
            table_rows.append([slot.ljust(SLOT_W), "—".ljust(NAME_W), "-".rjust(VAL_W), "-".rjust(VAL_W)])
            continue
        multiplier *= (1 + scaled / 100)
        display = name if len(name) <= NAME_W else name[:NAME_W - 1] + "…"
        table_rows.append([slot.ljust(SLOT_W), display.ljust(NAME_W), f"{base:g}%".rjust(VAL_W), f"{scaled:.1f}%".rjust(VAL_W)])
    table_text = "\n".join(" ".join(c) for c in table_rows)
    total_pct = (multiplier - 1) * 100

    lines = [
        f"🏗 <b>Оптимізація {html.escape(stat)}</b> (якість {quality}%):",
        f"<pre>{html.escape(table_text)}</pre>",
        f"<b>Сумарний бонус (множення, не сума): {total_pct:.1f}%</b>",
    ]
    await message.reply_text("\n".join(lines), parse_mode="HTML")
    items_summary = "; ".join(f"{slot}={name}({scaled:.1f}%)" for slot, name, base, scaled in rows if name)
    return f"posted build_optimize result [total={total_pct:.1f}]: total {stat} bonus = {total_pct:.1f}%. Items: {items_summary}"


async def _run_calculate_tool(message, expression: str) -> str:
    """Reuses telegram_go._calculate directly (safe ast-based eval, no
    Python eval()) - same reasoning /go's own docstring already gives for
    having this tool at all: "use this instead of doing arithmetic
    yourself, you will get it wrong". /orna didn't have this until a live
    report showed exactly that failure - a multi-slot bonus-stacking
    question (multiply several per-slot % bonuses together) needs real
    multiplication across several numbers gathered over several turns,
    which is precisely the kind of compounding arithmetic a model is
    unreliable at doing in free-form "thought" text. Synchronous, no I/O."""
    if not expression:
        return "calculate needs a numeric expression in action_input"
    return _calculate(expression)


_GUIDE_EXCERPT_CHARS = 6000


async def _run_class_guide_tool(message, topic: str, query: str) -> str:
    """Long-form written community guides (strategy/build REASONING - why
    a setup works, tradeoffs between two builds) for a specific class or
    cross-class build - see orna_guides.py for the full topic list and
    why this is separate from knowledge_search's short-fact corpus. These
    guides run from ~10KB to ~180KB of prose - far too much to hand the
    model whole every time - so this returns a query-focused excerpt via
    orna_guides.guide_excerpt (see it for the header-/tab-aware ranking that
    keeps a "raid" query from landing on the wrong same-named build), or the
    guide's own opening when no query is given.
    No reply_text - like knowledge_search/web_search, this is raw source
    material for the model to read and write the real answer from in
    finish(), not already-formatted content to show verbatim."""
    available = ", ".join(k for k, _ in orna_guides.list_guides())
    if not topic:
        return f"class_guide needs a topic in args - one of: {available}"
    key = orna_guides.resolve_guide(topic)
    if key is None:
        return f"no guide found for {topic!r} - available topics: {available}"

    text = await asyncio.to_thread(orna_guides.read_guide, key)
    if not text:
        return f"guide for {key!r} is empty or missing on disk"

    return orna_guides.guide_excerpt(text, query, _GUIDE_EXCERPT_CHARS)


async def _run_knowledge_tool(message, query: str) -> str:
    """Curated community reference (orna_knowledge.txt, see
    orna_scrape_knowledge.py) for exactly the gap web_search exists for -
    most notably per-monster/boss elemental damage resistances/immunities,
    which the live codex doesn't track at all (verified: not even an empty
    field). Free, instant, no API call, and - being pre-vetted community
    data rather than an arbitrary web page - more trustworthy than a
    fresh web_search, so this is the one to try FIRST for that kind of
    question; web_search is the fallback when this doesn't have it either.
    Same no-reply_text pattern as web_search: the matched rows are raw
    semi-structured data (see orna_knowledge.py), not something to show
    the user verbatim - the model reads this observation and writes the
    real answer in finish()."""
    if not query:
        return "knowledge_search needs a query in action_input"
    # asyncio.to_thread: same reasoning as _run_assess_tool's aussies
    # lookup - _load()'s first call does a synchronous disk read (306KB),
    # and a fuzzy-correction miss runs difflib over a ~3500-word
    # vocabulary; individually fast, but any blocking call on the event
    # loop stalls every other chat's request too, not just this one.
    result = await asyncio.to_thread(orna_knowledge.search, query)
    if not result:
        return f"no knowledge-base matches for {query!r} - try web_search instead"
    return result[:3000]


async def _run_web_search_tool(message, query: str) -> str:
    """Last-resort tool: Orna's structured data (codex + aussiescodex)
    covers stats/facts/drops/effects, but not strategy - a boss's real
    immunities in practice, community-discovered counters, meta builds,
    that kind of thing genuinely isn't in either data source and never
    will be. Reuses telegram_go._tavily_search directly rather than a
    second Tavily client - same API key, same call shape, no reply_text
    here (unlike every other tool) since raw search results aren't
    trustworthy/structured enough to show the user verbatim the way a
    codex result is - the model reads this observation and writes the
    actual answer itself in finish(), same as /go's own "search" action."""
    if not query:
        return "web_search needs an English search query in action_input"
    if not TAVILY_API_KEY:
        return "web_search unavailable: no search API configured"
    data = await _tavily_search(query)
    return data["text"][:2000]


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
    "instead (a text condition on the name plus an attr condition), not search_codex. Translate the NAME itself, "
    "but drop casual/emphatic filler words that aren't really part of it - Ukrainian \"клятий\"/\"проклятий\" "
    "(\"damn/cursed X\") is usually just annoyed emphasis about a material being hard to get, not a real item "
    'modifier - search "Ortanite", not "Cursed Ortanite", unless a search for the plain name turns up nothing AND '
    "a modified variant genuinely exists. A dead-end search costs nothing here (failed attempts aren't shown to "
    "the user, only what you eventually finish with) - retry with a simpler form rather than giving up.\n"
    "- query(args={...}): search the full item/monster/boss/class/spell/building/dungeon/follower/raid database "
    "by attributes - see the condition rules below.\n"
    "- events(action_input=<keyword, or empty>): the current/near-term event calendar - ONLY for a scheduled, "
    "time-limited game EVENT (a double-orns weekend, an EXP event, a limited-time special raid/gauntlet event "
    "that appears and disappears on the calendar) - ALREADY filtered to what's live or upcoming (anything fully "
    "ended is excluded before you ever see it, and a link to the full calendar is always shown to the user "
    "alongside the results), so you don't need to reason about dates yourself - only about whether a "
    'description\'s wording matches what was asked (wording varies: "earn 25% more orns" and "double orns, gold, '
    'and experience" both mean "gives more orns"). If nothing shown matches, say so plainly - the user already '
    "has the calendar link, so don't guess or invent a match that isn't really there. Leave action_input empty "
    "to see everything currently listed. NOT for \"raids\" in general - a question about raid STRATEGY, raid "
    "ITEMS/gear, or a specific raid boss (\"items for heretic raids\", \"how do I beat raid X\") is a class_guide/"
    "knowledge_search/query/web_search question, never events - \"raids\" only means events() when the ask is "
    "clearly about a scheduled calendar occurrence (\"коли наступний рейд-івент\", \"is there a raid event right "
    "now\"), not raids as an ongoing PvE content type. Live-verified failure: \"suggest items for heretic raids\" "
    "was misread as an events() ask and answered \"no matching event\" instead of using class_guide/knowledge_"
    "search - the word \"raids\" alone is not enough, check what's actually being asked FOR.\n"
    "- open_entry(action_input=<url from a previous observation, e.g. \"/codex/items/foo/\">): fetch one specific "
    "entry's full detail - facts, effects, AND its cross-link sections (e.g. an item's \"Dropped by\" monsters, a "
    "class's \"Skills\") all come back in the digest. This is how to answer \"which monster/boss drops X\" or "
    '"where do I get X" - open_entry the ITEM\'s own page and read its "Dropped by"-style section from the '
    'digest, don\'t query()/search monsters for the item\'s name (that searches monster DESCRIPTIONS, not their '
    "drop tables, and won't find it). Also use this to confirm an exact stat/fact before answering; not needed "
    "just for browsing (query/search_codex already show a result list with buttons the user can open themselves).\n"
    "- calculate(action_input=<numeric expression, e.g. \"1.5 * 1.2 * 1.1\">): evaluates + - * / ** % and "
    "parentheses. Use this for ANY arithmetic beyond trivial single-step math - ESPECIALLY combining several "
    "numbers gathered across multiple earlier tool calls (e.g. multiplying several items' bonus percentages "
    "together). Never do multi-step or compounding math in your own \"thought\" text and just state the result - "
    "you will get it wrong. Write the actual numbers you found into the expression yourself.\n"
    "- assess(args={\"item\":\"<English item name>\",\"quality\":\"<quality name or %>\"}): projects an item's full "
    "upgrade-level stat table (attack/magic/defense/resistance/hp/mana/dexterity/crit/ward/foresight across its "
    "upgrade levels, plus any orn/exp/gold/luck bonus scaled the same official way) at a GIVEN quality - the exact "
    "same calculation the screenshot-upload assess flow uses, just started from a name+quality instead of OCR'd "
    "stats. Use this whenever the user names a SPECIFIC item and asks to assess/project/calculate its stats, e.g. "
    '"assess arisen aaru robe legendary" or "aaru robe at 185%". quality is either a percentage ("185", "185%") '
    "or a named tier (broken/poor/regular/superior/famed/legendary/ornate/masterforged/demonforged/godforged) - "
    "pass whichever form the user gave verbatim, don't convert it yourself. This POSTS the full table to the "
    "user directly (same as search_codex/query results) - finish() just needs a short closing line, the table IS "
    "the answer.\n"
    "- compare(args={\"items\":[\"<English item name>\", \"<English item name>\", ...],\"quality\":\"<optional, "
    "same forms as assess>\"}): side-by-side stat comparison of 2-6 items at their FULLY ASSESSED stats (default "
    "quality 200%/level 13 if not given - effectively \"fully forged\", since a comparison is normally about a "
    "build's ceiling), diffed against the first item in the list. Use this for \"which is better, X or Y\" - never "
    "open_entry both and compare by eye, the raw codex numbers aren't upgrade-projected and aren't a fair "
    "comparison. POSTS the table directly - finish() just needs a short closing line.\n"
    "- build_optimize(args={\"stat\":\"<a STACKING bonus stat - orn_bonus/exp_bonus/gold_bonus/luck_bonus/...>\","
    "\"slots\":[\"head\",\"weapon\",\"off-hand\",\"torso\",\"legs\",\"accessory\",\"accessory\"] (optional - this "
    "full 7-slot loadout, with accessory TWICE for Orna's 2 accessory slots, is the default if omitted),"
    "\"useable_by\":\"<optional class filter>\",\"quality\":\"<optional, same forms as assess>\"}): for \"best/max "
    "STAT across every slot\" questions - finds the best item per slot AND computes the correctly-stacked "
    "(multiplicative, quality-scaled) total in ONE call. This REPLACES manually calling query() once per slot "
    "plus calculate() to stack them yourself - always prefer build_optimize for this question shape, it's faster "
    "and can't arithmetic-drift the way doing it across several turns can. Only for STACKING BONUS stats (orn/exp/"
    "gold/luck bonus and similar %-bonus stats) - for a single raw combat stat like magic/attack, use query's "
    "sort_by instead (that's a \"pick the best one\" ask, not a \"stack across slots\" ask). POSTS the full "
    "breakdown - finish() just needs a short closing line.\n"
    "- towers(): no input. Current floor (15-50, 50=cleared/at the top awaiting reset) of all 5 real-time \"Wild "
    "Towers of Olympia\" (Selene/Eos/Oceanus/Themis/Prometheus) - pure deterministic math from the current time, "
    "always available, never a dead end. Use for \"how tall is tower X now\"/\"which tower is at max\" etc. For "
    "GEAR/REWARDS/mechanics ABOUT the towers (not their live height), use class_guide(topic=\"towers\") instead - "
    "these are two different things sharing a name. POSTS the result - finish() just needs a short closing line.\n"
    "- class_guide(args={\"topic\":\"<class or build name, e.g. summoner/thief/realmshifter/deity/gilgamesh/"
    "beowulf/swash/heretic/towers>\",\"query\":\"<optional specific sub-topic/keyword to focus the excerpt on>\"}): "
    "long-form WRITTEN COMMUNITY GUIDES (strategy reasoning - why a build works, gear priorities, playstyle "
    "tradeoffs) for a SPECIFIC class or cross-class build the user is clearly asking about - use whenever the "
    "request names one of these classes/builds AND wants strategy/gear/build advice, not just a stat lookup (a "
    "stat lookup is still query/search_codex/assess). Give a `query` whenever the ask has a specific angle - and "
    "INCLUDE the game-mode/section word the request names (raid/raids, dungeon, tower, early, endless, ...) "
    "ALONGSIDE the build/gear/stat word, e.g. query \"omniflask raid\", not just \"omniflask\": a class guide often "
    "splits the SAME build name across tabs by mode (an Early-T10 \"Omniflask Raiding\" and a Raids-tab \"Omniflask "
    "Weakness\" are different gear), so dropping the mode word can land on the wrong one. Without any query you only "
    "see the guide's own opening, which may not be the relevant part for a long guide. No reply_text - like "
    "knowledge_search/web_search, read this as source material and write the real answer in finish().\n"
    "- knowledge_search(action_input=<search term>): a curated community reference (player-maintained sheets) for "
    "exactly what Orna's own codex genuinely doesn't track: PER-MONSTER/BOSS ELEMENTAL DAMAGE RESISTANCES/"
    "IMMUNITIES most of all (the codex has NO immunity field for bosses at all - not even an empty one - even "
    "though it matters enormously for \"how do I beat/kill X\" questions; don't conclude \"no immunities, use "
    "standard attacks\" just because open_entry's facts are silent about it, that silence is a missing-data gap, "
    "not evidence). In the \"Monster Data\" section's elemental columns (Arcane/Dark/Dragon/Earth/Fire/Holy/"
    "Lightning/Physical/Water), the number is a damage MULTIPLIER, read exactly: 1 = normal damage, 0 = takes "
    "ZERO damage (full IMMUNITY, not \"neutral\" - never call a 0 neutral), above 1 = extra damage (a real "
    "weakness worth recommending), below 1 = reduced damage (a resistance). Also covers gear XP/orn/gold/luck "
    "boost percentages, combat mechanics notes (faction/party/defend/berserk/gauntlet bonuses), badges, titles, "
    "pets/bestial bonds, skills/spells, buildings, raid rewards, view distance, and leveling costs. Free and "
    "instant (no API call) and pre-vetted community data - try this BEFORE "
    "web_search for anything it might plausibly cover, especially a \"how do I beat/kill X\" question (always try "
    "it at least once for those, specifically looking for elemental resistances). Results are matched rows with "
    "their column header attached - read them like a small table. If nothing matches, fall back to web_search.\n"
    "- web_search(action_input=<English search query>): the open web (via Tavily) - LAST RESORT for what Orna's "
    "own data AND knowledge_search's community reference both genuinely don't cover: boss/monster STRATEGY "
    "(specific tactics, not just resistances - try knowledge_search first for those), community meta discussion, "
    "best builds/counters. Not for facts/stats/drops (always search_codex/query/open_entry - free, authoritative, "
    "try them FIRST). Write a focused English query (add \"orna rpg\" if the term alone is ambiguous outside the "
    "game). Read the results and write the actual answer yourself in finish() - don't dump the raw results, "
    "briefly mention a source if one was genuinely useful, and if nothing useful turns up, say so honestly rather "
    "than guessing. One follow-up web_search with a refined query is fine if the first didn't help; don't loop on "
    "it beyond that.\n"
    "- knowledge_search, web_search, AND class_guide - CRITICAL: only state a specific detail (a follower/spell/"
    "item name, an exact number, a named mechanic, a build/gear recommendation) if it's ACTUALLY present in what "
    "came back - never invent a plausible-sounding specific to make the answer feel more complete, and never fill "
    "a gap with confident-sounding general RPG knowledge that isn't specific to Orna. Live-verified failure: a "
    "class_guide-informed answer invented a generic \"element X beats element Y\" rock-paper-scissors chart and "
    "specific items/bosses that don't exist in Orna at all - Orna has NO such fixed elemental triangle (per-"
    "monster elemental resistance comes from knowledge_search's Monster Data multipliers, not a genre trope), and "
    "the actual class_guide excerpt that turned up (the Heretic guide's real \"Raids\" section: named builds like "
    "\"Omniflask Weakness\", real gear like \"Arisen Kaladanda\"/\"Celestial Staff\") was right there and got "
    "ignored in favor of invented content. If the results only support a general insight (e.g. \"immune to "
    "everything except arcane damage\"), give exactly that general insight and stop there rather than padding it "
    "with specifics you don't actually have.\n"
    "- ask(action_input=<question>, options=[2-4 short choices]): a clarifying question. The user can only TAP a "
    "button, never type free text - always give options. Only when a specific missing detail would materially "
    'change the results and there\'s no reasonable default (e.g. "good gear for my class" names no class, or a '
    'name/search matches several unrelated things and it genuinely matters which). Most requests do NOT need '
    "this. Never ask twice in the same conversation.\n"
    "- finish(action_input=<short closing text>): end the turn. Results a TOOL already showed the user (search "
    "hits, reports, event cards) don't need repeating - finish is just a short closing sentence (e.g. \"Ось "
    'варіанти для обох слотів."), or, for the two fixed-reply cases below, the exact fixed text. An answer built '
    "from knowledge_search or web_search is different: nothing was shown to the user yet, so finish() IS the "
    "answer - write it out properly, in the user's own language, from what came back. Don't call finish before "
    "you have enough information.\n"
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
    "an enemy / grants (a buff to the player, including a follower's temporary bond proc - \"T.\" in a status "
    "name means \"Temporary\", not \"Team\") / cures a NAMED status (e.g. "
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

_CLASS_GUIDE_RULE = (
    "MANDATORY RULE for CLASS/BUILD STRATEGY questions: when the request names a SPECIFIC class/build from "
    "class_guide's list (summoner, thief/realmshifter, deity, gilgamesh, beowulf, swash, heretic) and asks for "
    "BUILD ADVICE/STRATEGY/GEAR PRIORITIES/how to play it (not a plain stat/item lookup, that's still query/"
    "search_codex/assess) - e.g. \"дай пораду по білду для класу thief\", \"how do I play Beowulf\", \"best "
    "Summoner build\" - you MUST call class_guide(topic=<the class>) AT LEAST ONCE before finish, even if you "
    "already feel confident from general knowledge. Live-verified failure: skipping straight to a query()-based "
    "gear search plus your own general \"glass cannon, max attack\" knowledge produced a generic, possibly-"
    "outdated answer instead of using the actual curated guide - these guides are written specifically for this "
    "and kept current; general training knowledge about a live-patched mobile game is exactly the kind of thing "
    "that goes stale. Give a specific `query` argument (a gear slot, a stat, a playstyle word from the request) "
    "to focus the excerpt on the relevant part of a long guide - and keep the game-mode/section word the request "
    "names (raid/dungeon/tower/early/endless/...) IN that query, since the same build name recurs across tabs "
    "tuned per mode and dropping it fetches the wrong build's gear - then base finish() on what it actually says.\n"
    "LANGUAGE: the guides are written in English, but your finish() answer MUST be in the user's OWN language - an "
    "English build question gets an ENGLISH answer, a Ukrainian one a Ukrainian answer. Neither the English guide "
    "text you just read nor the Ukrainian example phrasing in this rule may flip your reply to Ukrainian.\n"
    "SECOND mandatory rule, once class_guide's excerpt already lists a build's own gear (Weapon:/Headpiece:/"
    "Armor:/Legwear:/Accessory:/... lines naming real items): finish() MUST be built from those exact names - do "
    "NOT ALSO run a generic stat-sorted query() (sort_by magic/ward/attack/...) \"just in case\" and let ITS "
    "results replace or blend with what the guide actually said. A stat query answers a DIFFERENT question "
    "(\"what's the single highest-X item across the whole codex\") - its results are NEVER a substitute for a "
    "specific build's own curated gear list, even when a guide entry also gives a vague fallback for one slot "
    "(\"best ward gear you have\") alongside a named item elsewhere - the named item is still the primary answer "
    "for ITS slot, the fallback only applies to slots that genuinely have no named item. Live-verified failure: "
    "class_guide correctly returned a build's real named gear, but finish() instead presented an UNRELATED "
    "generic stat search's results - none of the items in the final answer matched what the guide actually "
    "listed for that build. To show named items with their real stats and tappable buttons (rather than typing "
    "names in prose), call search_codex ONCE PER named item the guide gave you - never a generic stat query as a "
    "substitute for names you already have.\n"
)

_STRATEGY_RULE = (
    "MANDATORY RULE for any \"how do I beat/kill/defeat X\" or \"what's X weak to\" question about a specific "
    "boss/monster: you MUST call knowledge_search AT LEAST ONCE (and web_search too if that doesn't help) before "
    "finish - never finish such a question from codex facts (search_codex/open_entry/query) alone, even if you "
    "already opened the boss's codex page and it looked complete. The codex's own facts NEVER include elemental "
    "immunities for a boss (that field doesn't exist there at all) - a complete-looking codex page is exactly the "
    "situation this rule is for, not a reason to skip the extra step. Live-verified failure: skipping straight to "
    "finish with only codex facts produced \"no known weaknesses, just hit it hard\" for a boss that is actually "
    "immune to every element except one - confidently wrong instead of checking."
)

_AGGREGATE_RULE = (
    "MULTI-SLOT / BUILD-OPTIMIZATION QUESTIONS (e.g. \"what's the max orn bonus from wearing the best orn item in "
    "every slot\"): call build_optimize ONCE with the relevant stat (and quality/useable_by if given) - it does "
    "the per-slot lookup, quality scaling, and multiplicative stacking natively and posts the full breakdown "
    "itself. Do NOT do this by hand with several query() calls plus calculate() - that manual recipe used to be "
    "the only way and needed real hardening (it still exists as a fallback below for a case build_optimize can't "
    "cover, e.g. a non-standard slot combination), but build_optimize is faster and can't arithmetic-drift the "
    "way doing it across several turns can. After build_optimize posts its breakdown, finish() just needs a short "
    "closing line referencing the total it already computed - don't recompute or restate the numbers yourself.\n"
    "FALLBACK (only if build_optimize's fixed slot/stat shape genuinely doesn't fit the ask): the same idea done "
    "manually - one query() per slot with sort_by=<stat>, note each result's \"[sort_by=value]\" from the "
    "observation text (never open_entry just to re-read a number you already have), scale each with calculate() "
    "using scaled = ((100 + base) * (100 + scaling) - 10000) / 100 (scaling per quality tier: superior=+10, "
    "famed=+15, legendary=+20, ornate=+25, masterforged=+30, demonforged=+40, godforged=+50, regular/poor=+0), "
    "then calculate() the multiplicative stack: (1 + slot1%/100) * (1 + slot2%/100) * ... - never multiply more "
    "than two numbers in your own head, write the calculate() expression out."
)


_CYRILLIC_RE = re.compile(r"[Ѐ-ӿ]")


def _orna_system_prompt(user_text: str = "") -> str:
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M %A")
    actions = ('"today"|"next"|"need"|"search_codex"|"query"|"events"|"open_entry"|"calculate"|"assess"|'
               '"compare"|"build_optimize"|"towers"|"class_guide"|"knowledge_search"|"web_search"|"ask"|"finish"')
    # Deterministic per-request language lock. Prompt-only "reply in the
    # user's language" guidance kept losing, for build/class_guide answers, to
    # the prompt's Ukrainian examples plus the long ENGLISH guide excerpt the
    # model reads right before finishing (live bug: English build questions
    # answered in Ukrainian ~half the time even after that guidance). Detecting
    # the script the user actually wrote in (this guild writes English or
    # Ukrainian) and stating the required language up front, per request, is
    # far more reliable than making the model infer it.
    lang_lock = ""
    if user_text:
        req_lang = "Ukrainian" if _CYRILLIC_RE.search(user_text) else "English"
        lang_lock = (
            f"CRITICAL LANGUAGE LOCK: the user's current request is written in {req_lang}. Every ask/finish "
            f"reply you send for THIS request MUST be written in {req_lang} - never another language, no matter "
            f"what language the guide/codex text you read is in or what language the examples below happen to use.\n\n"
        )
    return (
        lang_lock +
        'You are a ReAct agent answering /orna requests about the mobile RPG "Orna" for a Telegram bot used by '
        f'its guild - requests come in English or Ukrainian. Current date/time: {now} (server local time) - use '
        'this for "today"/"next event"/other relative dates. LANGUAGE (important): reply text (ask/finish '
        "action_input) MUST be in the SAME language the user wrote in - an English question gets an ENGLISH "
        "answer, a Ukrainian question a Ukrainian one. Do NOT default to Ukrainian; the example reply texts "
        "shown later in this prompt are illustrative only and never set your answer's language (this most often "
        "bites build/class_guide answers, where an English guide excerpt plus a Ukrainian example has flipped "
        "replies to Ukrainian). Tool arguments (action_input for other tools, query conditions) are always "
        "in ENGLISH regardless of the request's language, since the underlying data is English.\n\n"
        f"You have these tools - each turn, pick exactly ONE:\n{_TOOLS_TEXT}\n"
        f"{_CONDITION_RULES}\n\n"
        f"{_MULTI_PART_EXAMPLE}\n\n"
        f"{_STRATEGY_RULE}\n\n"
        f"{_CLASS_GUIDE_RULE}\n\n"
        f"{_AGGREGATE_RULE}\n\n"
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
    usage_stats.record_tool_call(action)
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
        if action == "knowledge_search":
            return await _run_knowledge_tool(message, action_input)
        if action == "web_search":
            return await _run_web_search_tool(message, action_input)
        if action == "calculate":
            return await _run_calculate_tool(message, action_input)
        if action == "assess":
            return await _run_assess_tool(message, str(args.get("item") or ""), str(args.get("quality") or ""))
        if action == "compare":
            return await _run_compare_tool(message, args.get("items") or [], str(args.get("quality") or ""))
        if action == "build_optimize":
            return await _run_build_optimize_tool(
                message, args.get("slots") or [], str(args.get("stat") or ""),
                str(args.get("useable_by") or ""), str(args.get("quality") or ""),
            )
        if action == "towers":
            return await _run_towers_tool(message)
        if action == "class_guide":
            return await _run_class_guide_tool(message, str(args.get("topic") or ""), str(args.get("query") or ""))
    except Exception as e:
        logger.warning("orna: tool %r failed", action, exc_info=True)
        return f"{action} failed: {e}"
    return (f"unknown action {action!r}; valid actions are today, next, need, search_codex, query, events, "
            "open_entry, knowledge_search, web_search, calculate, assess, compare, build_optimize, towers, "
            "class_guide, ask, finish.")


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


async def _call_step_model(session: "OrnaSession", step_number: int):
    """One model call for one loop turn, with a single retry on failure.
    Live report: a long multi-tool-call request (several query/
    knowledge_search calls gathering numbers for a final calculation)
    died on ONE "Ollama returned non-JSON content: ''" - empty output,
    most likely the local model (this step had already passed CLOUD_STEPS)
    running out of its own generation budget mid-"thought" under a long
    accumulated tool-call history, not a systematic failure. Discarding
    every step of reasoning already done over one blip is a bad trade -
    retrying the identical call once before giving up on the whole
    request costs a few seconds and matches the retry-once convention
    telegram_nlp._local_chat_json already uses for the same reason."""
    for attempt in range(2):
        try:
            if step_number <= CLOUD_STEPS:
                # Normal path: try cloud, fall back to local mid-turn if the
                # cloud call itself fails (out of credits, network, ...).
                return await chat_json_with_fallback(
                    GO_MODEL, LOCAL_OLLAMA_HOST, LOCAL_OLLAMA_MODEL, session.messages, api_key=OLLAMA_API_KEY,
                    timeout=STEP_MODEL_TIMEOUT,
                )
            # Past CLOUD_STEPS: skip the cloud attempt entirely, local only.
            return await chat_json(LOCAL_OLLAMA_HOST, LOCAL_OLLAMA_MODEL, session.messages, timeout=STEP_MODEL_TIMEOUT)
        except (OllamaError, UnsupportedMultimodal) as e:
            if attempt == 0:
                # Light log here on purpose (no exc_info) - this is an
                # anticipated, handled retry, not a crash; the full
                # traceback is logged once, where it's actually useful, if
                # the retry below also fails and the caller gives up.
                logger.warning("orna: step %d model call failed (%s), retrying once", step_number, e)
                continue
            raise


async def _advance_inner(sid: str, message) -> None:
    session = _ORNA_SESSIONS.get(sid)
    if session is None:
        return

    while session.steps_left > 0:
        session.steps_left -= 1
        step_number = MAX_STEPS - session.steps_left
        try:
            step = await _call_step_model(session, step_number)
        except (OllamaError, UnsupportedMultimodal) as e:
            logger.warning("orna: model call failed twice, giving up", exc_info=True)
            await message.reply_text(f"Не вдалося обробити запит: {e}")
            return

        action = step.get("action")
        action_input = str(step.get("action_input") or "").strip()
        args = step.get("args") if isinstance(step.get("args"), dict) else {}

        if action == "finish" or not action:
            usage_stats.record_tool_call("finish")
            # finish() is the one place the model's own free-form prose
            # reaches the user (every other reply is a tool-built,
            # already-HTML message) - nothing in the prompt asks for
            # Markdown, but it writes it anyway often enough (**bold**,
            # headings, and - since class_guide started handing back
            # long-form guide excerpts - full pipe tables) that a plain
            # reply_text was showing that syntax completely literally.
            # Same fix /go already has for its own model-authored replies.
            await _reply_markdown(message, action_input or "Не вдалося сформувати відповідь.")
            return

        if action == "ask":
            usage_stats.record_tool_call("ask")
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
            await _reply_markdown(message, action_input or "Уточніть, будь ласка:", reply_markup=keyboard)
            return

        session.messages.append({"role": "assistant", "content": json.dumps(step)})
        observation = await _run_tool(message, action, action_input, args)
        session.messages.append({"role": "user", "content": f"Observation: {observation}"})

    usage_stats.record_tool_call("_step_budget_exhausted")
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
        {"role": "system", "content": _orna_system_prompt(text)},
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
        # Consume the pending ask right here (no await between the bounds
        # check above and this clear, so it's atomic on the event loop): a
        # duplicate callback delivery - Telegram redelivery, or a fast
        # double-tap racing the keyboard-removal edit below - then fails the
        # bounds check and no-ops. Without this, resuming the SAME shared,
        # unlocked OrnaSession twice (concurrent_updates makes the overlap
        # real) double-appends messages, double-spends steps, and posts
        # duplicate replies for the rest of the request.
        session.ask_options = []
        try:
            await query.edit_message_text(f"{query.message.text}\n\n→ {choice}", reply_markup=None)
        except TelegramError:
            pass  # e.g. keyboard already gone - harmless, the loop resume below still runs
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

    # playorna's codex is cached in-process via lru_cache with no TTL, so
    # without this it kept serving pre-patch stats/tiers (assess, proof
    # pricing) until a full restart - clear_cache() had no caller at all.
    # This maintenance command is the natural place to drop it too, so one
    # /update_codex refreshes BOTH data sources after a game patch.
    clear_codex_cache()

    lines = ["✅ Кодекс оновлено:"]
    for cat, count in stats["categories"].items():
        lines.append(f"  {cat}: {count}")
    lines.append(f"stats: {stats['stats_vocab']}, status: {stats['status_vocab']}")
    lines.append("playorna codex cache cleared")
    await message.reply_text("\n".join(lines))


def build_orna_handler() -> CommandHandler:
    return CommandHandler("orna", handle_orna)


def build_orna_callback_handler() -> CallbackQueryHandler:
    return CallbackQueryHandler(orna_callback, pattern=r"^orna\|")


def build_update_codex_handler() -> CommandHandler:
    return CommandHandler("update_codex", handle_update_codex)
