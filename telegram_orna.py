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

import ast
import asyncio
import httpx
import datetime
import html
import io
import json
import logging
import os
import re
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, Optional

from PIL import Image
from telegram import (
    InlineKeyboardButton, InlineKeyboardMarkup, InlineQueryResultArticle, InputTextMessageContent, Update,
)
from telegram.error import TelegramError
from telegram.ext import (
    CallbackQueryHandler, ChosenInlineResultHandler, CommandHandler, ContextTypes,
    InlineQueryHandler, MessageHandler, filters,
)

from ollama_client import OllamaError, UnsupportedMultimodal, chat_json, chat_json_with_fallback
from orna_aussies import build_url as build_aussies_url
from orna_aussies import decode as decode_effect_code
from orna_aussies import display_name
from orna_aussies import has_aussies_page
from orna_aussies import fuzzy_codex_name
from orna_aussies import count_records, query_records, refetch_now, resolve_codes as resolve_effect_codes
import orna_codex_db
import orna_monuments
from orna_aussies import unresolvable_condition_fields
from orna_aussies import build_supergraph, resolve_entity
from orna_aussies import class_abilities as orna_aussies_class_abilities
from orna_aussies import _codex as _aussies_codex
from orna_aussies import _parse_number as _aussies_parse_number
from orna_calendar import CALENDAR_URL_UK, fetch_events
import orna_bonuses
import orna_echo
import orna_pinecone
import orna_qa
import orna_classes
import orna_guides
import orna_knowledge
import orna_mechanics
import orna_reddit
import orna_releases
import orna_towers
from orna_assess import (
    AssessInput, CodexEntry, QUALITY_CODE_BONUS_KEYS, get_assess_result, get_quality_bonus, get_quality_code,
)
from orna_codex import clear_cache as clear_codex_cache, codex_search, fetch_codex_json
from telegram_assess import _format_response
from orna_sheets import GUILD_NAMES, fetch_sheet_data, get_today_month_day
from telegram_go import _Status  # shared ephemeral status line, see its docstring
from telegram_go import (
    GO_ALLOWED_USER_IDS, GO_MODEL, OLLAMA_API_KEY, TAVILY_API_KEY, _calculate, _reply_markdown, _tavily_search,
)
from telegram_nlp import OLLAMA_HOST as LOCAL_OLLAMA_HOST, OLLAMA_MODEL as LOCAL_OLLAMA_MODEL
from telegram_nlp import extract_quantities, extract_resources
from telegram_remind import schedule_reminder  # towers tool's "remind me at floor 50" buttons
from orna_material_names_uk import EN_TO_UK, ITEM_EN_TO_UK, UK_TO_EN
from telegram_resources import build_report, pre_table, send_report_blocks
import usage_stats

logger = logging.getLogger(__name__)

_RESULTS_PER_PAGE = 8
# 6 was enough before web_search existed (a multi-part query is usually
# 2-3 steps), but a "how do I beat X" strategy question now genuinely
# needs codex-miss + retry + open_entry + knowledge_search/web_search
# (+ maybe a refined retry) + finish. 16 gives real headroom for that
# chain plus a genuinely multi-part request on top of it.
MAX_STEPS = 35
# EVERY step tries Ollama Cloud first and falls back to local, per explicit
# ask 2026-09-24. This replaced a context-weight scheme (CLOUD_CONTEXT_CHARS,
# now gone) that kept the "cheap" routing/lookup steps on the free local model
# and spent cloud only on the heavy read-and-synthesize ones. The reason that
# was abandoned: the cheap steps are not actually cheap to get WRONG - the
# local model picking the wrong tool, or dropping a number, costs a step out
# of MAX_STEPS and (since /orna started timing out rather than running out of
# steps) real wall-clock, which is now the scarcer resource. MAX_CLOUD_CALLS
# stays only as a runaway guard, high enough not to bind on a normal request
# - MAX_STEPS is 16, so 20 covers every step plus the close-out call. Set it
# to 0 to force local-only (that is what the verification harness's
# FORCE_LOCAL does).
MAX_CLOUD_CALLS = 20
# /orna's own cloud model, defaulting to /go's so nothing changes unless it
# is set. Split out 2026-09-24 so /orna can run a bigger pure-reasoning model
# (nemotron-3-super, 120B, tools+thinking) while /go keeps a VISION one:
# /go's "Continue" button attaches photos, nemotron has no vision, and a
# single shared setting would silently degrade that to text-only (Ollama
# 400s, ollama_client drops the image and retries - it doesn't crash, it just
# stops seeing pictures). /orna's loop is text-only, so it loses nothing.
ORNA_CLOUD_MODEL = os.environ.get("ORNA_CLOUD_MODEL", GO_MODEL)
# The loop's action names, in ONE place - both the system prompt's action
# enum and the `tools` array below are built from this.
_ACTIONS = ("today", "next", "need", "search_codex", "query", "sql", "events", "open_entry", "research",
            "calculate", "assess", "compare", "build_optimize", "estimate_stats", "towers", "class_guide",
            "knowledge_search", "monuments", "releases", "web_search", "ask", "finish")
# Declared to Ollama on every step call - NOT because the loop wants native
# tool calling (it reads its action out of either channel, see
# ollama_client._from_tool_calls), but because NOT declaring them made the
# LOCAL model 500. gpt-oss:20b is a harmony-format model and emits a native
# tool call for roughly half of these "pick one named action" prompts; with
# no tools declared, Ollama's harmony parser has no name to map back, logs
# "no reverse mapping found for function name", and fails the whole request
# with a bare 500 on about a third of them (live 2026-09-24: 91 such
# warnings -> 27 500s in one day's server log). That 500 carries no body, so
# nothing downstream can translate it, and the retry hits the same wall - a
# request that has already spent a dozen steps dies outright. Declaring the
# names gives the parser its mapping: 0 warnings, 0 500s over the same
# probe, with the tool-call replies arriving in the shape _from_tool_calls
# already handles. The parameter schema mirrors the JSON object the prompt
# asks for, so a native call's arguments land under the keys the loop reads
# rather than whatever the model invents (an unschema'd call produced
# {"name": "godforged lost helmet"} where the loop wanted "action_input").
_STEP_TOOLS = [{"type": "function", "function": {
    "name": name,
    "description": f"The /orna ReAct action {name!r}.",
    "parameters": {"type": "object", "properties": {
        "thought": {"type": "string"},
        "action_input": {"type": "string"},
        "args": {"type": "object"},
        "options": {"type": "array", "items": {"type": "string"}},
        # finish() only: 0-100, how sure the answer is. Gated at
        # _CONFIDENCE_FLOOR and clamped by _evidence_ceiling.
        "confidence": {"type": "integer"},
    }},
}} for name in _ACTIONS]
# Shorter than ollama_client.DEFAULT_TIMEOUT's 90s read timeout (which
# /go still uses, unchanged, via its own default call) - /orna's loop has
# a hard step budget where a slow/hanging call is pure waste (it can
# always fall back to local, or retry, or just move on), unlike /go's
# single-shot-per-turn use where waiting out a genuinely-slow-but-working
# cloud response is more worth it. Live incident: one step spent ~2.5
# minutes (a full 90s cloud timeout, then a slow local response) before
# giving up - this halves the worst case per attempt.
STEP_MODEL_TIMEOUT = httpx.Timeout(connect=10.0, read=45.0, write=20.0, pool=10.0)
# The LOCAL leg gets much longer than the cloud one. Different jobs: the cloud
# call should give up fast so the fallback happens quickly, but local IS the
# fallback - there is nothing after it, so cutting it off mid-generation just
# throws the step away. gpt-oss:20b runs ~49 tok/s here and a long accumulated
# tool history can take well over 45s to answer.
LOCAL_MODEL_TIMEOUT = httpx.Timeout(connect=10.0, read=120.0, write=20.0, pool=10.0)
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
LOOP_TIMEOUT_SECONDS = 600
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
# sid -> [(label, url)] for the finish() "Джерела" button. Kept OUT of
# _ORNA_SESSIONS because that is pruned on SESSION_TTL_SECONDS (15 min) while
# a posted answer stays in the chat forever - tapping the button an hour later
# should still work. Same short-id-in-callback_data pattern as _STATE.
# Follow-ups and typed answers to an ask() go through `/clarify <text>`, never
# bare free text. Design ask 2026-09-28: the bot lives in a crowded guild chat,
# and the old "listen to this user's next message for 180s" window kept
# catching chatter that was not meant for it. An explicit command costs the
# user six characters and removes the guessing - and the pending-filter
# machinery that guessing needed.
# One conversation per user: user_id -> sid. A new /orna replaces it; it dies
# SESSION_TTL_SECONDS after the bot's last reply in it (`created` is refreshed
# on every reply, so it measures idleness, not age).
_USER_SESSIONS: dict = {}
# Steps handed to a session resumed by a typed answer - enough to run the
# tool the answer unblocks and finish, without restarting the whole budget.
_RESUME_STEPS = 8

# PLAN-WORK-REVIEW-RELEASE (design ask 2026-10-02): the per-step "REASON
# TWICE" rule already asks the model to plan before its first tool call and
# re-check before finish - but that is advisory text squeezed into the SAME
# JSON call that is also picking an action, and it loses to other
# instructions in the very same prompt (live: the CONTEXT FORMAT rule said
# "a number you rely on must be restated in finish()", which directly
# contradicted "don't repeat stats a posted card already shows" - fixed
# alongside this, but the underlying lesson is that a reasoning instruction
# sharing a call with the thing it is supposed to check is a weak lever).
# PLAN and REVIEW are now also genuinely SEPARATE model calls - one before
# any tool runs, one before the draft answer is allowed to post - with
# nothing else to do but that one task. Both cost latency, so both are
# scoped to COMPLEX requests only (_looks_complex_request): a short, single-
# item lookup resolves in 1-2 tool calls where an upfront plan or a second
# opinion adds a real delay for essentially no benefit, and the per-step
# in-line reasoning already covers it well enough (see _demo for the actual
# measured effect). Bounded to one redo round so REVIEW cannot turn into a
# second open-ended loop - same "always replies, never hangs" guarantee
# _close_out/MAX_ASKS_PER_REQUEST already give the rest of this file.
MAX_REVIEW_ROUNDS = 1
# A bare "/clarify thanks"/"ok"/"дякую" is a CLOSER, not a
# new question. Live 2026-09-27: a user said "thank you" (no /orna) after an
# answer; the follow-up wait resumed the loop, which RAN TOOLS again and
# re-stated the same answer - the prompt's "do NOT re-state" lost. So this is
# caught deterministically before the loop is ever resumed (tool guard, not a
# prompt rule). Matched only when the WHOLE message is closer/filler tokens, so
# "thanks, and when does the next one start" is still a real follow-up.
_CLOSER_TOKENS = {
    "thanks", "thank", "thankyou", "thx", "ty", "tysm", "tnx", "u", "you", "cheers",
    "ok", "okay", "k", "kk", "cool", "nice", "great", "perfect", "awesome", "got",
    "it", "gotit", "very", "much", "so", "a", "lot", "man", "mate", "bro", "dude",
    "appreciate", "appreciated", "yw",
    "дякую", "дяки", "дякс", "дяк", "спасибі", "спасиб", "дуже", "ок", "окей",
    "круто", "супер", "зрозумів", "зрозуміла", "зрозуміло", "класно", "чудово",
}


def _is_pleasantry(text: str) -> bool:
    """True if the message is only closer/filler words or has no words at all
    (pure emoji/punctuation) - a "thanks"/"ok"/"дякую 🙏" that must not resume
    the loop. False the moment a substantive word appears ("thanks, and when...")."""
    tokens = re.sub(r"[^\w\s]", " ", (text or "").lower()).split()
    return not tokens or all(w in _CLOSER_TOKENS for w in tokens)


# Live 2026-09-24: the loop asked three times in a row. The user tapped
# "I'll provide details", then "Specialization/Class" - options that name
# WHAT to supply rather than answering anything - so each tap resumed the
# loop with no new information and it simply asked again. The prompt's "don't
# ask more than once" did not hold, so it is enforced here.
MAX_ASKS_PER_REQUEST = 2
# "Інше", "Other", "свій варіант", ... - a model-authored option that really
# means "none of these". Matched loosely because the model writes it freely.
_OTHER_OPTION_RE = re.compile(r"^(інше|инше|іньше|other|своя|свій|свое|своє)\b|\b(варіант|answer|option)$", re.IGNORECASE)

_SOURCES: dict[str, list] = {}
_SOURCES_MAX = 200
_MAX_SOURCE_BUTTONS = 8


_MAX_CITED_PER_SEARCH = 3


def _cite_entries(sources: Optional[list], entries: list) -> None:
    """Cite the top few codex pages a search surfaced. Capped at
    _MAX_CITED_PER_SEARCH because a result LIST is weaker evidence than a page
    open_entry actually read, and one loose search can return 50 rows - all of
    them would crowd out the sheet/web citations that answered the question."""
    if sources is None:
        return
    for e in entries[:_MAX_CITED_PER_SEARCH]:
        url = e.get("url") or ""
        _add_source(sources, e.get("name") or url,
                    f"https://playorna.com{url}" if url.startswith("/") else url)


def _add_source(sources: list, label: str, url: str) -> None:
    """Record one citation, newest last, de-duplicated by URL."""
    if not url or not str(url).startswith(("http://", "https://")):
        return
    if any(u == url for _l, u in sources):
        return
    if len(sources) < 24:  # a long research chain shouldn't grow unbounded
        sources.append((label.strip()[:60] or url, url))


def _remember(state: dict) -> str:
    if len(_STATE) >= _STATE_MAX:
        _STATE.pop(next(iter(_STATE)), None)
    key = uuid.uuid4().hex[:10]
    _STATE[key] = state
    return key


def _capabilities_text() -> str:
    """Fixed, deterministic reply for a meta "what can you do" ask, and the
    ONLY reference the model gets about the bot's own commands - deliberately
    NOT model-generated prose. Public commands only: admin ones (/go, /stats,
    /ban, /unban, /update_codex) are never listed here or anywhere the model
    can read, so it cannot reveal them."""
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
        "• /clarify <текст> — уточнити або продовжити останню відповідь /orna "
        "(розмова живе 15 хв після відповіді; новий /orna починає нову)\n"
        "• /res_today, /res_next <ресурс> — ресурси сьогодні / коли з'явиться ресурс\n"
        "• Скриншот предмета — якість і прокачка; скриншот «NEEDED OFFERINGS» — чого бракує\n"
        "• Скриншот «Memory completed» / «Спомин завершено» з підписом кольору відьми й номера "
        "варіанту (напр. «Red 4») — поділитися amity; /amity — amity цього тижня й хто поділився "
        "(попросіться до нього в пати); /amity delete — видалити свої; /iam <нік> — вказати свій нік у грі, "
        "він показується поруч з вами в /amity\n"
        "• /remind <час> <текст> — поставити нагадування (це окрема команда, не /orna)\n"
        "• /report <опис> — повідомити про помилку"
    )


_REMINDER_NUDGE = (
    "Це /orna — нагадування я тут не ставлю. Скористайтесь командою /remind, "
    "наприклад: /remind 18:00 купити пруфи."
)


# -----------------------------------------------------------------------------
# "today" / "next" - same data the existing /res_today, /res_next serve
# -----------------------------------------------------------------------------

# Cards a tool posts itself are rendered in the USER's language by code (see
# _mon_item): material names from the game's own Ukrainian table, dates and
# labels from these. Guild/tower names are proper nouns and stay as they are.
_UK_MONTHS = {"January": "січня", "February": "лютого", "March": "березня", "April": "квітня",
              "May": "травня", "June": "червня", "July": "липня", "August": "серпня",
              "September": "вересня", "October": "жовтня", "November": "листопада", "December": "грудня"}


def _uk_date(month_day: str) -> str:
    """'October 5' -> '5 жовтня'; anything else unchanged."""
    month, _, day = (month_day or "").partition(" ")
    return f"{day} {_UK_MONTHS[month]}" if month in _UK_MONTHS and day.isdigit() else month_day


def _fetch_failed(e, uk: bool) -> str:
    return f"Не вдалося отримати дані: {e}" if uk else f"Could not load the data: {e}"


async def _today_text(uk: bool = True, values: Optional[list] = None) -> str:
    today = get_today_month_day()
    try:
        values = values if values is not None else await fetch_sheet_data()
    except Exception as e:
        return _fetch_failed(e, uk)

    tdg: dict[str, list[str]] = defaultdict(list)
    for res in values:
        if not res:
            continue
        for i, date in enumerate(res[1:]):
            if date == today and i < len(GUILD_NAMES):
                tdg[GUILD_NAMES[i]].append(res[0])

    if not tdg:
        return f"Сьогодні ({_uk_date(today)}) немає ресурсів." if uk else f"No resources today ({today})."

    table = pre_table([[guild, ", ".join(_mon_item(m, uk) for m in materials)] for guild, materials in tdg.items()])
    return (f"📦 <b>Ресурси на {html.escape(_uk_date(today))}</b>" if uk else
            f"📦 <b>Resources for {html.escape(today)}</b>") + f"\n{table}"


async def _next_text(resource_query: str, uk: bool = True, values: Optional[list] = None) -> Optional[str]:
    """None means: not a known Material Forecast resource - caller should
    fall through to codex search instead."""
    try:
        values = values if values is not None else await fetch_sheet_data()
    except Exception as e:
        return _fetch_failed(e, uk)

    text = resource_query.lower().strip()
    lines = ["📅 <b>Коли й у якій гільдії з'явиться</b>" if uk else "📅 <b>When and where it appears next</b>"]
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
        lines.append(f"<b>{html.escape(_mon_item(res[0], uk))}:</b>")
        lines.append(pre_table([[g, _uk_date(d) if uk else d] for g, d in guild_dates]))

    return "\n".join(lines) if found else None


def _plain(html_text: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", html_text or ""))


async def _sheet_or_none():
    try:
        return await fetch_sheet_data()
    except Exception:
        return None          # the text builders report the failure themselves


async def _run_today_tool(message, uk: bool = True) -> str:
    # The model gets the DATA, not "sent it": live 2026-10-05 it could not tell
    # whether Red Draconite was in today's list and called today() twice.
    values = await _sheet_or_none()
    text = await _today_text(uk, values)
    await message.reply_text(text, parse_mode="HTML")
    return "Today's guild-shop materials (posted to the chat):\n" + _plain(await _today_text(False, values))


async def _run_next_tool(message, material: str, uk: bool = True) -> str:
    if not material:
        return "next needs a material name in action_input"
    values = await _sheet_or_none()
    result = await _next_text(material, uk, values)
    if result is None:
        return f"{material!r} is not a known Material Forecast resource - try search_codex or query instead"
    await message.reply_text(result, parse_mode="HTML", disable_web_page_preview=True)
    return "Guild shops and dates (posted to the chat):\n" + _plain(await _next_text(material, False, values) or "")


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


def _format_entry(detail: dict, aussies_url: Optional[str] = None) -> str:
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
    # Cross-link sections (e.g. "Dropped by", "Used in", "Gives") used to be
    # hidden behind per-section buttons that posted a SECOND message with the
    # names once tapped - in a 180-person group chat that's a dead button
    # every member can tap, each tap bloating the chat with another message.
    # Inlined as text instead (2026-10-01 ask): same names, zero extra taps,
    # zero extra messages. Capped at 100 per section like the open_entry
    # tool's own digest, so a huge cross-link (a popular material "used in"
    # dozens of recipes) doesn't blow the message up - "(+N more)" says so
    # rather than silently truncating.
    for section in detail.get("sections") or []:
        entries = section.get("entries") or []
        if not entries:
            continue
        names = ", ".join(html.escape(e.get("name", "?")) for e in entries[:100])
        if len(entries) > 100:
            names += f" (+{len(entries) - 100} more)"
        lines.append("")
        lines.append(f"<b>{html.escape(section.get('title', '?'))}:</b> {names}")
    if aussies_url:
        lines.append("")
        lines.append(f'📊 <a href="{html.escape(aussies_url)}">Aussie Codex</a>')
    return "\n".join(lines)


# A generous ceiling, not a normal bound: query_records already caps its own
# results at 50, so this shows every one of them (and then some) rather than
# hiding names the model needs to reason over - the 256k context has room. Still
# marked PARTIAL beyond it, so a truncation is never silent.
_MAX_NAMES_IN_OBSERVATION = 100


def _names_observation(entries: list, fmt=None) -> str:
    """The name list a search/query hands BACK to the model, as opposed to the
    buttons it posts to Telegram. Truncating this SILENTLY is a live bug
    (2026-09-25): search_codex returned "13 results for 'Judge Trifecta'"
    followed by only the first FIVE names, so the model opened exactly those
    five, never learned the other eight existed, and answered that the whole
    set was "warrior or thief classes only" - it had in fact missed four
    valhallan_summoner pieces. Nothing in the observation said the list was
    cut, and the model cannot read the buttons: whatever is not in this string
    does not exist as far as it is concerned. Same "(+N more)" honesty the
    open_entry section digest below already uses."""
    fmt = fmt or (lambda e: f"{e.get('name', '?')} ({e.get('url', '')})")
    listed = entries[:_MAX_NAMES_IN_OBSERVATION]
    names = "; ".join(fmt(e) for e in listed)
    if len(entries) > len(listed):
        names += (f" (+{len(entries) - len(listed)} MORE not listed - this list is PARTIAL, "
                  "do not describe the whole set from it)")
    return names


# Cap on the read-entries button's list - a loose query can match 50 rows.
_MAX_VIEWED_ENTRIES = 40

# How many entries finish() will POST as full cards instead of hiding behind
# the "Записи кодексу" button. The 2026-09-26 change deferred every card
# because a browsing request posted twelve of them and buried the answer; it
# also took the rich card away from the far more common "what is this item"
# lookup, which is the whole point of asking (reported 2026-09-30: "now the
# codex tool returns only the final description and a button"). A request that
# looked at one or two entries IS that lookup, so its card is the answer and
# gets posted; a request that browsed a dozen still gets the quiet button.
_AUTO_CARD_MAX_ENTRIES = 2


def _remember_entries(session, entries: list) -> None:
    """Record codex entries the loop LOOKED AT, for the one button finish()
    offers. Deduped by url, capped so a 50-row query cannot make an unusable
    keyboard.

    This is the whole of the 2026-09-26 UI change: the loop used to post a card
    per lookup - twelve full entry cards, or ten one-result search headers, on a
    single request - so the answer landed at the bottom of a wall of reasoning
    artefacts the user had to scroll past. The model's observations are
    unchanged, so the DATA is still in its context; only the chat gets quieter.

    NOTE this partly reverses an earlier explicit preference (CLAUDE.md: query
    results were once link-only buttons and were changed to render richly in
    chat, "once it was clear having the stats actually visible in the chat was
    the valuable part"). The stats are still visible in chat here - tapping an
    entry posts the identical card - they are just one tap away instead of
    automatic. Flagged rather than silently overwritten."""
    if session is None or not isinstance(getattr(session, "viewed_entries", None), list):
        return
    seen = {e.get("url") for e in session.viewed_entries}
    for entry in entries:
        url = entry.get("url")
        if not url or url in seen or len(session.viewed_entries) >= _MAX_VIEWED_ENTRIES:
            continue
        seen.add(url)
        session.viewed_entries.append({"name": entry.get("name") or url, "url": url,
                                       "tier": entry.get("tier"),
                                       "sort_value": entry.get("sort_value")})


async def _run_codex_search(message, query: str, lang: str = "en", sources: Optional[list] = None,
                            session=None) -> str:
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

    # A ONE-LETTER typo must be tried before the lossy ladder below. Live
    # 2026-10-05: "Red Dragonite" (real: Red Draconite) hit nothing, the ladder
    # dropped the leading word, found plain "Dragonite" - a different material -
    # and the answer described the wrong thing. A near-exact correction keeps
    # every word the user said; dropping one throws meaning away.
    if not results:
        near = await asyncio.to_thread(fuzzy_codex_name, query, _NEAR_EXACT_CUTOFF)
        if near:
            observation = await _corrected_search(query, near, session, sources)
            if observation:
                return observation

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
            # Recorded for finish()'s one button rather than posted - see
            # _remember_entries.
            _remember_entries(session, desc_entries)
            _cite_entries(sources, desc_entries)
            return (f"{len(desc_entries)} matches by description for {query!r}: "
                    f"{_names_observation(desc_entries)}")

    # Deliberately no reply_text here on a genuine dead end: this is an
    # intermediate step the model can still recover from (retry a
    # different name, fall back to query()) - posting "nothing found" as
    # its own chat message for every failed attempt along the way is
    # exactly the noise reported live (several dead-end messages plus a
    # step-budget-exhausted message, for a request that should have been
    # one clean answer). Only a genuinely final "nothing anywhere" belongs
    # in the user's chat, and that's finish()'s job once the model gives up.
    if not results:
        # LAST RESORT: the name may simply be misspelled or misheard. Live
        # 2026-09-26: "what crest of feeling does?" - the real item is `Crest of
        # the Felling`, ONE substituted letter away - and the loop answered "no
        # such item exists in the current codex database" while its own earlier
        # search for "crest" had listed the right name on screen. The mechanical
        # ladder above cannot reach that shape: it strips quality words,
        # possessives and trailing words, but a typo INSIDE a word needs a fuzzy
        # match against the real name vocabulary. Placed after every other
        # fallback so it can only turn a dead end into a hit.
        corrected = await asyncio.to_thread(fuzzy_codex_name, query)
        if corrected:
            observation = await _corrected_search(query, corrected, session, sources)
            if observation:
                return observation
        return f"0 results for {query!r}"

    _remember_entries(session, results)
    _cite_entries(sources, results)
    return f"{len(results)} results for {query!r}: {_names_observation(results)}"


# difflib ratio for a correction trusted BEFORE the word-dropping ladder: one
# wrong letter in a two-word name ("red dragonite" vs "red draconite" = 0.92).
_NEAR_EXACT_CUTOFF = 0.88


async def _corrected_search(query: str, corrected: str, session, sources) -> str:
    """Search `corrected` in place of a misspelled `query`; the observation
    says it was a correction, or "" when the corrected name finds nothing."""
    try:
        results = (await asyncio.to_thread(codex_search, corrected, "en")).get("results") or []
    except Exception:
        logger.warning("orna: fuzzy-name retry failed for %r", corrected)
        return ""
    if not results:
        return ""
    logger.info("orna: %r looks like %r - searched that instead", query, corrected)
    _remember_entries(session, results)
    _cite_entries(sources, results)
    # The model MUST be told it was a correction, or it will present the answer
    # as if the user's spelling was right - and the user never learns the name.
    return (f"0 results for {query!r}, but that looks like a misspelling of {corrected!r} "
            f"({len(results)} result(s)): {_names_observation(results)}. Answer about "
            f"{corrected!r} and SAY that is how you read the question.")


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


async def _run_query_tool(message, conditions: list, combinator: str, category: str, sort_by: str, sort_dir: str,
                          session=None, count: bool = False, group_by: str = "") -> str:
    conditions = [c for c in conditions if isinstance(c, dict)] if isinstance(conditions, list) else []
    aggregate = bool(count) or bool(group_by)
    if not conditions and not sort_by and not aggregate:
        return ('query needs ONE of: "count": true (for "how many"/a total/a breakdown - add '
                '"group_by" for a per-value split), at least one condition (to filter), or a '
                '"sort_by" (to rank). If you are counting, use "count": true - do NOT add a '
                'sort_by to get a list and then count its rows: a listing is capped at 50, so '
                'counting what you can see gives 50, not the real total.')

    # An unusable field name must not come back as "0 results" - that reads as
    # "nothing in the game has this" and gets reported to the user as fact. Say
    # the field is wrong so the next step can fix it. See
    # orna_aussies.unresolvable_condition_fields for the live failure.
    bad_fields = await asyncio.to_thread(unresolvable_condition_fields, conditions)
    if bad_fields:
        parts = []
        for kind, field, close in bad_fields:
            hint = f" - did you mean {', '.join(close)}?" if close else ""
            parts.append(f"{kind} field {field!r} does not exist{hint}")
        return ("query did NOT run - " + "; ".join(parts)
                + ". This is NOT an empty result: nothing was searched, so it says nothing about whether such "
                  "records exist. Re-issue the query with a real field, or filter on the entry's name with a "
                  '{"kind":"text","field":"name"} condition instead.')

    combine = combinator if combinator in ("and", "or") else "and"

    if aggregate:
        # "How many ...?" is a different question from "which ...?", and it
        # cannot be answered off the listing path: that one caps at 50, so
        # len(results) is the cap and reporting it is a confidently WRONG
        # number. Nothing is posted to the chat - a count is one number the
        # model restates in finish(), not a card.
        try:
            agg = await asyncio.to_thread(count_records, conditions, combine, category or None, group_by or "")
        except Exception as e:
            logger.warning("orna: count failed for %r group_by=%r", conditions, group_by, exc_info=True)
            return f"count failed: {e}"
        where = (" AND " if combine == "and" else " OR ").join(
            _describe_condition(c) for c in conditions) or "everything in the codex"
        scope = f"[{category}] {where}" if category else where
        if agg["groups"]:
            breakdown = ", ".join(f"{k}: {v}" for k, v in agg["groups"].items())
            return (f"COUNT for {scope} = {agg['total']} total, grouped by {agg['field']}: {breakdown}. "
                    f"These are exact full-database counts (not a capped sample) - report the numbers as they are.")
        return (f"COUNT for {scope} = {agg['total']}. This is an exact full-database count (not a capped "
                f"sample) - report the number as it is.")

    try:
        matches = await asyncio.to_thread(
            query_records, conditions, combine,
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
    # A truncated list must report the TRUE total, never len(matches) - that is
    # the CAP, and reporting it as the count is the observation-honesty rule's
    # exact failure ("50 matches" for 1598 real ones). Only paid for when the
    # cap was actually hit; the count is a second 5ms scan of the same records.
    total = len(entries)
    if total >= 50:
        try:
            total = (await asyncio.to_thread(
                count_records, conditions, combine, category or None, ""))["total"]
        except Exception:
            logger.warning("orna: count for truncated query failed", exc_info=True)
    suffix = f" (PARTIAL: showing the first 50 of {total})" if total > len(entries) else ""

    # Recorded for finish()'s one button rather than posted - see
    # _remember_entries, including the note on the earlier preference this
    # partly reverses. `suffix` is kept in the observation below instead.
    _remember_entries(session, entries)
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
    return (f"{total} matches for {summary}{suffix}: {_names_observation(entries, _fmt)}")


_SQL_MAX_ROWS = 50
_SQL_OBS_MAX = 12000


def _format_rows(columns: list, rows: list) -> str:
    """Rows as compact `col=value` lines - readable to the model without the
    byte cost of a padded table, and unambiguous when a value is empty (an
    aligned table's blank cell and a literal empty string look identical)."""
    if not columns:
        return "(no columns)"
    if len(columns) == 1:
        return "; ".join("∅" if r[0] is None else str(r[0]) for r in rows)
    out = []
    for r in rows:
        out.append(" | ".join(f"{c}={'∅' if v is None else v}" for c, v in zip(columns, r)))
    return "\n".join(out)


async def _run_sql_tool(message, sql: str, args: dict, session=None) -> str:
    """Run one model-written read-only SELECT against the codex database.

    This is the general-purpose counterpart to query(): query() is a fixed
    condition vocabulary (fast, safe, no SQL to get wrong) while this can
    COUNT, GROUP BY, join a record to its drops' stats, and full-text search -
    the questions a filter cannot express at all.

    Posts nothing to the chat: rows are evidence the model reasons over, the
    same as knowledge_search/web_search, not something to show a user raw.
    Every safety guarantee (read-only, one statement, 5s abort, row cap) is
    enforced in orna_codex_db.run_sql - in code, not in the prompt."""
    sql = (sql or "").strip()
    if not sql:
        return ("sql needs a SELECT statement in action_input. Call sql with action_input=\"schema\" "
                "to see the tables and columns first.")

    # The model does not have to carry the schema in its head (and the prompt
    # only shows an abridged version): asking for it is one cheap step.
    if sql.lower().strip(" ;`") in ("schema", "tables", ".schema", "show tables"):
        try:
            return "CODEX DATABASE SCHEMA:\n" + await asyncio.to_thread(orna_codex_db.schema_text)
        except Exception as e:
            logger.warning("orna: sql schema failed", exc_info=True)
            return f"could not read the schema: {e}"

    limit = args.get("limit") if isinstance(args, dict) else None
    try:
        limit = max(1, min(int(limit), _SQL_MAX_ROWS))
    except (TypeError, ValueError):
        limit = _SQL_MAX_ROWS

    try:
        # to_thread: SQLite work is synchronous CPU/disk and would otherwise
        # block the event loop for every chat, per CLAUDE.md's standing rule.
        result = await asyncio.to_thread(orna_codex_db.run_sql, sql, limit)
    except ValueError as e:
        # A rejected or malformed query is NOT an empty result - saying
        # "0 rows" here would be read as "the game has none of those".
        return (f"SQL did NOT run: {e}. Nothing was searched, so this says nothing about whether such "
                f"records exist. Fix the statement and retry, or call sql with action_input=\"schema\" "
                f"to check the real table/column names.")
    except Exception as e:
        logger.warning("orna: sql tool failed for %r", sql[:200], exc_info=True)
        return f"SQL failed: {e}"

    rows, columns = result["rows"], result["columns"]
    if not rows:
        return (f"0 rows for: {sql}. The query RAN and matched nothing - that is real evidence, but check the "
                f"column values are spelled as the schema lists them (e.g. rarity='celestial', not 'Celestial').")

    body = _format_rows(columns, rows)
    if len(body) > _SQL_OBS_MAX:
        body = body[:_SQL_OBS_MAX].rsplit("\n", 1)[0] + "\n… (cut here)"
        note = " PARTIAL - the text was cut, re-run with fewer columns or an aggregate if you need the rest."
    elif result["truncated"]:
        note = (f" PARTIAL - only the first {len(rows)} rows are shown; there are MORE. Do NOT count these "
                f"rows and report that as a total: re-run as SELECT count(*) for the real number.")
    else:
        note = " This is the COMPLETE result for that query."
    return f"{len(rows)} row(s) for: {sql}\n{body}\n{note}"


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


async def _run_towers_tool(message, uk: bool = True) -> str:
    """Current floor of all 5 "Wild Towers of Olympia" - pure
    deterministic math (orna_towers.py, ported line-for-line from
    OrnaCodex's own tower.ts and cross-checked against the original TS
    run under Node before deploying - see orna_towers._demo), not looked
    up from any data source at all. No args needed - cheap enough to
    always report all 5 and let the model read whichever one the request
    actually asked about. Also posts a "🔔 remind me at floor 50" button
    per tower not already there - see orna_towers.time_to_floor - modelled
    on telegram_resources.py's own guild reminder buttons, but simpler:
    the fire time here is a plain ELAPSED delay (now -> the tower's own
    ETA), not an absolute clock time, so there's no per-user UTC-offset
    ask to do first, unlike that flow's `request_utc_offset`."""
    now = datetime.datetime.now(datetime.timezone.utc)
    floors = orna_towers.get_tower_floors(now)

    NAME_W = 11
    # The time to floor 50 is ON the card: when only the buttons carried it, the
    # model re-listed every tower with its ETA under the card (live 2026-10-05).
    table_rows = [("Вежа" if uk else "Tower").ljust(NAME_W) + ("Поверх" if uk else "Floor").ljust(8)
                  + ("До 50" if uk else "To 50")]
    for tf in floors:
        label = ("МАКС" if uk else "MAX") if tf.floor >= 50 else str(tf.floor)
        eta = orna_towers.time_to_floor(now, tf.kind, 50)
        left = "" if eta is None else (lambda h: f"{h} год" if uk else f"{h}h")(
            int(-(-(eta - now).total_seconds() // 3600)))
        table_rows.append(tf.kind.capitalize().ljust(NAME_W) + label.ljust(8) + left)
    table_text = "\n".join(table_rows)
    lines = ["🗼 <b>Вежі Олімпії зараз:</b>" if uk else "🗼 <b>Towers of Olympia now:</b>",
             f"<pre>{html.escape(table_text)}</pre>"]

    upcoming = orna_towers.get_tower_floors_in_next_days(now, 1)
    if upcoming:
        nxt = upcoming[0]
        delta_min = int((nxt["time"] - now).total_seconds() // 60)
        when = nxt["time"].strftime("%Y-%m-%d %H:%M")
        lines.append(f"Наступна зміна поверхів: {when} UTC (за {delta_min} хв)" if uk else
                     f"Next floor change: {when} UTC (in {delta_min} min)")

    # One button per tower not yet at 50 - tapping schedules a plain
    # elapsed-delay reminder (telegram_remind.schedule_reminder) for the
    # EXACT eta orna_towers.time_to_floor computed, not a re-derived
    # estimate. `reminders` holds the absolute UTC eta (ISO, re-read at tap
    # time so a reminder tapped hours later still fires at the real
    # instant, not "eta minus however long the button sat there").
    reminders = []
    obs_parts = []
    for tf in floors:
        eta = orna_towers.time_to_floor(now, tf.kind, 50)
        if eta is None:
            obs_parts.append(f"{tf.kind}={tf.floor} (already at max)")
            continue
        hours = int(-(-(eta - now).total_seconds() // 3600))  # ceil to whole hours
        obs_parts.append(
            f"{tf.kind}={tf.floor} (reaches 50 at {eta.strftime('%Y-%m-%d %H:%M')} UTC, in {hours}h - "
            "this is the EXACT figure, do not recompute a different ETA from the floor count)")
        reminders.append({
            "kind": tf.kind,
            "eta": eta.isoformat(),
            "hours": hours,
            "text": (f"🗼 Вежа {tf.kind.capitalize()} досягла 50 поверху!" if uk else
                     f"🗼 Tower {tf.kind.capitalize()} has reached floor 50!"),
        })

    if reminders:
        key = _remember({"reminders": reminders, "scheduled": set()})
        rows = [
            [InlineKeyboardButton(
                (f"🔔 {r['kind'].capitalize()} — 50 поверх (за {r['hours']} год)" if uk else
                 f"🔔 {r['kind'].capitalize()} — floor 50 (in {r['hours']}h)"),
                callback_data=f"orna|towerrem|{key}|{i}")]
            for i, r in enumerate(reminders)
        ]
        await message.reply_text("\n".join(lines), parse_mode="HTML", reply_markup=InlineKeyboardMarkup(rows))
    else:
        await message.reply_text("\n".join(lines), parse_mode="HTML")

    return ("posted current tower floors (out of 50, 50=cleared/at the top) with a remind-me-at-50 button per "
            "tower not yet maxed; state the ETA/hours EXACTLY as given below, never re-derive a days-remaining "
            "estimate from the floor count alone (a live bug did this and said \"14 floors in 14 days\", i.e. "
            "assumed 1 floor/day, when the real rate is ~6 floors/day): " + "; ".join(obs_parts))


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


# A codex path as playorna actually serves them: /codex/<category>/<id>/.
# Anything else handed to open_entry was invented rather than taken from a tool
# result - see _codex_path_problem.
_CODEX_PATH_RE = re.compile(r"^/codex/[a-z-]+/[A-Za-z0-9_-]+/?$")


def _codex_path_problem(url: str) -> str:
    """Why `url` is not a codex page path, or "" if it is fine.

    open_entry only fetches playorna codex pages, and the model twice handed it
    something else (live 2026-09-26): `/codex/items/vritra charm/` - a path it
    built from an item NAME, space included, instead of using the url a search
    returned - and `https://playerecho.com/orna/circle-of-anguish`, a CITATION
    url from a knowledge_search block. Both raised inside fetch_codex_json,
    burned a step, and in the Vritra case the request went on to answer from
    invention. Failing with an instruction is strictly better than failing with
    a traceback: the loop can recover from the first."""
    url = (url or "").strip()
    if not url:
        return "no url given"
    if url.startswith(("http://", "https://")):
        host = url.split("/")[2].lower() if len(url.split("/")) > 2 else ""
        if "playorna.com" not in host:
            return (f"{url!r} is not a playorna codex page (host {host!r}). open_entry only opens "
                    "/codex/... pages from search_codex or query results. A citation link from "
                    "knowledge_search cannot be opened - the text it gave you IS the source")
        url = "/" + "/".join(url.split("/")[3:])
    if not _CODEX_PATH_RE.match(url):
        return (f"{url!r} is not a codex path. It must look like /codex/items/<id>/ and must come "
                "from a search_codex or query RESULT, not be built from the item's name (a name "
                "with a space in it is the giveaway) - call search_codex first and use the url it "
                "returns")
    return ""


async def _send_entry(message, entry_ref: dict, lang: str, post: bool = True) -> Optional[dict]:
    """Fetch a codex entry and (by default) post the full rendered card -
    sprite, facts/effects/tags, cross-link sections (Dropped by/Used in/...)
    and an Aussie Codex link, all as plain text now (see _format_entry) -
    returning its `detail` dict so a caller can build a text digest from it.

    `post=False` fetches and returns WITHOUT rendering anything. That is what
    the open_entry TOOL uses now: a request that reads twelve entries used to
    post twelve full cards, burying the actual answer under reasoning
    artefacts the user then had to scroll past (reported 2026-09-26 on the
    Judge Trifecta run). The model's observation is unchanged - the data stays
    in its context either way - and the user gets ONE button on the answer that
    opens any of those entries on demand. A button TAP still posts, since there
    the card IS what was asked for."""
    url = entry_ref.get("url")
    problem = _codex_path_problem(url)
    if problem:
        # Deliberately no reply_text: this is a recoverable mis-step for the
        # model to fix, not something to show the user (same reasoning as a
        # dead-end search). The tool wrapper returns `problem` as its
        # observation - see _run_open_entry_tool.
        logger.info("orna: refusing open_entry for %r - %s", url, problem[:80])
        return {"_problem": problem}
    try:
        data = await asyncio.to_thread(fetch_codex_json, url, lang)
    except Exception as e:
        logger.warning("orna: failed to fetch codex page %s", url, exc_info=True)
        if post:
            await message.reply_text(f"Не вдалося завантажити сторінку кодексу: {e}")
        return None

    detail = data.get("detail")
    if not detail:
        if post:
            await message.reply_text("Сторінку кодексу не вдалося розпізнати.")
        return None
    if not post:
        return detail        # tool path: the digest is the product, not a card

    sprite = detail.get("sprite")
    if sprite:
        try:
            upscaled = await _fetch_upscaled_sprite(sprite)
            await message.reply_photo(upscaled if upscaled else sprite)
        except TelegramError:
            logger.warning("orna: failed to send entry sprite %s", sprite, exc_info=True)

    # playorna urls are always "/codex/<category>/<id>/" - reuse that to
    # link an "Aussie Codex" page for the same record as a plain text link
    # (only 4 of 9 categories have one). This used to be a button, same as
    # the per-section cross-link buttons below _format_entry now inlines as
    # text - in a 180-person group chat every tappable button is another
    # member's dead click bloating the chat with a new message, and a plain
    # <a> link costs nothing to show and nothing to tap (Telegram opens it
    # directly, no bot reply involved).
    aussies_url = None
    parts = [p for p in url.split("/") if p]
    if len(parts) >= 3 and parts[0] == "codex" and has_aussies_page(parts[1]):
        aussies_url = build_aussies_url(parts[1], parts[2])

    await message.reply_text(
        _format_entry(detail, aussies_url),
        parse_mode="HTML",
        disable_web_page_preview=True,
    )
    return detail


async def _run_open_entry_tool(message, url: str, sources: Optional[list] = None,
                               session=None) -> str:
    if not url:
        return "open_entry needs a url in action_input (from a previous observation)"
    # post=False: see _send_entry. The entry is recorded on the session so
    # finish() can offer it, instead of a card landing in the chat now.
    detail = await _send_entry(message, {"url": url}, "en", post=False)
    if detail and detail.get("_problem"):
        # An invented url, not a fetch failure - say what is wrong so the next
        # step fixes it instead of retrying the same thing or answering from
        # memory (which is what happened live for the Vritra Charm).
        return f"open_entry did NOT run: {detail['_problem']}"
    if not detail:
        return f"couldn't open {url}"
    # Cite the pages actually READ. A search_codex result list isn't cited -
    # it only surfaced names, and those results are already tappable in chat.
    if sources is not None:
        _add_source(sources, detail.get("name") or url,
                    f"https://playorna.com{url}" if url.startswith("/") else url)
    if session is not None and isinstance(getattr(session, "viewed_entries", None), list):
        entry = {"name": detail.get("name") or url, "url": url, "tier": detail.get("tier"),
                 # READ, as opposed to merely listed by a search - finish()
                 # cards these first, so a one-item lookup shows the item the
                 # loop actually read and not every near-name the search hit.
                 "opened": True}
        for seen in session.viewed_entries:
            if seen.get("url") == url:
                seen["opened"] = True
                break
        else:
            session.viewed_entries.append(entry)
    facts = "; ".join(f"{f.get('label')}: {f.get('value')}" for f in (detail.get("facts") or []))
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
        names = ", ".join(e.get("name", "?") for e in entries[:100])
        if len(entries) > 100:
            names += f" (+{len(entries) - 100} more)"
        digest += f" | {section.get('title', '?')}: {names}"
    return digest


# Char budget for the codex half of the observation - a generous CEILING for a
# pathological multi-entity call, not a normal bound. The model has a 256k
# context, so populating it with the data it needs to reason is the goal; the
# biggest single entity (a 76-drop raid with full per-leaf stats) is ~15KB, well
# under this. Anything past it trims only at a line boundary (see
# _truncate_lines), never silently mid-value.
_RESEARCH_CODEX_MAX = 40000


def _truncate_lines(text: str, limit: int, marker: str) -> str:
    """Trim `text` to about `limit` chars, cutting ONLY at newline boundaries so
    a value is never clipped mid-number ("attack 244" -> "attack 24" would be a
    WRONG number, worse than missing), then append `marker`. Unchanged if it
    already fits."""
    if len(text) <= limit:
        return text
    kept, used = [], 0
    for line in text.split("\n"):
        if kept and used + len(line) + 1 > limit:
            break
        kept.append(line)
        used += len(line) + 1
    return "\n".join(kept) + marker


def _leaf_line(m: dict) -> str:
    """One compact analysis line for a cross-linked record. Shows ALL stats and
    effects - they are short and few (max 18 stats / 21 effects in the data),
    and a "which class benefits" answer reasons over them, so a silent cap here
    would quietly make the answer wrong."""
    slot = "/".join(x for x in (m.get("place"), m.get("item_type")) if x)
    tr = " ".join(x for x in (f"t{m['tier']}" if m.get("tier") else "", m.get("rarity") or "") if x)
    meta = ", ".join(x for x in (slot,
                                 f"useable_by={m['useable_by']}" if m.get("useable_by") else "",
                                 tr) if x)
    stats = ", ".join(f"{k} {v}" for k, v in (m.get("stats") or {}).items())
    bits = [f"{m['name']} [{m['category']}]"]
    if meta:
        bits.append(meta)
    if stats:
        bits.append(stats)
    if m.get("effects"):
        bits.append("effects: " + "; ".join(m["effects"]))
    return " — ".join(bits)


def _render_supergraph(bundle: dict) -> str:
    """One structured observation from build_supergraph's dict. Honest about
    caps (PARTIAL) and unresolved names; bounded to _RESEARCH_CODEX_MAX."""
    out = ["RESEARCH SUPERGRAPH (from local codex.json, zero network):"]
    for ent in bundle.get("entities", []):
        facts = ent.get("facts", {})
        fbits = [f"{k}={facts[k]}" for k in
                 ("tier", "rarity", "hp", "place", "item_type", "useable_by", "events") if k in facts]
        out.append(f"\n{ent['name']} [{ent['category']}]" + (" — " + ", ".join(fbits) if fbits else ""))
        if facts.get("stats"):
            out.append("  stats: " + ", ".join(f"{k} {v}" for k, v in facts["stats"].items()))
        if facts.get("effects"):
            out.append("  effects: " + "; ".join(facts["effects"]))
        if ent.get("bond"):        # a follower's bestial_bond, per tier
            out.append("  Bestial Bond (what it grants when bonded):")
            out.extend("    " + t for t in ent["bond"])
        if ent.get("alternatives"):
            alt = ", ".join(f"{n} [{c}]" for c, _i, n in ent["alternatives"][:5])
            out.append(f"  (note: this name also matches: {alt})")
        for rel in ent.get("relations", []):
            head = f"  {rel['title']} ({rel['total']}"
            head += f", showing first {len(rel['members'])}, PARTIAL" if rel["partial"] else ""
            head += "):"
            out.append(head)
            # skills are spells with no useable_by/stats worth a full line -> names only
            if rel["field"] == "skills":
                out.append("    " + ", ".join(m["name"] for m in rel["members"]))
            else:
                out.extend("    • " + _leaf_line(m) for m in rel["members"])
    if bundle.get("unresolved"):
        out.append("\nCould not resolve: " + ", ".join(bundle["unresolved"])
                   + " (not in the codex dump; try search_codex or a different spelling).")
    return _truncate_lines("\n".join(out), _RESEARCH_CODEX_MAX,
                           "\n[…codex section truncated at a line boundary - PARTIAL, "
                           "research fewer entities for the rest…]")


async def _run_research_tool(message, action_input: str, args: Optional[dict] = None,
                             sources: Optional[list] = None, session=None) -> str:
    """One-call supergraph for analytical questions: the entity, its drops/
    skills/upgrade-materials with each leaf's stats/useable_by/effects, PLUS
    the related community knowledge - so the model reasons over the whole
    subject in one step instead of chaining open_entry per drop. The codex
    half is fully local (build_supergraph reads only codex.json/translations).
    Posts no per-entity cards; finish() offers buttons to open any of them."""
    args = args or {}
    names = args.get("entities")
    if not names:
        raw = (action_input or "").strip()
        # A real entity name can itself contain "," or "and" ("Arisen Thor, the
        # Storm God", "Sword and Shield"), so resolve the WHOLE string first and
        # split into several entities ONLY when it doesn't resolve on its own -
        # an eager split mis-resolved those to a wrong item + an unresolved half.
        if raw and "id" in await asyncio.to_thread(resolve_entity, raw):
            names = [raw]
        else:
            names = [p.strip() for p in re.split(r",| and | та ", raw) if p.strip()] or [raw]
    names = [n for n in names if n]
    if not names:
        return "research needs an entity name in action_input"
    cap = args.get("per_relation_cap", 80)
    bundle = await asyncio.to_thread(build_supergraph, names, cap)
    codex_text = _render_supergraph(bundle)

    # Backstop for the prompt's "not for a class/specialization" rule: a class or
    # specialization name is NOT a codex entity - it collides with a same-named
    # boss (research "Gilgamesh" -> the boss Fallen Gilgamesh). If one was sent
    # anyway, say so on top of the (wrong-entity) codex result and point at the
    # tools that actually know classes/specs. find_class matches real class/spec
    # names one-directionally, so a plain item/monster query does not trip it.
    class_hits = sorted({n for n in names
                         if await asyncio.to_thread(orna_classes.find_class, n)})
    if class_hits:
        codex_text = ("NB: " + ", ".join(class_hits) + " is a class/specialization, not a codex entity - any "
                      "codex result below is a DIFFERENT same-named boss/monster. A class/spec's stats, "
                      "modifiers and whether it is a class vs a specialization come from estimate_stats / "
                      "knowledge_search (orna_classes), never from the codex.\n\n") + codex_text

    # record entities for finish() buttons + cite aussies (playorna url shape)
    for ent in bundle.get("entities", []):
        url = f"/codex/{ent['category']}/{ent['id']}/"
        if session is not None and isinstance(getattr(session, "viewed_entries", None), list):
            if not any(e.get("url") == url for e in session.viewed_entries):
                session.viewed_entries.append({"name": ent["name"], "url": url,
                                               "tier": ent["facts"].get("tier")})
        if sources is not None and has_aussies_page(ent["category"]):
            _add_source(sources, ent["name"], build_aussies_url(ent["category"], ent["id"]))

    # knowledge half - the same aggregation knowledge_search uses (best effort).
    # Join names (not just names[0]) so an entities-only call with no
    # action_input still gathers knowledge for every subject.
    subject = action_input or " ".join(names)
    knowledge = await _gather_knowledge(subject, sources)
    if knowledge:
        return codex_text + "\n\nCOMMUNITY KNOWLEDGE:\n" + knowledge
    return codex_text


_QUALITY_NAME_TO_PERCENT = {
    "broken": 50, "poor": 90, "regular": 100, "normal": 100,
    "superior": 101, "famed": 120, "legendary": 140, "ornate": 171,
}
_FORGED_LEVELS = {"masterforged": 11, "demonforged": 12, "godforged": 13}


# An UPGRADE LEVEL written into a quality spec ("185% lv10", "legendary +10",
# "рівень 10"). Quality and level are two independent axes - an item is 185%
# quality AND upgraded to 10 - and this parser used to return level 1 for
# every percentage, so a player's upgraded gear was always projected
# unupgraded. Only an explicit marker counts: a bare number is the quality.
_LEVEL_MARKER = r"(?:level|lvl|lv|рівень|рів|ур)"
_LEVEL_IN_SPEC_RE = re.compile(
    rf"\b{_LEVEL_MARKER}\.?\s*(\d{{1,2}})\b|\b(\d{{1,2}})\s*{_LEVEL_MARKER}\b|\+(\d{{1,2}})\b")
# A quality percentage sitting inside an item phrase ("Heretics Robe 200%").
_PCT_IN_NAME_RE = re.compile(r"(?<![\w.])(\d{1,3})\s*%")


def _level_in(text: str) -> Optional[int]:
    """The upgrade level written in `text`, in whichever of the three forms -
    "lv10", "20lvl", "+10" - or None. Capped at 20, the real ceiling for a
    celestial weapon (orna_assess.get_assess_result); the per-item code clamps
    again to the projection array it actually gets back."""
    found = _LEVEL_IN_SPEC_RE.search(text)
    if not found:
        return None
    return min(max(int(next(g for g in found.groups() if g)), 1), 20)


def _split_item_phrase(text: str) -> tuple:
    """(name, quality%, level) from an item phrase as a player actually types
    it: "Godforged Heretics Robe 200%" -> ("Heretics Robe", 200, 13),
    "Celestial Staff 20lvl" -> ("Celestial Staff", None, 20).

    Live 2026-09-25: the user gave every quality and forge tier in their
    request ("Godforged Fallen Sky Shoes 195%") and the bot asked for both
    anyway, twice, because splitting the phrase was left to the model. The
    tool does it now, so handing the phrase straight through is correct.
    A forge word is never part of a codex name so it is removed; a quality
    NAME might be ("Ornate ..."), so it is only READ, never removed -
    _resolve_aussies_entry's own candidate ladder strips it if it has to."""
    quality = level = None
    rest = text
    found = _LEVEL_IN_SPEC_RE.search(rest)
    if found:
        level = _level_in(rest)
        rest = rest[:found.start()] + " " + rest[found.end():]
    found = _PCT_IN_NAME_RE.search(rest)
    if found:
        quality = int(found.group(1))
        rest = rest[:found.start()] + " " + rest[found.end():]
    kept = []
    for word in rest.split():
        key = word.lower().strip(",.")
        if key in _FORGED_LEVELS:
            level = level if level is not None else _FORGED_LEVELS[key]
            continue
        kept.append(word)
    if quality is None and kept:
        for edge in (kept[0], kept[-1]):
            key = edge.lower().strip(",.")
            if key in _QUALITY_NAME_TO_PERCENT:
                quality = _QUALITY_NAME_TO_PERCENT[key]
                break
    return " ".join(kept).strip(" ,-"), quality, level


def _parse_quality_spec(spec: str) -> Optional[tuple]:
    """<quality%, level> from a free-text quality spec - a percentage
    ("185", "185%"), a named tier, an explicit upgrade level ("lv10",
    "+10", "рівень 10"), or any combination ("185% lv10").

    Orna has 13 levels: 1-10 plus Masterforged/Demonforged/Godforged, which
    are really upgrade LEVELS 11/12/13 in the game's own mechanics (past
    level 10, orna_assess.get_quality_code derives the quality bucket from
    LEVEL, not quality% - see that function), not a quality percentage, so
    those map to a level instead; quality defaults to 100% for them (a
    forged item assumed pushed to the tier's floor, not some arbitrary
    higher %). An explicit level wins over one implied by a forge name.
    The 7 percentage-tier names (Broken..Ornate) use that tier's own LOWER
    bound as a representative % (see get_quality_code's own thresholds)
    since there's no single canonical "the" percentage for a bare name -
    the caller notes this assumption in the reply rather than leaving it
    silent. Level defaults to 1 and quality to 100% when only the other one
    is given. None if the spec isn't parseable at all."""
    text = spec.strip().lower()
    level = _level_in(text)
    if level is not None:
        found = _LEVEL_IN_SPEC_RE.search(text)
        text = (text[:found.start()] + " " + text[found.end():])
    text = text.strip().rstrip("%").strip()
    if not text:
        return (100, level) if level is not None else None
    # "200% godforged" / "godforged 200%": a forge word is a LEVEL, so it
    # combines with a percentage rather than making the spec unparseable
    # (live 2026-09-29: the model then dropped "godforged" and assessed lv1).
    words = text.split()
    forged = [w for w in words if w in _FORGED_LEVELS]
    if forged and len(words) == 2:
        pct = _parse_quality_spec(next(w for w in words if w not in _FORGED_LEVELS))
        if pct is not None:
            return pct[0], level if level is not None else _FORGED_LEVELS[forged[0]]
    if text in _FORGED_LEVELS:
        return 100, level if level is not None else _FORGED_LEVELS[text]
    if text in _QUALITY_NAME_TO_PERCENT:
        return _QUALITY_NAME_TO_PERCENT[text], level or 1
    try:
        return int(round(float(text))), level or 1
    except ValueError:
        return None


# Dual wielding two one-handed weapons sums both and scales the pair. This is
# the guild's statement of game behaviour, not derivable from the codex (no
# other source in the repo states it) - treated exactly like the Ascension/PVP
# rules in orna_classes: implemented as given, and pinned in _demo.
_DUAL_WIELD_FACTOR = 0.65
# Orna's real slot capacities: two accessory slots, one of everything else,
# and two hands (so two one-handed weapons, or one two-hander).
_SLOT_CAPACITY = {"head": 1, "torso": 1, "legs": 1, "weapon": 2, "off-hand": 1, "accessory": 2}


def _check_loadout(worn: list) -> tuple:
    """Validate a set of worn items and decide whether it dual-wields.

    `worn` is [{"name", "place", "two_handed", "celestial"}]; returns (conflicts,
    dual_wield). `conflicts` are human-readable reasons the loadout cannot
    exist, so a caller can refuse instead of totalling up a character nobody
    can actually build.

    Live 2026-09-25: asked for "best magic items for head, torso, hands, legs,
    accessories", the loop chose the Celestial Archistaff (two_handed) AND the
    Arisen North Star (off-hand) and summed both. A two-handed weapon occupies
    BOTH hands - there is no off-hand and no second weapon beside it."""
    by_slot: dict = {}
    for item in worn:
        by_slot.setdefault(item.get("place") or "", []).append(item)
    weapons = by_slot.get("weapon", [])
    offhands = by_slot.get("off-hand", [])
    two_handed = [w for w in weapons if w.get("two_handed")]

    conflicts = []
    if two_handed:
        blockers = [w["name"] for w in weapons if w is not two_handed[0]]
        blockers += [o["name"] for o in offhands]
        if blockers:
            conflicts.append(
                f"{two_handed[0]['name']} is TWO-HANDED and fills both hands, so it cannot be worn with "
                f"{', '.join(blockers)}. Either drop the off-hand/second weapon, or use a one-handed weapon "
                f"instead - two one-handed weapons dual-wield at {int(_DUAL_WIELD_FACTOR * 100)}% of their "
                "COMBINED stats, which can beat a two-hander")
    if len(two_handed) > 1:
        conflicts.append("two TWO-HANDED weapons cannot both be worn: "
                         + ", ".join(w["name"] for w in two_handed))
    # Only ONE celestial weapon per player, whether it is the one-handed or the
    # two-handed kind (game rule, stated by the guild 2026-09-25). So a
    # "best of everything" pick cannot dual-wield two celestials - which is
    # exactly what a naive top-magic-per-slot search produces, since the
    # celestials top most stat rankings.
    celestial = [w for w in worn if w.get("celestial")]
    if len(celestial) > 1:
        conflicts.append("only ONE celestial weapon can be equipped, and these are both celestial: "
                         + ", ".join(w["name"] for w in celestial)
                         + " - keep one and pair it with a non-celestial weapon")
    for slot, cap in _SLOT_CAPACITY.items():
        here = by_slot.get(slot, [])
        if len(here) > cap:
            conflicts.append(f"{len(here)} items in the {slot} slot but only {cap} fit: "
                             + ", ".join(w["name"] for w in here))
    # Dual wield is two ONE-handed weapons in hand. An off-hand item is a
    # shield/orb, not a second weapon, so it does not trigger the factor.
    dual_wield = len(weapons) == 2 and not two_handed and not conflicts
    return conflicts, dual_wield


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
    fields instead of regex-matching scraped page text - including
    is_two_handed, which this file long assumed aussies did not expose: it
    does, as a TAG ("two_handed", on 106 items). While it was hardcoded False
    it both understated a celestial two-hander's adornment slots (orna_assess
    keys its slot base off this flag) and let estimate_stats total a two-handed
    weapon TOGETHER WITH an off-hand - an impossible loadout, reported live
    2026-09-25. The weapon SUBTYPE is not a substitute: archistaffs are 20
    two-handed and 67 one-handed.
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
        place=place,
        is_celestial_weapon=is_celestial_weapon,
        is_two_handed="two_handed" in (record.get("tags") or []),
        is_upgradable=is_upgradable,
        has_scaling_slots=has_scaling_slots,
        boss_scaling=boss_scaling,
    )


def _strip_quality_words(item_name: str) -> str:
    """`item_name` minus any leading/trailing quality words, or "" if that
    changes nothing. The vocabulary is the same two tables _parse_quality_spec
    already accepts, so there is no third list to keep in sync. Only strips at
    the EDGES - a real codex name could contain one of these words in the
    middle, and only the edges are where a "<quality> <item>" phrase puts it."""
    words = item_name.split()
    quality_words = set(_QUALITY_NAME_TO_PERCENT) | set(_FORGED_LEVELS)
    while words and words[0].lower().rstrip("%") in quality_words:
        words.pop(0)
    while words and words[-1].lower().rstrip("%") in quality_words:
        words.pop()
    out = " ".join(words)
    return out if out and out != item_name else ""


def _name_candidates(item_name: str) -> list:
    """Progressively looser forms of a free-text item name, for when the
    name as given finds nothing. Two real failure shapes, both live
    2026-09-24 on one six-item request:

    * the QUALITY is repeated inside the name ("godforged lost helmet") -
      natural for the model to pass, since that is how the user wrote it,
      but quality is a separate argument and the codex name is "Lost
      Helmet";
    * a trailing word that is not part of the name at all - the user's own
      qualifier ("arisen terror IN HAND", meaning which slot it is in), or
      a word the codex spells possessively so the full phrase misses
      ("court jester outfit" finds nothing, "court jester" finds "Court
      Jester's Outfit").

    Dropping trailing words covers both, and only ever runs after an exact
    lookup already came back empty, so it can only turn a dead end into a
    hit. Capped at 3 drops and never down to a bare single word, since a
    one-word remainder of a longer name matches far too loosely.

    A third shape, live 2026-09-25: the codex spells the FIRST word
    possessively and the user doesn't - "Cupid Locket" is "Cupid's Locket",
    "Heretics Robe" is "Heretic's Robe". Dropping the trailing word doesn't
    save these (bare "Cupid" matches the monster first), so the possessive
    forms are tried directly, before the lossier drops.

    It runs the OTHER WAY too, live 2026-09-25: the user writes a possessive
    the codex doesn't have - "Ymir's Brilliant Feathers" is really "Ymir
    Brilliant Feathers". Before this, the add-a-possessive branch fired on a
    head that already had one and produced only garbage ("Ymir's's ...",
    "Ymir''s ..."), so every caller dead-ended. Apostrophes are normalized to
    the straight one first, since a phone keyboard types the curly U+2019 and
    the codex uses the straight form throughout."""
    raw, item_name = item_name, item_name.replace("\u2019", "'")
    base = _strip_quality_words(item_name) or item_name
    out = [base] if base != raw else []
    words = base.split()
    if len(words) > 1:
        head = words[0]
        if "'" in head:
            # "Ymir's Brilliant Feathers" -> "Ymir Brilliant Feathers"
            out.append(" ".join([head.split("'")[0]] + words[1:]))
        else:
            # "Cupid Locket" -> "Cupid's Locket"; "Heretics Robe" -> "Heretic's Robe"
            for possessive in (head + "'s", head[:-1] + "'s" if head.lower().endswith("s") else ""):
                if possessive and possessive != head:
                    out.append(" ".join([possessive] + words[1:]))
    for n in range(1, 4):
        if len(words) - n < 2:
            break
        cand = " ".join(words[:-n])
        if cand != item_name:
            out.append(cand)
    return out


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
        # Live 2026-09-24: assess dead-ended on EVERY item of a six-item
        # request because the model passed the user's own wording through as
        # the name. The loop then fell back to search_codex/open_entry and
        # eventually answered that the items "were not found" - a failure that
        # read as the model ignoring its own tools and was really this.
        # search_codex (the TOOL) has had its own retry ladder for a while;
        # this resolver, which assess/compare/build_optimize all go through,
        # had none. See _name_candidates.
        for cand in _name_candidates(item_name):
            try:
                results = (await asyncio.to_thread(codex_search, cand, "en")).get("results") or []
            except Exception:
                logger.warning("orna: name retry failed for %r", cand)
                continue
            if results:
                logger.info("orna: resolved %r via looser name %r", item_name, cand)
                break
    if not results:
        # Same fuzzy last resort as _run_codex_search - assess/compare/
        # build_optimize/estimate_stats all dead-end here, and a misspelled item
        # name is exactly as likely from them.
        corrected = await asyncio.to_thread(fuzzy_codex_name, item_name)
        if corrected:
            try:
                results = (await asyncio.to_thread(codex_search, corrected, "en")).get("results") or []
            except Exception:
                results = []
            if results:
                logger.info("orna: resolved %r via fuzzy name %r", item_name, corrected)
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
    bonus_lines, bonus_observed = [], []
    for key in sorted(QUALITY_CODE_BONUS_KEYS & entry.stats.keys()):
        base = entry.stats.get(key)
        if not isinstance(base, (int, float)) or isinstance(base, bool):
            continue
        scaled = get_quality_bonus(base, quality, quality_code, entry.is_adornment, key)
        bonus_lines.append(f"{key.replace('_', ' ')}: {base:g}% base → {scaled:g}% at this quality")
        bonus_observed.append(f"{key}={scaled:g}%")
    if bonus_lines:
        reply += "\n\n<b>Бонус-статистики (поза основною таблицею):</b>\n" + "\n".join(f"• {l}" for l in bonus_lines)

    await message.reply_text(reply, parse_mode="HTML", disable_web_page_preview=True)
    # The SCALED bonus numbers go back to the model as text, not just into
    # the posted table - same rule query's "[sort_by=value]" already follows,
    # and for the same live reason: the model cannot read what it only sent
    # to Telegram. Live report 2026-09-24: asked to total the Orn Bonus of six
    # named godforged items, the loop assessed them correctly, got back only
    # "posted assessment for X", and answered "на жаль, не отримали точні дані
    # про бонуси" - the one number the whole request was about was computed and
    # then dropped on the floor.
    bonuses = ", ".join(bonus_observed) if bonus_observed else "none"
    return (f"posted assessment for {entry.name} at quality={quality}% level={level}. "
            f"{_projection_observation(result, level)} "
            f"Quality-scaled bonus stats [{bonuses}] - use THESE numbers, not the item's base values.")


def _projection_observation(result, level: int) -> str:
    """The projected combat stats as TEXT for the model. The table only went
    to Telegram, so the loop could not read the number it had just shown -
    live 2026-09-29: asked for a 200% godforged item's magic "+20%", it
    re-ran codex lookups and added 20% to the BASE stat instead."""
    rows = []
    for stat, row in result.stats.items():
        vals = [v for v in row.values]
        if not vals:
            continue
        at = vals[min(level, len(vals)) - 1]
        rows.append(f"{stat}={at:g} (base {row.base:g}; lv1..lv{len(vals)}: "
                    + "/".join(f"{v:g}" for v in vals) + ")")
    return (f"Projected stats at level {level} [{'; '.join(rows) or 'none'}] - these ARE this item's "
            "numbers at this quality/level; do further arithmetic on them, do not look the item up again.")


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
    names = [str(n).strip() for n in (item_names or []) if str(n).strip()][:12]
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
    out = _calculate(expression)
    # A pure PRODUCT is a stacking multiplier, and the answer the user wants
    # is the percentage (product - 1) * 100. Doing that one last step in its
    # head is exactly the arithmetic this tool exists to take away, and it
    # has slipped live twice: "x21.76" reported as "+1776%" (not +2076%),
    # and "x195.81" reported as "+95.8%" (not +19481%).
    #
    # Triggering on any product rather than only on the "(1 + b/100) * ..."
    # shape _AGGREGATE_RULE asks for: that was the first version, and it
    # MISSED the second slip, because the model had already converted each
    # bonus to a multiplier itself and wrote "21.757 * 1.25 * 2 * 2 * 1.2 *
    # 1.2 * 1.25" - a perfectly good stacking expression with no "(1 +" in
    # it. Requiring value > 1 keeps the note off ordinary shrinking or
    # non-bonus math; a "+" or "-" anywhere means it isn't a pure product.
    # ponytail: a plain "2 * 3" still gets the note. It is clearly labelled
    # and the model can ignore it; tightening this needs to know the caller's
    # intent, which the expression alone doesn't carry.
    if _is_pure_product(expression):
        try:
            value = float(out.rsplit("=", 1)[-1].strip())
        except ValueError:
            return out
        if value > 1:
            return f"{out}  [as a stacking bonus: x{value:g} total = +{(value - 1) * 100:g}% bonus]"
    return out


def _is_pure_product(expression: str) -> bool:
    """True when the result IS a multiplier: only "*" between the top-level
    terms. Live 2026-10-05 the old "(1 +" branch also labelled
    "(1+0.575)*(1+0.575) - 1" = 1.48 (already the BONUS, +148%) as "x1.48 =
    +48%", and "((...)-1)*100" = 148 as "x148 = +14706%" - and the model
    repeated the tool's wrong label. Anything subtracted, added or scaled by
    100 at the top level is no longer a multiplier."""
    top = expression
    while True:
        stripped = re.sub(r"\([^()]*\)", "", top)
        if stripped == top:
            break
        top = stripped
    return "*" in expression and not re.search(r"[+\-]|\b100\b", top)


_GUIDE_EXCERPT_CHARS = 20000


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


# knowledge_search can compose six source blocks; capped in TOTAL, not just
# per block - see the note where they are joined. A generous CEILING for the
# 256k-context model, not a tight bound: real multi-corpus results run a few KB,
# well under this, so the cap only bites a pathological query and then drops
# WHOLE blocks (marked), never a silent mid-block cut.
_KNOWLEDGE_OBS_MAX = 40000
# Per-corpus ceiling. Also generous: a single corpus rarely returns this much,
# but if one does it is trimmed at a LINE boundary with this marker, never
# clipped mid-row/mid-formula (a half-row is worse than a marked-short one).
_KN_BLOCK_MAX = 15000
_KN_TRIM = "\n[… trimmed at a line boundary - PARTIAL, ask a narrower question for the rest …]"


# Hits per Pinecone namespace. Knowledge chunks are table slices (small), so
# more of them; the rest are whole sections/threads/comments.
_VECTOR_TOP_K = {"knowledge": 10, "mechanics": 3, "echo": 6, "ornabook": 6, "qa": 6, "reddit": 6}
# Keep only hits within this of the query's BEST score across all corpora.
# Measured 2026-10-06: without it every query filled the 40k cap, and the cap
# drops blocks from the END - so "are summons followers" lost its best hit (a
# dev's direct answer, 0.50, in the last block) to 14 table rows at ~0.30.
_VECTOR_RELATIVE = 0.15


async def _vector_knowledge(query: str) -> Optional[dict]:
    """{namespace: [hit, ...]} from Pinecone (see orna_pinecone), or None to
    fall back to the grep scorers: Pinecone not configured, or any search
    failed - one dead namespace must not leave knowledge_search half-blind."""
    if not orna_pinecone.enabled():
        return None
    try:
        found = await asyncio.gather(*(asyncio.to_thread(orna_pinecone.search, ns, query, k)
                                       for ns, k in _VECTOR_TOP_K.items()))
    except Exception as e:
        logger.warning("orna: pinecone search failed for %r, falling back to grep (%s)", query[:60], e)
        return None
    floor = max((h["score"] for hits in found for h in hits), default=0) - _VECTOR_RELATIVE
    return {ns: [h for h in hits if h["score"] >= floor] for ns, hits in zip(_VECTOR_TOP_K, found)}


async def _gather_knowledge(query: str, sources: Optional[list] = None) -> str:
    """The shared community-knowledge aggregation both knowledge_search and
    research reuse (extracted so there is no second copy). Returns "" when
    nothing matched, for the caller to report. Every corpus below is a
    different PROVENANCE of answer, not a different question, which is why they
    ride one call rather than one tool each. Original notes preserved:

    Curated community reference (orna_knowledge.txt, see
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
    # asyncio.to_thread: same reasoning as _run_assess_tool's aussies
    # lookup - _load()'s first call does a synchronous disk read (306KB),
    # and a fuzzy-correction miss runs difflib over a ~3500-word
    # vocabulary; individually fast, but any blocking call on the event
    # loop stalls every other chat's request too, not just this one.
    # A query naming several things at once is handled inside
    # orna_knowledge.search now (its word-scoring fallback) rather than by
    # splitting on punctuation here - the model writes those lists with
    # commas, with "and", or with nothing at all between them.
    vec = await _vector_knowledge(query)

    def _vec_text(ns: str) -> str:
        """One namespace's hits as a block body, each hit cited."""
        hits = vec.get(ns) or []
        if sources is not None:
            for h in hits:
                _add_source(sources, h.get("title", ""), h.get("url", ""))
        return "\n\n".join(h["text"] for h in hits)

    if vec is not None:
        result = _vec_text("knowledge")
    else:
        result = await asyncio.to_thread(orna_knowledge.search, query, "", 60)
    # Cite the sheet+tab each matched section came from. search() prefixes
    # every block with "[<section title>]", and that title is the key
    # orna_knowledge.source_url resolves, so the citation is per-TAB rather
    # than one vague "the knowledge base" link.
    if vec is None and result and sources is not None:
        for line in result.split("\n"):
            if line.startswith("[") and line.endswith("]"):
                title = line[1:-1]
                url = await asyncio.to_thread(orna_knowledge.source_url, title)
                if url:
                    _add_source(sources, title, url)

    # The developer corpus (orna_reddit) is searched by the SAME tool rather
    # than getting its own: the model already picks between 18 actions, and
    # "community sheet" vs "what a dev said on reddit" is a distinction about
    # the ANSWER's provenance, not about which question to ask. Matched at
    # ENTRY level so a paragraph of reasoning arrives whole - see orna_reddit.
    if vec is not None:
        reddit = _vec_text("reddit")
    else:
        reddit_hits = await asyncio.to_thread(orna_reddit.search, query)
        if reddit_hits and sources is not None:
            for entry in reddit_hits[:3]:
                if entry.url:
                    _add_source(sources, entry.head[:60], entry.url)
        reddit = await asyncio.to_thread(orna_reddit.format_entries, reddit_hits) if reddit_hits else ""

    # Amities/crucibles live in neither the sheets nor the codex (checked:
    # aussies' codex.json has no such category), so they ride along on the
    # same tool rather than becoming a 19th action - see orna_bonuses.
    try:
        bonuses = await asyncio.to_thread(orna_bonuses.search, query)
    except Exception as e:
        logger.warning("orna: bonuses lookup failed for %r (%s)", query[:60], e)
        bonuses = ""

    # Curated, community-verified (2026) prose on how each core system works
    # (factions/ascension/quality/forging/adornments/towers/flasks/...) - the
    # gap the codex leaves for "how does X work" as opposed to "what are this
    # item's stats". to_thread: first call reads the file off disk. See
    # orna_mechanics.py.
    try:
        mechanics = _vec_text("mechanics") if vec is not None else \
            await asyncio.to_thread(orna_mechanics.search, query)
    except Exception as e:
        logger.warning("orna: mechanics lookup failed for %r (%s)", query[:60], e)
        mechanics = ""

    blocks = []
    if result:
        blocks.append(_truncate_lines(result, _KN_BLOCK_MAX, _KN_TRIM))
    if mechanics:
        if sources is not None:
            _add_source(sources, orna_mechanics.SOURCE_TITLE, orna_mechanics.SOURCE_URL)
        blocks.append(
            "GAME MECHANICS (community-verified 2026 reference - how the system works in "
            "general; for an exact current number prefer the codex / releases()):\n" + mechanics)
    if bonuses:
        if sources is not None:
            _add_source(sources, "Amities / Crucibles (aussiescodex)", orna_bonuses.AMITIES_URL)
        blocks.append("AMITY / CRUCIBLE DATA (aussiescodex):\n" + _truncate_lines(bonuses, _KN_BLOCK_MAX, _KN_TRIM))

    # Class/specialization stat modifiers, bonus stats and passives. Not in
    # the codex either - see orna_classes.
    try:
        classes = await asyncio.to_thread(orna_classes.search, query)
    except Exception as e:
        logger.warning("orna: class lookup failed for %r (%s)", query[:60], e)
        classes = ""
    if classes:
        blocks.append(
            "CLASS / SPECIALIZATION DATA (aussiescodex stats estimator). Stat modifiers are PERCENTAGES "
            "applied to your gear-derived stats; a tier-10 specialization also has absolute base stats. "
            "Ascension Level adds +1% per level to every stat (AL 100 doubles them), and PVP doubles HP "
            "only:\n" + _truncate_lines(classes, _KN_BLOCK_MAX, _KN_TRIM))
    # Written guides that state the MECHANICS AND FORMULAS outright - the one
    # thing no other source here has (the codex gives an entry's numbers and
    # never a formula; the sheets tabulate results). Rides on this tool rather
    # than becoming a 19th action, same reasoning as the amity/class/reddit
    # blocks above: this is another provenance of answer, not another question.
    try:
        echo = _vec_text("echo") if vec is not None else await asyncio.to_thread(orna_echo.search_text, query)
    except Exception as e:
        logger.warning("orna: echo lookup failed for %r (%s)", query[:60], e)
        echo = ""
    if echo:
        if vec is None and sources is not None:
            for sec in await asyncio.to_thread(orna_echo.search, query):
                _add_source(sources, sec.label[:60], sec.url)
        blocks.append(
            "GUIDE MECHANICS / FORMULAS (playerecho.com community guides - the source to quote for a "
            "FORMULA or a mechanic the codex has no field for: Ward capacity, Ascension altar costs, "
            "dungeon cooldowns and godforging, anguish proofs, per-event tier gates. Indented lines are "
            "verbatim formulas - use them as written rather than reasoning one out):\n" + _truncate_lines(echo, _KN_BLOCK_MAX, _KN_TRIM))
    # Ornabook (book.cadelabs.ovh): a community mechanics book, read by the SAME
    # section reader as orna_echo (orna_echo.search with its own path) rather
    # than a second search engine. Audited against the other corpora on
    # 2026-10-05 by READING them, after a regex pass mislabelled two covered
    # facts as new:
    #   * genuinely new: the dual-wield BONUS formula, (1 + 0.65*B)^2 - the
    #     repo had only a rejected "50% for world bonuses" claim for that -
    #     raid "sanding", per-stat buff rows (T. Crit ↑↑↑ also gives +10% Att,
    #     not Mag), Drakeblight's 500 cap;
    #   * already covered, now independently CORROBORATED: buff tiers
    #     +25/+50/+100% and that they stack multiplicatively (orna_knowledge),
    #     hybrid fighting the mean of Def and Res (orna_mechanics);
    #   * one CONFLICT, unresolved: T. Crit ↑↑↑ is +60% here and +80% in the
    #     community sheet, and no dev comment settles it - recorded in
    #     orna_mechanics.txt so an answer states both rather than picking one.
    try:
        book = _vec_text("ornabook") if vec is not None else \
            await asyncio.to_thread(orna_echo.search_text, query, 6, _ORNABOOK_PATH)
    except Exception as e:
        logger.warning("orna: ornabook lookup failed for %r (%s)", query[:60], e)
        book = ""
    if book:
        if vec is None and sources is not None:
            for sec in await asyncio.to_thread(orna_echo.search, query, 6, _ORNABOOK_PATH):
                _add_source(sources, sec.label[:60], sec.url)
        blocks.append(
            "ORNABOOK MECHANICS (book.cadelabs.ovh, a community-written guide - quote it for the dual-wield "
            "BONUS formula, how buffs stack, per-stat status-effect tiers, dungeon modes/options and key-cost "
            "multipliers, raids and sanding, gauntlets, Anguish 2.0. Where it and another source give a "
            "DIFFERENT number, say so and give both rather than picking one. Labels in [brackets] are decoded game icons: "
            "[T. Att ↑↑↑] is the temporary triple attack buff, [Def] the defense stat; '—' is an empty "
            "table cell, kept so every number stays under its own column. It is community-written: the "
            "codex and releases() outrank it for any number they also state):\n"
            + _truncate_lines(book, _KN_BLOCK_MAX, _KN_TRIM))
    # Player Q&A, indexed by the QUESTION rather than by an answer's wording -
    # the one axis none of the other corpora have, and the only source carrying
    # a CORRECTED PREMISE ("those are summons, not followers").
    try:
        qa = _vec_text("qa") if vec is not None else await asyncio.to_thread(orna_qa.search_text, query)
    except Exception as e:
        logger.warning("orna: qa lookup failed for %r (%s)", query[:60], e)
        qa = ""
    if qa:
        if vec is None and sources is not None:
            for th in await asyncio.to_thread(orna_qa.search, query):
                _add_source(sources, f"r/OrnaRPG: {th.title}"[:60], th.url)
        blocks.append(
            "PLAYER Q&A (r/OrnaRPG threads where someone asked this before. The [Nup] figure is that answer's "
            "upvotes - a heavily-upvoted answer is strong evidence and a 2up one is weak; DEV marks Orna's own "
            "developers. Read these for a CORRECTED PREMISE too: the top answer often says the question itself is "
            "based on a misunderstanding, which is worth more than answering it as asked. Each block carries its "
            "DATE - an old answer may predate a patch, so releases() outranks it on numbers):\n" + _truncate_lines(qa, _KN_BLOCK_MAX, _KN_TRIM))
    if reddit:
        blocks.append(
            "DEVELOPER COMMENTS (Orna's own devs on reddit - more authoritative than the community "
            "sheets, but some are years old, so a later patch may have changed the numbers; check "
            "releases() before quoting a figure that matters):\n"
            + _truncate_lines(reddit, _KN_BLOCK_MAX, _KN_TRIM)
        )
    if not blocks:
        return ""
    # TOTAL cap, not just a per-block one. Each block was capped individually
    # (3000, 2000, ...) but knowledge_search now composes up to SIX of them -
    # sheets, player Q&A, guide formulas, mechanics, class data, dev comments -
    # and one call was measured at 14,909 characters. That is a large slice of
    # the step's context spent on sources that may all be marginal, which is the
    # opposite of helping the model reason. Whole blocks are dropped from the END
    # (they are appended in deliberate order) and the model is TOLD how many, so
    # it can narrow the query rather than assume it saw everything - the same
    # "never let a truncation look complete" rule as _names_observation.
    out, dropped = [], 0
    used = 0
    for block in blocks:
        if used + len(block) > _KNOWLEDGE_OBS_MAX and out:
            dropped += 1
            continue
        out.append(block)
        used += len(block) + 2
    if dropped:
        out.append(f"[{dropped} further source block(s) omitted to keep this observation readable - "
                   "ask a NARROWER question if you need them.]")
    return "\n\n".join(out)


# The Ornabook corpus, read by orna_echo's section reader (see
# orna_scrape_ornabook.py for why the format is shared, not duplicated).
_ORNABOOK_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "orna_ornabook.txt")

def _monument_line(group: list) -> str:
    """One floor of one monument: its matched cells. A category cell is shown
    WITHOUT its slot label on purpose: the slots are named "Reward 1/2/3", and
    live (2026-10-05) "Reward 3: Proofs" was read as "3 proofs" - the answer
    claimed floors "each give 3 proofs" and "Ithra only gives up to 2" from a
    chart that has no quantities at all. Material/Potion keep their label,
    since there the label says what KIND of thing is named."""
    parts = []
    for r in group:
        name = r["name"] + (f" ({r['value']})" if r["name"] != r["value"] else "")
        parts.append(name if r["kind"] == "category" else f"{r['slot']}: {name}")
    return ", ".join(parts)


# Cards a tool posts are localized by CODE from the game's OWN Ukrainian text,
# never by a model and never by hand: names from the codex item list
# (orna_item_names_uk.json, paired by slug), and each reward category below
# from the Ukrainian codex where the game says it. Live 2026-10-05 hand-written
# labels ("Відмички", "Броня") were wrong - the game says Ключ, Обладунки.
_MONUMENT_UK = {
    "materials": "Матеріали",          # codex item-type filter
    "potions": "Цілющі засоби",        # codex item-type filter ("useable" = Curative)
    "armor": "Обладунки",              # codex item-type filter
    "weapon": "Зброя",                 # codex item-type filter / Місце
    "accessory": "Аксесуар",           # an accessory's Місце fact
    "proofs": "Відзнаки",              # items "Відзнака агонії", ...
    "orns": "Орни",                    # the orns CURRENCY (game: "Бонус орн")
}
# Categories that ARE a codex item - rendered by that item's own Ukrainian name.
_MONUMENT_ITEM = {"skeleton keys": "Skeleton Key", "arena tokens": "Arena Token",
                  "monster remains": "Monster Remains", "astralseed": "Astralseed"}


def _mon_label(value: str, uk: bool) -> str:
    low = value.lower()
    if not uk:
        return value
    return _MONUMENT_UK.get(low) or ITEM_EN_TO_UK.get(_MONUMENT_ITEM.get(low, value), value)
def _uk(session) -> bool:
    """Whether a card a tool posts itself should be in Ukrainian."""
    return getattr(session, "user_lang", "") == "Ukrainian"


def _mon_item(name: str, uk: bool) -> str:
    return (EN_TO_UK.get(name) or ITEM_EN_TO_UK.get(name, name)) if uk else name


def _monument_floor_cells(group: list, uk: bool = False) -> str:
    """One floor for the CHAT (the model reads _monument_line instead): a named
    material/potion replaces its generic slot - "Materials" + "Material: Pure
    Darkstone" is just "Pure Darkstone"."""
    named = {r["kind"]: _mon_item(r["name"], uk) for r in group if r["kind"] != "category"}
    cells = []
    for r in group:
        if r["kind"] == "category":
            kind = {"materials": "material", "potions": "potion"}.get(r["value"].lower())
            cells.append(named.pop(kind) if kind in named else _mon_label(r["name"], uk))
    return ", ".join(cells + list(named.values()))   # + a named item with no generic slot


async def _run_monuments_tool(message, query: str, args: dict, sources: Optional[list] = None,
                              session=None) -> str:
    """Which monument and floor gives what THIS WEEK (floorchart.top).

    A specific-name lookup ("where do I get adamantine") posts nothing: it is
    one or two floors, and the model's one-sentence answer was right every time
    measured. A CATEGORY lookup ("which monument gives proofs") POSTS the exact
    floor list itself, like `towers` posts its table - because it is a list of
    numbers, and live (2026-10-05) the model re-typing one got a floor wrong 1
    run in 3 even with the exact list in its observation. Code does not mis-copy.
    Week staleness and an empty match are said outright, never left to infer."""
    args = args if isinstance(args, dict) else {}
    floor = args.get("floor")
    try:
        floor = int(floor) if floor not in (None, "") else None
    except (TypeError, ValueError):
        floor = None
    try:
        res = await asyncio.to_thread(orna_monuments.search, (query or "").strip(),
                                      str(args.get("monument") or ""), floor)
    except Exception as e:
        logger.warning("orna: monuments failed for %r", query, exc_info=True)
        return f"monuments did NOT run: the floorchart.top chart could not be loaded ({e})."
    if sources is not None:
        _add_source(sources, "floorchart.top monument rewards", orna_monuments.SITE_URL)

    head = (f"MONUMENT REWARDS, week {res['week']} (floorchart.top, community-entered each week; matched "
            f"{res['matched_as']}). It says WHAT drops on each floor, never HOW MANY - the chart has no "
            "quantities, so never state an amount. A generic category (\"Proofs\", \"Materials\") is shown "
            "alone; \"Material:\"/\"Potion:\" name the exact item.")
    if res["stale"]:
        head += (f" STALE: the site still shows week {res['week']} but it is now week {res['current_week']} - "
                 "the new chart has not been entered yet. Say so; do not present these as this week's rewards.")
    if not res["matches"]:
        return (head + f"\n0 matches for {query!r} on any monument floor this week. Not in this week's "
                "rotation - it may appear in a later week.")

    by_floor: dict = {}
    for r in res["matches"]:
        by_floor.setdefault((r["monument"], r["floor"]), []).append(r)
    lines = [f"- {mon} floor {fl}: {_monument_line(grp)}" for (mon, fl), grp in sorted(by_floor.items())]
    if str(res["matched_as"]).startswith("category"):
        # "Which monument gives the most X" needs a real number, and the only
        # real one is how many FLOORS carry it - so hand it over, labelled as
        # floors, rather than leave the model to invent a quantity. With the
        # EXACT floor list inline: live 2026-10-05, re-listing proofs from 25
        # near-identical lines, the model added floors that are not there
        # (Demeter 9, Ithra 11, Thor 12). Copying one line beats rebuilding it.
        per: dict = {}
        for mon, fl in sorted(by_floor):
            per.setdefault(mon, []).append(fl)
        summary = "; ".join(f"{m} - {len(f)} floor(s): {', '.join(map(str, f))}"
                            for m, f in sorted(per.items(), key=lambda kv: -len(kv[1])))
        summary = f"BY MONUMENT (exact floors; copy these, do not re-derive them): {summary}"
        uk = _uk(session)
        cat = str(res["matched_as"]).split(chr(39))[1]
        label = _mon_label(cat.title(), uk)
        blocks = [f"<b>{html.escape(label)}</b> " + (
            f"у монументах, тиждень {res['week']}" + (" (застаріло - новий тиждень ще не внесено)"
                                                      if res["stale"] else "") if uk else
            f"in the monuments, week {res['week']}" + (" (stale - the new week is not entered yet)"
                                                       if res["stale"] else ""))]
        floors_word = "пов." if uk else "floors"
        for mon, floors in sorted(per.items(), key=lambda kv: -len(kv[1])):
            rows = [[str(fl), ", ".join(_mon_item(r["name"], uk) for r in by_floor[(mon, fl)]
                                        if r["kind"] != "category") or label] for fl in floors]
            if all(r[1] == label for r in rows):   # nothing named: the floors are the whole story
                blocks.append(f"<b>{mon}</b> ({len(floors)} {floors_word}): {', '.join(map(str, floors))}")
            else:
                blocks.append(f"<b>{mon}</b> ({len(floors)} {floors_word})\n" + pre_table(rows))
        blocks[-1] += f'\n\n<a href="{orna_monuments.SITE_URL}">floorchart.top</a>'
        try:
            await send_report_blocks(message, blocks)
            posted = _POSTED_NOTE
            if session is not None:
                session.posted_note = "the monument list was already posted to the chat above the answer"
        except Exception:
            logger.warning("orna: monuments card failed to send", exc_info=True)
            posted = ""
        # When every cell matched is the same generic word ("Proofs"), the
        # per-floor lines carry nothing the summary does not - drop them.
        if len({r["name"] for r in res["matches"]}) == 1:
            return head + "\n" + summary + ("\n" + posted if posted else "")
        lines.append(summary)
        if posted:
            lines.append(posted)
    elif len(by_floor) > 3:
        # A list-shaped result that is NOT a category - the whole chart ("що
        # зараз в монументах?"), one monument, one floor across monuments - is
        # also posted by the tool. Live 2026-10-05: the whole-chart ask posted
        # nothing, and the model answered "Ось список нагород..." - pointing at a
        # list the user never received (REVIEW flagged it; the redo repeated
        # it). One message per monument keeps each under Telegram's 4096 limit.
        uk = _uk(session)
        title = (f"Нагороди монументів</b>, тиждень {res['week']}" + (
                     " (застаріло - новий тиждень ще не внесено)" if res["stale"] else "") if uk else
                 f"Monument rewards</b>, week {res['week']}" + (
                     " (stale - the new week is not entered yet)" if res["stale"] else ""))
        rows_by: dict = {}
        for (mon, fl), grp in sorted(by_floor.items()):
            rows_by.setdefault(mon, []).append([str(fl), _monument_floor_cells(grp, uk)])
        blocks = [f"<b>{mon}</b>\n" + pre_table(rows) for mon, rows in rows_by.items()]
        blocks[0] = f"<b>{title}\n\n" + blocks[0]
        blocks[-1] += f'\n\n<a href="{orna_monuments.SITE_URL}">floorchart.top</a>'
        posted = ""
        try:
            # packed into as few messages as fit (was one message per monument)
            await send_report_blocks(message, blocks)
            posted = _POSTED_NOTE
            if session is not None:
                session.posted_note = "the full monument chart was already posted to the chat above the answer"
        except Exception:
            logger.warning("orna: monuments chart failed to send", exc_info=True)
        if posted:
            lines.append(posted)
    return head + "\n" + "\n".join(lines)


async def _run_knowledge_tool(message, query: str, sources: Optional[list] = None) -> str:
    """knowledge_search tool: the shared community-knowledge aggregation
    (_gather_knowledge), with the empty case reported so the model falls
    through to web_search."""
    if not query:
        return "knowledge_search needs a query in action_input"
    out = await _gather_knowledge(query, sources)
    return out or f"no knowledge-base matches for {query!r} - try web_search instead"


# Gear stats ADD together; the class/AL/PVP layer multiplies on top. Keeping
# those two phases separate is the whole reason this isn't done in the model's
# head - see orna_classes.estimate for the second half.
_ESTIMATE_STATS = ("hp", "mana", "attack", "defense", "magic", "resistance",
                   "dexterity", "foresight", "crit", "ward", "view_distance")
# A RUNAWAY GUARD, not a game rule. The game has no ceiling anyone has
# documented - players above AL 500 are real (reported 2026-09-24), and an
# earlier version of this capped at a made-up 200, which would have silently
# clamped such a player's estimate to a far-too-low number. It exists only
# because AL multiplies EVERY stat, so a model typo like 1000000 renders an
# astronomically wrong table that still looks well-formed. Set far above any
# plausible real value; raise it freely if players ever get near it.
ASCENSION_LEVEL_SANITY_CAP = 10_000


def _as_bool(value) -> bool:
    """A tool argument as a bool, tolerating the string forms the model
    actually sends. `bool("false")` is True, so a plain bool() silently
    turned pvp="false" into PVP mode and doubled the user's HP - the exact
    "unvalidated LLM tool argument" shape the pitfalls list warns about."""
    if isinstance(value, str):
        return value.strip().lower() not in ("", "false", "0", "no", "none", "ні", "нi", "нет")
    return bool(value)


# Explicit "I have none" - an ANSWER, not a missing input. Not every player
# has a tier-10 specialization (aussiescodex's own estimator ships a "None"
# entry for exactly this), so "none" has to be distinguishable from silence.
_NO_VALUE_WORDS = {"none", "no", "n/a", "-", "немає", "нема", "ні", "нi", "нет", "без"}

# A tool prefixes its observation with this when it could not run because the
# USER still has to supply something. The loop watches for it: the model is
# told to ask(), but it sometimes states the same thing in finish() instead,
# which ENDS the request - and then the user's typed answer has nothing
# listening for it ("Bot ignored my answer", live 2026-09-25).
_NEEDS_INPUT = "NEEDS_INPUT:"


def _reassign_class_pools(spec: str, klass: str) -> tuple:
    """Put each name in the slot its POOL says it belongs to, whichever key it
    arrived under.

    orna_classes' pool names are aussiescodex's and are INVERTED from the game's
    (that module's docstring has the measurement): its `spec_stats` pool is
    really the tier-10 **CLASS** (Gilgamesh, Heretic Ara, Beowulf) and its
    `classes` pool is really the **SPECIALIZATION** (Ranger, Sequencer). Since
    the model is now taught the game's vocabulary, it sends
    class="Gilgamesh"/specialization="Sequencer" - the opposite of what this
    function's callers index by. Both conventions have to work, and the name
    itself settles it: the two pools are fully DISJOINT, so no name is ever
    ambiguous.

    Returns (spec, klass) in orna_classes' OWN terms, i.e. spec = the tier-10
    class, klass = the specialization - the slots the caller then resolves.
    A name in neither pool is left where it came from, so the caller's existing
    "that is not a real name" message still names the slot the model filled.
    """
    def belongs(name: str, kind: str) -> bool:
        return bool(name) and orna_classes.find_class(name, kind=kind) is not None

    # "none" is a real ANSWER for the tier-10 class slot, never a misfiled name.
    spec_none = bool(spec) and spec.lower() in _NO_VALUE_WORDS
    if spec and klass and not spec_none and belongs(spec, "class") and belongs(klass, "specialization"):
        return klass, spec                      # both filled, both in the other's pool
    if spec and not klass and not spec_none and not belongs(spec, "specialization") \
            and belongs(spec, "class"):
        return "", spec                         # a specialization alone, in the class slot
    if klass and not spec and not belongs(klass, "class") and belongs(klass, "specialization"):
        return klass, ""                        # a tier-10 class alone, in the spec slot
    return spec, klass


async def _run_estimate_stats_tool(message, args: dict, sources: Optional[list] = None) -> str:
    """Estimate a character's stats. BASE stats (from the specialization + AL,
    optionally the class's modifiers) and ITEM stats (from worn gear) are
    computed SEPARATELY and shown as their own blocks, plus a combined total
    when both are present - so a player can ask for base stats alone (just
    class, spec and AL, no gear) OR for a full loadout.

    Order of operations:
      * gear: each item assessed at its own quality/level (the same
        orna_assess.get_assess_result path /orna assess uses), summed - gear
        stats are ADDITIVE;
      * base: the tier-10 specialization's absolute base stats;
      * both blocks are then run through orna_classes.scale, which applies the
        class's percent modifiers, then Ascension Level (+1%/level, AL 100
        doubles), then PVP (HP doubled). scale() is LINEAR per stat, so
        base_scaled + items_scaled equals scaling the combined block - the
        total is exact, just broken out. The AL/PVP rules live only in
        orna_classes.scale, pinned by that module's self-check.

    REQUIRED: Ascension Level, and at least one of (a real specialization ->
    base stats, or items -> gear stats). OPTIONAL, never demanded: `class`
    (its modifiers are applied when given), `items`, and `pvp` (defaults to
    PVE - a base-stats question is "class, spec, AL" and shouldn't drag the
    user through a PVP prompt; the assumption is stated in the reply). An
    item's quality defaults to 100% and level to 1. The tool refuses a call
    it can't compute anything from and hands back exactly what's missing,
    rather than guessing."""
    max_items = 12
    items = args.get("items") or []
    if isinstance(items, (str, dict)):
        # a lone string would iterate characters; a lone dict is unsliceable
        # (live probe: TypeError "unhashable type: 'slice'")
        items = [items]
    if not isinstance(items, (list, tuple)):
        items = []
    # The KEYS are the game's words (class = the tier-10 class, specialization =
    # the passive package on top); orna_classes' pools are named the other way
    # round, so `spec` below is the tier-10 class and `klass` the
    # specialization. See its docstring for the measurement, and
    # _reassign_class_pools for the tolerance when the two arrive crossed.
    spec = str(args.get("class") or args.get("klass") or "").strip()
    klass = str(args.get("specialization") or "").strip()
    amities = args.get("amities") or {}

    spec, klass = _reassign_class_pools(spec, klass)
    if klass.lower() in _NO_VALUE_WORDS:
        klass = ""      # "no specialization" is an answer, not an unknown name

    # Resolve each name in ITS OWN pool. Live bug: "Heretic Ara Sequencer"
    # was passed as specialization="Heretic Ara", class="Heretic", and the
    # class lookup returned the tier-10 SPECIALIZATION (searched first by
    # default), whose modifiers are empty - so Sequencer's real -5/+15/-5 were
    # silently dropped and the estimate looked fine.
    spec_none = spec.lower() in _NO_VALUE_WORDS
    spec_entry = orna_classes.find_class(spec, kind="specialization") if spec and not spec_none else None
    class_entry = orna_classes.find_class(klass, kind="class") if klass else None

    # `or` would collapse a legitimate AL of 0 into "absent"; these three have
    # to tell "the user said 0/false" apart from "nobody has said yet".
    al_raw = next((args[k] for k in ("ascension_level", "al")
                   if args.get(k) is not None and str(args[k]).strip() != ""), None)
    al = None
    if al_raw is not None:
        try:
            al = min(max(int(float(str(al_raw).strip().rstrip("%"))), 0), ASCENSION_LEVEL_SANITY_CAP)
        except (TypeError, ValueError):
            al = None                      # "AL 100" - report it, never silently 0
    pvp_raw = args.get("pvp")
    pvp_given = pvp_raw is not None and str(pvp_raw).strip() != ""
    # PVP defaults to PVE rather than being required: a base-stats question is
    # "class, spec, AL" and must not drag the user through a PVP prompt. The
    # assumption is stated in the reply, never silent.
    pvp = _as_bool(pvp_raw) if pvp_given else False

    # NOTE the pool names are inverted (see _reassign_class_pools): the
    # "specialization" pool holds the tier-10 CLASS names, so the list a message
    # calls "class" is built from all_names("specialization") and vice versa.
    specs_list = _class_names_text("class")
    classes_list = _class_names_text("specialization")
    # REQUIRED: AL, and a base source (a real spec) or gear (items). class and
    # pvp are OPTIONAL - only a NAME the user actually typed but that doesn't
    # resolve is worth asking to fix (a typo), never a name they simply omitted.
    need = []
    if al is None:
        need.append("ascension_level: their AL as a plain number"
                    + (f" - {str(al_raw)[:20]!r} is not one" if al_raw is not None else ""))
    if klass and class_entry is None:
        need.append(f"specialization: {klass!r} is not a specialization - use one of {specs_list}, "
                    "or omit it")
    if spec and not spec_none and spec_entry is None:
        need.append(f"class: {spec!r} is not a tier-10 class - use one of {classes_list}, or \"none\"")
    if spec_entry is None and not items and not (spec and not spec_none):
        # Nothing to compute from: no (valid) specialization for base stats and
        # no items for gear stats. (If they typed a spec that just didn't
        # resolve, the line above already tells them how to fix it.)
        need.append("a tier-10 class (for BASE stats - one of " + classes_list + ') and/or items (for GEAR '
                    "stats, as [{\"name\":\"<item>\",\"quality\":\"<quality or %>\"}]) - at least one is required. "
                    'If they named a BUILD rather than items ("the omniflask raid build"), call class_guide or '
                    "knowledge_search FIRST and pass the item names it lists")
    if need:
        # Refuse the un-computable call in the TOOL, not the prompt. Live
        # failures were guessed inputs rendered as fact (a loadout the user
        # never mentioned; a lone class that rendered an empty table).
        return (_NEEDS_INPUT + " estimate_stats did NOT run, and showed the user NOTHING - these are missing or "
                "unusable:\n- " + "\n- ".join(need)
                + "\nDo NOT guess any of them, and do NOT call this again with the same arguments. Call ask() ONCE "
                "for exactly the items above, in one question (the user can type it all in a single message). "
                "Reminder: BASE stats need only class + specialization + AL; items and pvp are OPTIONAL (pvp "
                "defaults to PVE). If you cannot ask (inline mode), say plainly which of these you still need.")

    gear_raw, lines, skipped = {}, [], []
    if len(items) > max_items:
        skipped.append(f"передано {len(items)} предметів — враховано перші {max_items}")
    worn: list = []
    for raw in items[:max_items]:
        if isinstance(raw, dict):
            name = str(raw.get("name") or raw.get("item") or "").strip()
            quality = str(raw.get("quality") or "").strip()
            level_raw = next((raw[k] for k in ("level", "upgrade_level", "lvl")
                              if raw.get(k) is not None and str(raw[k]).strip() != ""), None)
        else:
            name, quality, level_raw = str(raw).strip(), "", None
        if not name:
            continue
        # Quality and LEVEL are two independent axes and both default here:
        # quality to 100% and level to 1 (per the game's own 13 levels - 1-10
        # plus masterforged/demonforged/godforged, which ARE levels 11/12/13).
        # The old default was 200%/level 13, a fully forged item, so anything
        # the user didn't spell out came back silently inflated.
        # Either may also be written into the NAME itself, which is how people
        # actually type a loadout ("Godforged Fallen Sky Shoes 195%") - read
        # them out rather than making the model split the phrase, which it
        # answered by asking the user for both all over again.
        name, phrase_q, phrase_level = _split_item_phrase(name)
        parsed = _parse_quality_spec(quality) if quality else None
        if quality and parsed is None:
            skipped.append(f"{name}: якість {quality[:15]!r} не розібрано — рахую 100%")
        if parsed is not None:
            q, spec_level = parsed
            # _parse_quality_spec always returns a level, defaulting to 1, so
            # only a level it really found beats one written in the name.
            level = spec_level if spec_level != 1 else (phrase_level or 1)
        else:
            q = phrase_q if phrase_q is not None else 100
            level = phrase_level or 1
        if level_raw is not None:
            try:
                level = min(max(int(float(str(level_raw).strip())), 1), 20)
            except (TypeError, ValueError):
                skipped.append(f"{name}: рівень {str(level_raw)[:10]!r} не розібрано — рахую {level}")
        entry, err = await _resolve_aussies_entry(name)
        if entry is None:
            skipped.append(f"{name} ({err})")
            continue
        inp = AssessInput(entry=entry, level=level if entry.is_upgradable else 1,
                          boss_scaling=entry.boss_scaling, quality=q, stats={})
        result = get_assess_result(inp, is_quality_calc=True)
        # AssessResult.stats is {stat: StatRow}, and StatRow.values holds one
        # projected value PER upgrade level. Take the requested level (or the
        # top one), NOT entry.stats - those are the item's unupgraded base,
        # which is what a first version silently summed: a godforged Lost
        # Helmet came out at its base 172 defense instead of 472.
        got = {}
        for stat, row in ((result.stats if result is not None else None) or {}).items():
            values = getattr(row, "values", None) or []
            if not values:
                continue
            idx = min(max(level, 1), len(values)) - 1
            if isinstance(values[idx], (int, float)):
                got[stat] = values[idx]
        if not got:
            # A non-scaling item (material, flat accessory) has no projection -
            # fall back to its raw stats rather than contributing nothing.
            got = {k: v for k, v in (entry.stats or {}).items() if isinstance(v, (int, float))}
        worn.append({"name": entry.name, "place": entry.place, "two_handed": entry.is_two_handed,
                     "celestial": entry.is_celestial_weapon,
                     "got": {k: v for k, v in got.items()
                             if k in _ESTIMATE_STATS and isinstance(v, (int, float))},
                     "q": q, "level": level})

    # An impossible loadout must not be totalled up: a two-handed weapon with
    # an off-hand is not a character anyone can build, and a stat block for one
    # is confidently wrong rather than approximate (live 2026-09-25). Refuse and
    # say why, so the loop re-picks - deliberately NOT the NEEDS_INPUT prefix,
    # which arms the wait-for-a-typed-answer path: this needs the MODEL to
    # choose a legal loadout, not the user to supply anything.
    conflicts, dual_wield = _check_loadout(worn)
    if conflicts:
        return ("estimate_stats did NOT run and showed the user NOTHING - that loadout cannot be worn:\n"
                + "\n".join(f"- {c}" for c in conflicts)
                + "\nPick a legal loadout and call estimate_stats again. Do not present stats for the "
                  "impossible one, and do not tell the user it works.")

    for item in worn:
        # Two one-handed weapons: the pair contributes 65% of its COMBINED
        # stats (see _DUAL_WIELD_FACTOR), which is why dual-wielding can still
        # beat a two-hander despite the penalty.
        factor = _DUAL_WIELD_FACTOR if (dual_wield and item["place"] == "weapon") else 1.0
        for stat, value in item["got"].items():
            gear_raw[stat] = gear_raw.get(stat, 0) + value * factor
        note = f" ×{_DUAL_WIELD_FACTOR} (dual wield)" if factor != 1.0 else ""
        lines.append(f"{item['name']} @ {item['q']}% lv{item['level']}{note}: " +
                     ", ".join(f"{k} {v:g}" for k, v in sorted(item["got"].items())))

    # BASE (specialization's absolute stats) and GEAR each go through the SAME
    # class-modifier + AL + PVP layer (orna_classes.scale), but stay separate
    # so base-only works and the gear contribution is shown on its own. scale()
    # is linear per stat, so base_scaled + items_scaled == scaling the combined
    # block - the total is exact, just broken out.
    class_mods = (class_entry or {}).get("stat_modifiers")
    base_scaled = orna_classes.scale((spec_entry or {}).get("base_stats") or {}, class_mods, al, pvp)
    items_scaled = orna_classes.scale(gear_raw, class_mods, al, pvp)

    if not base_scaled and not items_scaled:
        # Everything supplied was valid but there's nothing to show: no spec
        # (so no base) and not one item name resolved. Never post an empty table.
        return ("estimate_stats computed NOTHING, so the user was shown NOTHING: no specialization was given for "
                f"base stats and none of the item names resolved ({'; '.join(skipped) or 'no items given'}). "
                "Re-check spelling with search_codex and call again with real names, add a specialization for "
                "base stats, or ask the user.")

    total = {}
    for stat in _ESTIMATE_STATS:
        v = round(base_scaled.get(stat, 0) + items_scaled.get(stat, 0), 1)
        if v:
            total[stat] = v

    def _stat_table(block: dict) -> str:
        return pre_table([["Стат", "Значення"]] + [[k, f"{block[k]:g}"] for k in _ESTIMATE_STATS if block.get(k)])

    detail = []
    # spec_entry is the tier-10 CLASS and class_entry the SPECIALIZATION - the
    # pools are named the other way round (see _reassign_class_pools). These
    # two labels used to follow the POOL name, so the table read
    # "клас: Sequencer / спеціалізація: Heretic Ara", exactly backwards.
    if spec_entry:
        detail.append(f"клас: {_class_display(spec_entry['name'])}")
    elif spec_none:
        detail.append("клас: немає")
    if class_entry:
        detail.append(f"спеціалізація: {_class_display(class_entry['name'])}")
    detail.append(f"AL {al}")
    detail.append("PVP (HP ×2)" if pvp else ("PVE — припущення (напишіть «pvp» для PVP)" if not pvp_given else "PVE"))

    head = ["🧮 <b>Оцінка характеристик</b>", " · ".join(detail)]
    if base_scaled:
        head += ["", "<b>Базові стати</b> (клас · спеціалізація · AL):", _stat_table(base_scaled)]
    if lines:
        head += ["", "<b>Від предметів:</b>", "\n".join(f"• {html.escape(l)}" for l in lines)]
        if items_scaled:
            head += ["<i>внесок предметів (після AL / модифікаторів класу):</i>", _stat_table(items_scaled)]
    if base_scaled and items_scaled:
        head += ["", "<b>Разом (база + предмети):</b>", _stat_table(total)]
    if amities:
        head += ["", "Аміті/бонуси: " + html.escape(", ".join(
            f"{k} {v}" for k, v in amities.items()) if isinstance(amities, dict) else str(amities))]
    # Class and specialization PASSIVES are conditional bonuses the stat table
    # cannot express - Sequencer's are literally "Sequencer Doublecast (Dual
    # Staffs)" and "Sequencer Weapon Power (Dual Staffs)", i.e. they only apply
    # when dual-wielding staves. orna_classes.json has carried them all along
    # and nothing surfaced them, so an estimate silently ignored exactly the
    # nuance that decides whether a loadout is good (reported live 2026-09-25).
    passives = [str(x) for x in ((class_entry or {}).get("passives") or [])]
    passives += [str(x) for x in ((spec_entry or {}).get("passives") or [])]
    # ...plus the abilities DISCOVERED from the codex for this class/spec, with
    # what each one does. orna_classes.json only carries passiveEffects for 13
    # classes and none of the tier-10 specializations, so relying on it alone
    # meant a Gilgamesh/Deity/Heretic estimate listed no passives at all. Every
    # one of aussies' 82 classes has a structured `abilities` list and
    # translations.en.json describes all 134 - so this generalises to every
    # specialization from DATA, with no rule written per class.
    abilities = []
    for who in ((spec_entry or {}).get("name"), (class_entry or {}).get("name")):
        if not who:
            continue
        try:
            found = await asyncio.to_thread(orna_aussies_class_abilities, who)
        except Exception as e:
            logger.warning("orna: ability lookup failed for %r (%s)", who, e)
            continue
        for ab in found:
            if ab["name"] not in {a["name"] for a in abilities}:
                abilities.append({**ab, "owner": who})
    hand_note = ""
    if abilities:
        head += ["", "<b>Здібності класу/спеціалізації</b> (таблиця їх НЕ враховує):"]
        head += ["\n".join(f"• <b>{html.escape(a['name'])}</b> — {html.escape(a['description'][:190])}"
                            if a["description"] else f"• <b>{html.escape(a['name'])}</b>"
                            for a in abilities)]
    if passives:
        head += ["", "<b>Пасивки класу/спеціалізації</b> (умовні — таблиця їх НЕ враховує):",
                 "\n".join(f"• {html.escape(x)}" for x in passives)]
        if any("dual" in x.lower() for x in passives):
            hand_note = ("виконано: дві одноручні зброї" if dual_wield
                         else "НЕ виконано: немає двох одноручних зброй")
            head += [f"<i>умова «dual» — {hand_note}</i>"]
    if dual_wield:
        head += [f"<i>дві одноручні зброї: їхні стати враховані як ×{_DUAL_WIELD_FACTOR} від суми</i>"]
    if skipped:
        head += ["", "⚠️ не враховано: " + html.escape("; ".join(skipped))]
    await message.reply_text("\n".join(head), parse_mode="HTML", disable_web_page_preview=True)

    shown = ("base+items" if base_scaled and items_scaled else "base" if base_scaled else "items")
    summary = ", ".join(f"{k}={total[k]:g}" for k in _ESTIMATE_STATS if total.get(k))
    note = f" NOT counted: {'; '.join(skipped)} - say so in your answer." if skipped else ""
    pvp_note = " Assumed PVE (user didn't say - mention it)." if not pvp_given else ""
    # The passives and the dual-wield state go in the OBSERVATION, not only in
    # the posted table: the model cannot read what was only sent to Telegram,
    # and these are exactly the facts it must reason with when saying whether a
    # loadout is a good one.
    passive_note = ""
    if abilities:
        passive_note += (" [class/spec abilities (not in the table, mention any that change the answer): "
                         + "; ".join(f"{a['name']}: {a['description'][:400]}" for a in abilities[:20]) + "]")
    if passives:
        passive_note += (f" [conditional passives NOT in the table: {'; '.join(passives)}]"
                        + (f" [dual-wield condition {hand_note}]" if hand_note else ""))
    dual_note = (f" [dual wield: two one-handed weapons, their stats counted at "
                 f"x{_DUAL_WIELD_FACTOR} of the combined total]" if dual_wield else "")
    return (f"posted a stats estimate [{shown}] ({len(lines)} item(s); "
            f"class={_class_display(spec_entry['name']) if spec_entry else 'none'}, "
            f"specialization={_class_display(class_entry['name']) if class_entry else '-'}, "
            f"AL={al}, pvp={pvp}). Totals [{summary}].{note}{pvp_note}{dual_note}{passive_note} The table is already "
            "shown - finish() just needs a short closing line repeating WHICH inputs were used (class, spec, AL, "
            "PVE/PVP), plus any conditional passive above that the loadout does or does not satisfy, so the user can "
            "spot a wrong assumption.")


async def _run_releases_tool(message, query: str, sources: Optional[list] = None) -> str:
    """playorna.com's own patch notes (orna_releases, disk-cached a week).

    The codex and the community sheets both describe what IS and say nothing
    about what CHANGED, and the sheets are hand-maintained so they can lag a
    balance patch by weeks - an answer built from them can be confidently
    stale with nothing in the data hinting at it. These notes are the one
    source that does hint it. Same no-reply_text shape as knowledge_search:
    raw changelog lines aren't something to show verbatim, the model reads
    them and writes the caveat itself.

    asyncio.to_thread for the same reason every other data access here uses
    it - a cache miss does a blocking HTTP fetch, and that would stall the
    whole bot for every chat, not just this request."""
    try:
        notes = await asyncio.to_thread(orna_releases.search, query, 6)
    except Exception as e:
        logger.warning("orna: releases lookup failed for %r", query, exc_info=True)
        return f"couldn't read the patch notes: {e}"
    if not notes:
        return (f"no patch note mentions {query!r} - the notes on file cover only the last few months, "
                "so this may simply predate them; don't treat that as proof nothing changed")
    if sources is not None:
        for note in notes[:3]:
            _add_source(sources, f"{note['title']} ({note['date']})", note.get("url") or orna_releases.RELEASES_URL)
    return await asyncio.to_thread(orna_releases.format_notes, notes)


async def _run_web_search_tool(message, query: str, sources: Optional[list] = None) -> str:
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
    if sources is not None:
        for item in (data.get("sources") or [])[:5]:
            _add_source(sources, item.get("title") or item.get("url", ""), item.get("url", ""))
    return data["text"][:2000]


# -----------------------------------------------------------------------------
# the ReAct loop
# -----------------------------------------------------------------------------

def _class_display(name: str) -> str:
    """How a class/specialization name is SPELLED for a human or the model.
    aussiescodex misspells Deity as "Diety"; orna_classes keeps their spelling
    (it is their data) and find_class resolves either, but printing the typo -
    in a reply, or as the valid value in the prompt's own list - teaches it back
    to the model and shows the user a class name the game does not use."""
    return (name or "").replace("Diety", "Deity")


def _class_names_text(kind: str) -> str:
    """A class/specialization pool's names, for anything the MODEL reads.
    `kind` is orna_classes' own, i.e. "specialization" is the tier-10 CLASS."""
    return "/".join(_class_display(n) for n in orna_classes.all_names(kind))


_TOOLS_TEXT = (
    "- today(): no input. Materials available today in the guild shops (Material Forecast sheet). Posts the list.\n"
    "  WHERE TO GET A MATERIAL has three sources, and a \"where do I get X\" question checks all three: monster "
    "drops (search_codex/open_entry, its Dropped by), the guild shops (next), and this week's monuments "
    "(monuments).\n"
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
    "- sql(action_input=<one read-only SELECT>): the SAME codex data as a real SQLite database, for the "
    "questions a fixed filter CANNOT express: COUNT/SUM/AVG/MIN/MAX, GROUP BY breakdowns, joining a record to "
    "its drops'/materials' own stats, and full-text search over every field at once. Use this for \"how many\", "
    "\"what is the total/average/breakdown\", \"which X has the most Y per Z\", and for anything about "
    "STATUSES/buffs/debuffs/class abilities as things in their own right (those are NOT codex records - they "
    "live in the `terms` table with their descriptions). Prefer query() for a plain \"which records match these "
    "attributes\" filter; reach for sql() when you need to aggregate, join, or count. Read-only: SELECT (or "
    "WITH ... SELECT) only, one statement, no ';'. Send action_input=\"schema\" to get the exact tables, "
    "columns and the real spelling of every enum value - do that FIRST rather than guessing a column name. "
    "NEVER count the rows it returns and report that as a total: the result is capped, so a count needs an "
    "actual SELECT count(*). Abridged schema:\n"
    "    records(rid, category, id, name, description, tier, rarity, useable_by, item_type, place, family, "
    "type, targets, spell_type, hp, price, exotic, new, hidden, json)\n"
    "    stats(rid, field, value, raw) -- long/narrow: ANY of 157 stats, e.g. field='magic'\n"
    "    effects(rid, kind, code, name, chance) -- kind: immunities|causes|gives|cures\n"
    "    links(rid, relation, target_category, target_id, target_name) -- drops, dropped_by, "
    "upgrade_materials, skills, used_by, ...\n"
    "    labels(rid, kind, value) -- tags, events;  bonds(rid, bond_tier, type, name, value, raw) -- followers\n"
    "    terms(kind, code, name, description) -- kind='status' (312 buffs/debuffs), 'abilities' (134, with "
    "descriptions), 'stats', 'rarity', 'element', ...\n"
    "    search(name, description, body) -- FTS5 over records; `WHERE search MATCH 'sword'`, 'name: sword', "
    "'blind*', and join back on search.rid = records.rid\n"
    "  Materials are records: category='items' AND item_type='material'. Pets are category='followers'. "
    "Examples: SELECT count(*) FROM records WHERE category='items' | SELECT category, count(*) FROM records "
    "GROUP BY category | SELECT r.name, s.value FROM stats s JOIN records r USING(rid) WHERE s.field='magic' "
    "ORDER BY s.value DESC LIMIT 10 | SELECT t.name, t.description FROM terms t WHERE t.kind='status' AND "
    "t.name LIKE '%bleed%' | SELECT l.target_name, s.value FROM links l JOIN records r USING(rid) JOIN stats s "
    "ON s.rid=(SELECT rid FROM records WHERE category=l.target_category AND id=l.target_id) WHERE "
    "r.name='Fallen King Centaurus' AND l.relation='drops' AND s.field='attack'\n"
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
    "- research(action_input=<entity name(s)>, args={\"entities\":[...], \"per_relation_cap\":12}): the DEFAULT for "
    "an analytical or comparative question about a monster/boss/raid/item/follower - \"what does X drop and which "
    "class benefits\", \"how do I beat X\", \"compare what these bosses drop\". ONE call returns the whole subgraph "
    "from the local codex (the entity, plus its drops/skills/upgrade-materials with EACH one's stats, useable_by "
    "and effects) PLUS the community knowledge (incl. Monster-Data elemental immunities). Call it ONCE with every "
    "entity you need (pass several in args.entities), read the whole result, then finish - do NOT open_entry each "
    "drop one by one; that is the slow path this replaces. It also satisfies the STRATEGY rule below. "
    "NOT for a CLASS, SPECIALIZATION or CLASS LINE (Gilgamesh/Heretic Ara/Ranger/Sequencer/Mage/Thief/...): "
    "those are not codex entities here - a class name usually collides with a same-named BOSS (research "
    "\"Gilgamesh\" returns the boss Fallen Gilgamesh, not the class), and class/spec stats/modifiers live in "
    "orna_classes, not the codex. For anything about a class/specialization use estimate_stats (stats) or "
    "knowledge_search (what it gives, and whether a name is a class vs a specialization).\n"
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
    "or a named tier (broken/poor/regular/superior/famed/legendary/ornate/masterforged/demonforged/godforged), "
    "and a forge word COMBINES with a percentage - pass \"200% godforged\" whole, never drop either half - "
    "pass whichever form the user gave verbatim, don't convert it yourself. This POSTS the full table to the "
    "user directly (same as search_codex/query results) - finish() just needs a short closing line, the table IS "
    "the answer.\n"
    "- compare(args={\"items\":[\"<English item name>\", \"<English item name>\", ...],\"quality\":\"<optional, "
    "same forms as assess>\"}): side-by-side stat comparison of 2-6 items at their FULLY ASSESSED stats (default "
    "quality 200%/level 13 if not given - effectively \"fully forged\", since a comparison is normally about a "
    "build's ceiling), diffed against the first item in the list. Use this for \"which is better, X or Y\" - never "
    "open_entry both and compare by eye, the raw codex numbers aren't upgrade-projected and aren't a fair "
    "comparison. POSTS the table directly - finish() just needs a short closing line. ITEMS only - to compare "
    "two classes or specializations use estimate_stats (per character), not this.\n"
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
    "- estimate_stats(args={\"items\":[{\"name\":\"<item>\",\"quality\":\"<quality name or %>\","
    "\"level\":<upgrade level 1-13>}, ...],"
    "\"class\":\"<the TIER-10 CLASS, one of: " + _class_names_text("specialization") + ">\","
    "\"specialization\":\"<the SPECIALIZATION, one of: " + _class_names_text("class") + ">\","
    "\"ascension_level\":<the player's AL, any number>,\"pvp\":true|false,"
    "\"amities\":{\"<bonus>\":\"<value>\"}}): a character stat estimate. It computes BASE stats (from the "
    "tier-10 CLASS + AL, plus the SPECIALIZATION's percent modifiers) and ITEM stats (from worn gear) SEPARATELY "
    "and "
    "shows each as its own block plus a combined total. So there are TWO ways to use it: (a) BASE stats only - "
    "the player asks \"what are my/a Gilgamesh's base stats at AL 100\" and gives just the class + AL "
    "(specialization optional); pass NO items. (b) FULL loadout - also pass the items to add gear on top. Use it for "
    "\"which stats will I have\"/\"порахуй мої стати\"/\"базові стати\" questions. POSTS the table(s) - finish() "
    "just needs a short closing line. REQUIRED: ascension_level, AND at least one of (a tier-10 CLASS -> base "
    "stats, or items -> gear stats). OPTIONAL, so NEVER demand them: `items` (omit for a base-only estimate), "
    "`pvp` (defaults to PVE - don't ask; the reply states the assumption), and `specialization` (its modifiers "
    "are applied only if given). The tool REFUSES a call it can't compute anything from and hands back exactly "
    "what's missing. Item quality defaults to 100% and level to 1. QUALITY AND LEVEL ARE TWO DIFFERENT THINGS: "
    "quality is the % roll (100%, 185%, or a tier name like legendary), level is how far it is upgraded - 1 to "
    "10, then 11 masterforged, 12 demonforged, 13 godforged. \"godforged\" therefore means level 13, NOT a "
    "quality; an item can be 185% quality AND level 10. class=\"none\" is a valid ANSWER for a player "
    "who has no tier-10 class yet (then items are required, since there's no base to compute). NEVER guess a "
    "class, specialization, AL, or a loadout - a guessed input comes back as a confident WRONG number (live failures: "
    "a total built from three items the user never mentioned; a lone class that rendered an empty table). Ask "
    "instead. "
    "Pass the item names STRAIGHT THROUGH, exactly as the user wrote them - this tool resolves them "
    "itself and reports any it cannot, so do NOT search_codex them first (live failure: eight searches "
    "in a row, then it ran out of patience before ever calling this). An item's quality and level are "
    "PER-ITEM and OPTIONAL - never ask for them. \"Godforged Fallen Sky Shoes 195%\" already says "
    "level 13 and quality 195%, and \"Celestial Staff 20lvl\" already says level 20; hand the whole "
    "phrase over as the name and it is read out for you. "
    "The values can come from EARLIER TOOL RESULTS as well as from the user: if they name a BUILD rather "
    "than items (\"the omniflask raid build\", \"my Gilgamesh set\"), call class_guide or knowledge_search "
    "FIRST and pass the item names it lists. The two lists above are the ONLY valid class/specialization "
    "values: never translate one, never invent one, and never offer a name outside them as an ask() option - "
    "live failure 2026-09-25, a class question rendered the buttons \"Дудар\", \"Орdinator\" and "
    "\"Гільгармос\", none of which exist, so tapping one contributed nothing. \"Heretic Ara Sequencer\" is a "
    "CLASS plus a SPECIALIZATION: class=\"Heretic Ara\", specialization=\"Sequencer\" - two different levels "
    "of one character (see the TAXONOMY rule). Each is looked up in its own pool, so a name in the wrong field "
    "is still understood, but say them the right way round in the answer.\n"
    "- towers(): no input. Current floor (15-50, 50=cleared/at the top awaiting reset) of all 5 real-time \"Wild "
    "Towers of Olympia\" (Selene/Eos/Oceanus/Themis/Prometheus) - pure deterministic math from the current time, "
    "always available, never a dead end. Use for \"how tall is tower X now\"/\"which tower is at max\" etc. For "
    "GEAR/REWARDS/mechanics ABOUT the towers (not their live height), use class_guide(topic=\"towers\") instead - "
    "these are two different things sharing a name. THIS TOOL IS AUTHORITATIVE for any tower floor, cycle or "
    "reset timing: it is a verified port of the game's own formula, so prefer it over any guide prose a "
    "knowledge_search returns (one such guide described the 35-day cycle as a weekly reset). POSTS the result "
    "AND a \"🔔 remind me at floor 50\" button per tower not already there - the observation gives the EXACT "
    "ETA/hours for each: quote that number verbatim in finish(), never recompute a days-remaining estimate from "
    "the floor count yourself (live bug: with selene at floor 36, the model assumed 1 floor/day and answered "
    "\"14 days\" instead of reading the tool's own ~47h/~2-day figure - towers really gain ~6 floors/day). "
    "finish() just needs a short closing line naming the tool's own number.\n"
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
    "- monuments(action_input=<a thing, a category, or empty>, args={\"monument\": \"<optional: ithra|thor|"
    "vulcan|demeter>\", \"floor\": <optional int>}): THIS WEEK's rewards at the four Monuments (Ithra, Thor, "
    "Vulcan, Demeter) - which monument and FLOOR gives what. The answer to \"what's in the monuments\" (leave "
    "action_input EMPTY for the whole chart), \"where can I get <material/potion> this week\", \"which monument "
    "gives proofs/orns/keys/arena tokens\", \"what does Thor floor 5 give\". Pass the specific name "
    "(\"adamantine\", \"perfect runestone\", \"nostrum\") or a category (\"materials\", \"rare materials\", "
    "\"potions\", \"items\" for gear, \"proofs\", \"orns\", \"skeleton keys\", \"arena tokens\", "
    "\"monster remains\"). Leave it empty ONLY when the user asks for everything - a question about one kind "
    "of reward (\"what materials are in the monuments\") passes that category, so the user gets that list, not "
    "the whole chart. For the whole chart, a category, or any result longer than three floors, the tool POSTS "
    "the list to the chat itself - see finish() for what to send after it. It rotates WEEKLY - always say which week, and if it reports STALE, say the new week's chart is not "
    "out yet. Use it instead of knowledge_search or web_search for anything about monument rewards.\n"
    "- knowledge_search(action_input=<search term>): a curated community reference - player-maintained sheets, "
    "AMITY and CRUCIBLE tables (the gear-bonus affixes: their tiers, roll ranges and which equipment slots each "
    "can appear on), CLASS and SPECIALIZATION data (each one's stat modifiers, bonus stats and passive "
    "effects, plus tier-10 base stats - use it for \"what does class X give\" and stat-estimate questions; "
    "Ascension Level is +1%/level on every stat and PVP doubles HP) - none of that is in any codex page, so "
    "this tool is the only way to answer those, "
    "PLUS a community-verified (2026) reference on how each core SYSTEM works (factions, Ascension, item "
    "quality/forging, adornment slots, Wild Towers, flasks, kingdoms, followers) - use it for \"how does X "
    "work\" conceptual questions, not just item lookups, "
    "PLUS what Orna's own developers (u/OrnaOdie, u/Widogeist) have explained on reddit, which is where hidden "
    "mechanics, exact formulas and \"why it actually works like that\" answers live. A DEVELOPER COMMENTS block "
    "in the result outranks the sheets above it, but can be years old - check releases() before quoting a number "
    "from one that matters. Covers "
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
    "- releases(action_input=<item/class/mechanic name, English - or empty for the latest notes>): playorna's "
    "OWN patch notes (the last few months). The codex and the community knowledge base both describe what IS "
    "and never mention what CHANGED, and the knowledge base is hand-maintained so it can lag a balance patch by "
    "weeks. Use this whenever the answer depends on CURRENT balance - gear/stat recommendations, \"is X still "
    "good\", a build question, or any number out of knowledge_search that the user will act on - and mention any "
    "relevant change in finish(). A note here OVERRIDES the knowledge base, which is fan-maintained; the codex "
    "itself is official and already current, so this mainly qualifies knowledge_search and class_guide answers. "
    "Finding nothing is not proof nothing changed - the notes only go back a few months.\n"
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
    "- ask(action_input=<question>, options=[2-4 short choices]): a clarifying question. Give only REAL choices - "
    "an \"Інше\"/\"Other\" escape option is added automatically for you, so never include one yourself (a "
    "duplicate just wastes a button). Only when a specific missing detail would materially "
    'change the results and there\'s no reasonable default (e.g. "good gear for my class" names no class, or a '
    'name/search matches several unrelated things and it genuinely matters which). Most requests do NOT need '
    "this. Never ask twice in the same conversation.\n"
    "- finish(action_input=<short closing text, or EMPTY>): end the turn. When a tool POSTED a result that "
    "fully answers the question (monuments/today/next/towers say so), finish with an EMPTY action_input - "
    "nothing more is sent. Results a TOOL already showed the user (search "
    "hits, reports, event cards) don't need repeating - finish is just a short closing sentence (e.g. \"Ось "
    'варіанти для обох слотів."), or, for the two fixed-reply cases below, the exact fixed text. This INCLUDES '
    "codex item stats: when you looked at 1-2 codex entries (open_entry/search_codex/query), the full entry card "
    "- stats, effects, Dropped by/Gives/Upgrade materials sections - is posted to the chat AUTOMATICALLY, above "
    "your answer, as soon as you finish. Do NOT restate those numbers/stats/effects yourself - that duplicates "
    "what the user is about to see twice. Just answer the actual question asked (e.g. which one is better, "
    "whether it has a given effect) in one short sentence; name the item so it's clear which card it refers to. "
    "An answer built from knowledge_search or web_search is different: nothing was shown to the user yet, so "
    "finish() IS the answer - write it out properly, in the user's own language, from what came back. Don't call "
    "finish before you have enough information.\n"
)

_CONDITION_RULES = (
    "query's args: {\"conditions\": [<condition>, ...], \"combinator\": \"and\"|\"or\", \"category\": \"<one of "
    'items, monsters, bosses, raids, followers, classes, spells, buildings, dungeons, or empty for all>", '
    '"sort_by": "<stat field or empty>", "sort_dir": "asc"|"desc", "count": true|false, '
    '"group_by": "<field or empty>"}. '
    'For "HOW MANY ...?" / "what is the TOTAL/BREAKDOWN of ...?" set "count": true (and "group_by" for a '
    'per-value split, e.g. group_by "category" for what the whole codex holds, or "tier"/"rarity"/"item_type"/'
    '"place"/"useable_by"): that returns EXACT full-database totals. NEVER count a listing yourself - a plain '
    'query returns at most the first 50 rows, so counting what you see gives the cap (50), not the real number. '
    'A count needs no conditions at all ("how many items are in the codex" = count with category "items"). '
    "ONE query call = one filter - if the request "
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
    '  {"kind":"bond_bonus","field":"<a follower bestial-bond passive: orn_bonus/exp_bonus/gold_bonus/luck_bonus/'
    'ward_start/crit_chance/...>","cmp":">","value":<number, OPTIONAL>} - a FOLLOWER\'s bestial_bond BONUS: the '
    "passive %-stat it grants when bonded. This is the THIRD bond encoding, distinct from kind:\"ability\" (its "
    "bond SPELL grants) and kind:\"effect\" (its BOND status procs). Use it for \"which follower gives orn bonus\" "
    '(omit value for a presence check) or "which follower gives orn bonus over 30" (cmp/value threshold). Only '
    "followers have these.\n"
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
    "immune to every element except one - confidently wrong instead of checking. "
    "PREFER research(<boss name>) here: its one call carries both the boss's codex facts AND the community "
    "knowledge half (the Monster-Data immunities), so it satisfies this rule without a separate knowledge_search "
    "plus N open_entry calls."
)

# Below this, finish() must SAY it does not know rather than answer flatly
# (explicit ask 2026-09-26). The number is the model's own, which is weakly
# calibrated on its own - so it is CLAMPED by what the loop actually verified,
# see _evidence_ceiling. A self-reported 95% on zero tool calls is exactly the
# failure this exists to stop: three of six answers in the blind Reddit test
# invented a cause rather than admitting ignorance (see CLAUDE.md).
_CONFIDENCE_FLOOR = 75

_CONFIDENCE_RULE = (
    "EVERY finish() MUST carry a \"confidence\" number, 0-100: how sure you are that the answer is CORRECT, "
    "not how sure you are that you followed the steps. Judge it on the evidence you actually have:\n"
    "- 90-100: every claim came from a tool observation in this conversation.\n"
    "- 75-89: the substance came from observations, with a small gap you have stated as an assumption.\n"
    "- BELOW 75: you are guessing, the tools came back empty, the sources disagree, or part of the question is "
    "unanswered. Then SAY SO in the answer itself - lead with the fact that you do not know reliably, say which "
    "part is unverified and what would settle it (a tool that failed, a source that has no such field, an input "
    "the user has not given). Do NOT dress a guess up as an answer.\n"
    f"An answer below {_CONFIDENCE_FLOOR} is shown to the user with an explicit \"I don't know this reliably\" "
    "banner, so an inflated number does not help you - and the number is CLAMPED by what the loop actually "
    "verified: if you called no tool, or every tool came back empty, your confidence is capped no matter what you "
    "claim. \"I could not find this\" is a GOOD answer here; an invented mechanism is the worst one. Three of six "
    "answers in a live review stated a fabricated cause instead of admitting ignorance, which is what this rule "
    "exists to stop."
)

_REASONING_RULE = (
    "REASON TWICE - ONCE BEFORE THE TOOLS, ONCE BEFORE finish(). This is mandatory, and the place to do it is the "
    "\"thought\" field.\n"
    "1. BEFORE your first tool call, work out what the user actually WANTS and write it in \"thought\": the goal, "
    "and then EVERY explicit constraint they stated, listed one by one - slots, quality, upgrade level, class, "
    "specialization, Ascension Level, PVE/PVP, quantities, a game mode, a language. Constraints are the things you "
    "will be judged on, and they are easy to read past when the request is one long sentence. Then plan which tools "
    "answer it. A request that names several things at once is several tool calls, not one.\n"
    "2. BEFORE finish(), reason again over what the tools actually returned: walk your constraint list and check "
    "each one is satisfied by an OBSERVATION, not by your own assumption; check the numbers you are about to state "
    "came back from a tool rather than from memory; check nothing a tool warned about was dropped (a refusal, a "
    "PARTIAL list, a conditional passive, an assumption you had to make). If a constraint is unmet, fix it with "
    "another tool call instead of writing it up as if it were met. If it cannot be met, SAY so in the answer. ALSO "
    "check: if this is a 1-2 item codex lookup, its full stat card already posts automatically - your finish() "
    "text must NOT re-list its stats/effects/tags, only answer the actual question in one short sentence.\n"
    "GAME-RULE SANITY, because a stat table can be arithmetically perfect and still describe a character nobody can "
    "build: equipment must be legal (one head/torso/legs, TWO accessory slots, and two hands - so either one "
    "TWO-HANDED weapon alone, or two one-handed weapons, never a two-hander plus an off-hand); two one-handed "
    "weapons DUAL-WIELD at 65% of their combined stats, which can still beat a two-hander, so do not assume the "
    "two-hander wins; only ONE CELESTIAL weapon can be equipped at a time, so a \"best in every slot\" pick can "
    "NEVER be two celestials even though celestials top most stat rankings - pair one celestial with the best "
    "non-celestial; and class/specialization PASSIVES are conditional bonuses a stat total does not include "
    "(Sequencer's Doublecast and Weapon Power both require DUAL STAVES) - name the condition and say whether the "
    "loadout meets it. estimate_stats enforces the legality part and will refuse an impossible loadout: treat that "
    "refusal as a real finding and re-pick, never as a reason to state the numbers anyway."
)

_COMPLETENESS_RULE = (
    "MANDATORY RULE - NEVER GENERALISE FROM A SAMPLE. If your answer would make a claim about a WHOLE group - "
    "\"all/none/only/every/no X\", \"the set is for these classes\", \"there is nothing that...\", a count, a "
    "\"the best/strongest/cheapest X\" superlative - then you must have observed EVERY member of that group "
    "first. Whatever you did NOT observe is unknown, never \"absent\". Concretely:\n"
    "- An observation that says (+N MORE not listed) or PARTIAL means you have NOT seen the group. The count in an "
    "observation (\"13 results\") is the size of the group; the names listed may be fewer.\n"
    "- Opening entries one at a time to check an attribute does NOT become complete just because you opened "
    "several. Five of thirteen is a sample.\n"
    "- Prefer ONE tool call that returns the complete filtered set over N calls that each return one member: "
    "query() with a text condition on the name plus an attr/stat condition for the attribute answers "
    "\"which of set X have property Y\" exhaustively, in one step, and an empty result IS the answer \"none do\". "
    "A query that returns 0 is evidence; an open_entry you never made is not.\n"
    "- If you genuinely cannot cover the whole group (too many members, a tool that cannot filter on it), then do "
    "NOT state a universal. Say which members your answer is based on and that the rest were not checked.\n"
    "Keep reasoning until the claim you are about to make is actually supported - spending more steps is correct "
    "here; a confident universal from a partial look is the one outcome to avoid. Live-verified failure: asked "
    "which items of a 13-item set were useable by mages, the loop opened 5, and answered that the set was "
    "\"warrior or thief classes only\" - it had never seen the 4 valhallan_summoner pieces, and one filtered "
    "query() would have settled it in a single step."
)

_TAXONOMY_RULE = (
    "CLASS vs SPECIALIZATION vs CLASS LINE - three different levels, never one word for two of them:\n"
    "- a CLASS LINE is one of exactly six: Mage, Thief, Warrior, Valhallan, Summoner, Demigod. A character "
    "belongs to one for its whole life. A line holds a class at EVERY tier, 1 through 10 - it is NOT a single "
    "class, and its tier-10 class is only its last rung, so \"the Mage line\" is not a synonym for Heretic. "
    "GEAR RESTRICTIONS key on the LINE: that is what an item's useable_by (all_classes / magic_users / "
    "warrior_classes / thief_classes / valhallan_summoner_classes / melee_classes) means, and a \"which classes "
    "is this for\" answer comes from that field, never from what a name sounds like.\n"
    "- WHICH LINE a given class belongs to is NOT in any structured data here - a codex class record has its "
    "tier but no line, and a class description's \"can wield equipment of the thief\" is cross-line EQUIPMENT "
    "ACCESS, not membership (Heretic Corvus is a Mage-line class described as \"thief\"). So do not assert a "
    "class's line from memory: get it from knowledge_search (the tier-by-tier progression guides), or leave it "
    "out and answer what you can verify.\n"
    "- a CLASS is one tier 1-10 step inside a line (tier 1 is Mage/Thief/Warrior; the tier-10 classes are "
    + _class_names_text("specialization") + " - six line-ending classes plus two Celestial variants each). "
    "Several have a gendered second name: Heretic/Hera, Gilgamesh/Gallia, Beowulf/Bestla.\n"
    "- a SPECIALIZATION is the one passive package a class picks on top ("
    + _class_names_text("class") + "). Ranger, Berserker and Sequencer are SPECIALIZATIONS, "
    "NOT classes. A character is a class AND a specialization together (\"Heretic Ara Sequencer\"), which is "
    "what makes the combination unique.\n"
    "THIS RULE OUTRANKS A SOURCE'S WORDING. Community guides, aussiescodex and old reddit/Q&A posts routinely "
    "call the tier-10 CLASSES \"specializations\" - if a knowledge_search or class_guide block says \"the six "
    "tier-10 specializations are Gilgamesh, Heretic, ...\", it means CLASSES; use the vocabulary above and do not "
    "repeat theirs. Live failure 2026-09-27: a run read exactly that line back and answered \"Heretic is a "
    "tier-10 specialization\".\n"
    "So: never call Ranger/Sequencer/Berserker a class, never call Gilgamesh/Heretic a specialization, and never "
    "put a specialization and a class in one list as if they were the same kind of thing. When recommending gear "
    "\"for a class\", say which CLASS LINE (or which useable_by bucket) it is restricted to, and take that from "
    "the item's own useable_by - never from your own idea of what a name sounds like. Live failure 2026-09-27: an "
    "answer listed \"Ranger\", \"Summoner\" and \"Dexterity-based classes (Ranger, Assassin, Tamer)\" side by "
    "side as classes - Summoner is a real class, the other three are specializations, and the gear split it was "
    "describing is really by class line."
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
    "than two numbers in your own head, write the calculate() expression out.\n"
    "NAMED-ITEM BONUS TOTALS - a DIFFERENT shape, don't confuse it with the two above (e.g. \"total orn bonus "
    "from godforged lost helmet, godforged court jester outfit, legendary band of gods\"): the user already "
    "named the items AND each one's quality, so build_optimize (which PICKS the items for you) does not fit. "
    "Call assess(args={\"item\":\"<name>\",\"quality\":\"<the quality given FOR THAT item>\"}) once per named "
    "item - assess's observation hands back that item's QUALITY-SCALED bonus as \"[orn_bonus=57.5%]\", and THAT "
    "is the number to total. An item's codex page value (open_entry's \"Orn Bonus: +5%\") is the UNSCALED base "
    "and is simply wrong for a godforged/legendary/ornate item - never total those. Then calculate() the stack: "
    "(1 + b1/100) * (1 + b2/100) * ... written out as one expression. A flat bonus the user states themselves "
    "(\"+25% за кроки\", \"+25% from world event\") is just another (1 + 25/100) factor - no lookup needed. "
    "Anything else named that is not a codex item (Shrine of Luck, Temple of Wealth, Lucky Silver Coin, ...) is "
    "a knowledge_search lookup - those community tables give a MULTIPLIER (e.g. \"2\" = x2 = +100%), already in "
    "the same stacking form.\n"
    "DO NOT finish a named-item total until you have an assess OBSERVATION for EVERY item the user listed. Count "
    "them: if they named six items you need six assess observations (an item listed twice - the same weapon in "
    "hand and off-hand - is assessed once and counted twice in the calculate expression). Never state an item's "
    "bonus from your own knowledge of the game, never invent a per-item number, and never change how many of an "
    "item the user said they have. Live failure: after ONE assess call the answer claimed \"4 godforged helmets, "
    "4 godforged outfits\" with made-up per-item percentages, none of which was in the request or the data.\n"
    "STACKING CONVENTION - applies to EVERY case above, and both halves get got wrong live: (a) these bonuses "
    "stack MULTIPLICATIVELY, never additively - four +57.5% items are not \"+230%\"; (b) the product you get out "
    "of calculate() is a MULTIPLIER, not a percentage - the bonus percentage is (product - 1) * 100, so a product "
    "of 21.76 means x21.76, i.e. +2076%, NOT \"21.76%\". This is exactly what build_optimize computes and "
    "reports, so your answer must match how it would phrase the same total. State both forms (xN and +N%) in "
    "finish() so the number can't be misread."
)


# Observation markers meaning a tool produced NO usable evidence. Kept as
# substrings of the real observation strings the tools return, so a tool that
# starts refusing differently will fail OPEN (evidence assumed good) rather than
# silently capping every answer - a false cap is worse than a missed one here.
_DEAD_END_MARKERS = (
    "0 results", "0 matches", "no matches", "did not run", "did NOT run",
    "no codex entry found", "no knowledge-base matches", "couldn't open",
    "couldn't parse", "lookup failed", "query failed", "isn't assessable",
    "computed NOTHING", "nothing found", "no further tool calls",
)


# "How many / what is the total / breakdown" phrasings, EN + UK. Deliberately
# narrow: this gates a forced retry, so it must not fire on a pleasantry or a
# capability question ("what can you do"), which legitimately need no tool.
_AGGREGATION_RE = re.compile(
    r"\b(how many|how much|what('s| is) the (total|average|sum|count|breakdown)|"
    r"total number|count of|average |median |most common|least common|"
    r"скільки|яка (загальна|середня)|загальна кількість|у середньому|найпоширеніш)",
    re.IGNORECASE)


def _looks_aggregation_request(text: str) -> bool:
    """True for a question whose answer is a NUMBER derived from the whole
    database. These are exactly the questions the model will answer from
    memory if left alone - it reads as a simple factual question, so there is
    no obvious reason to call a tool, and a plausible wrong number comes back
    looking identical to a right one."""
    return bool(_AGGREGATION_RE.search(text or ""))


# What a real aggregate observation looks like. `query`'s count path opens with
# "COUNT for", and a sql() aggregate echoes its own statement, so the function
# name appears. Matched case-insensitively against every observation.
_AGG_EVIDENCE_MARKERS = ("count for", "count(", "sum(", "avg(", "min(", "max(", "group by")


def _has_aggregate_evidence(session) -> bool:
    return any(any(m in str(obs or "").lower() for m in _AGG_EVIDENCE_MARKERS)
               for obs in session.seen_calls.values())


def _counted_a_capped_list(session) -> bool:
    """True when the only thing the loop has to count from is a TRUNCATED
    listing. That is the live failure of 2026-10-04 exactly: "how many items
    are in the codex" was refused on an empty-conditions query, so the model
    added sort_by=attack to make the call legal, got back a 50-row capped
    list, and answered "there are 50 items in the codex" with confidence 82.
    A listing's row count is the CAP, never a total."""
    return (not _has_aggregate_evidence(session)
            and any("partial" in str(obs or "").lower() for obs in session.seen_calls.values()))


def _forced_evidence_note(session) -> Optional[str]:
    """The note to send a finish back with, or None to let it through. Fires
    ONCE per request, for an aggregation question that either made NO tool
    call at all or has nothing to count from but a CAPPED listing.

    Why this is code and not another prompt rule: verified live on this exact
    change. With sql() fully described in the prompt, "how many items are in
    the codex?" finished with a confident "2773" and an EMPTY action trace
    3/3 - the number was right, which is the dangerous part: it came from the
    model's memory of a public website, not from the database, and a stale or
    invented one would have looked exactly as convincing. CLAUDE.md's own
    history says a tool description alone does not get a new tool called
    (class_guide was 0/3 before its rule); this file's other finding says a
    prompt rule is a reduction, not a guarantee. So the guarantee lives here."""
    if session.pushed_for_evidence:
        return None
    if not _looks_aggregation_request(session.original_request):
        return None

    if not session.seen_calls:
        why = ("without having called a single tool, so that number can only have come from memory - it is a "
               "guess, and a wrong one would look identical to a right one")
    elif _counted_a_capped_list(session):
        # The narrow, verified case - do NOT widen this to "any aggregation
        # question without an aggregate", or every "how much defense does X
        # have" would burn a step: those answer from a complete observation,
        # and only a PARTIAL one is evidence that something was cut.
        why = ("counting the rows of a result that is explicitly marked PARTIAL. That number is the display "
               "CAP (50), not the total - the observation states the real total separately")
    else:
        return None

    session.pushed_for_evidence = True
    return (f"You are about to answer a HOW-MANY/total/breakdown question {why}. Get the real number: call "
            "query with args {\"count\": true, ...} (add \"group_by\" for a per-value split), or sql() with a "
            "real SELECT count(*). Send sql action_input=\"schema\" first if you need the exact table and "
            "column names. Never report the length of a capped list as a total.")


# Tools whose posted output IS the whole answer to a "show me" question: the
# monuments chart, the guild-shop schedule (today/next), the tower floors.
_LISTING_TOOLS = {"monuments", "today", "next", "towers"}

_POSTED_NOTE = "This result was POSTED to the chat. If it fully answers the user's question, call finish with an EMPTY action_input - nothing more is sent. Otherwise finish with only what it does not show (the takeaway or judgment asked for) and never re-list it."


class _PostRecorder:
    """Wraps the Telegram message a tool posts through, remembering the last
    message it sent. Everything else passes straight through. Inline mode's
    sink returns nothing from reply_text, so there nothing is recorded and the
    normal closing line is kept - inline is a single message anyway."""

    def __init__(self, message):
        self._message = message
        self.last = None

    async def reply_text(self, *args, **kwargs):
        sent = await self._message.reply_text(*args, **kwargs)
        if sent is not None:
            self.last = sent
        return sent

    def __getattr__(self, name):
        return getattr(self._message, name)


async def _run_listing(session, message, run):
    """Run a listing tool through a _PostRecorder; when it actually posted,
    note it on the session so finish() knows the card already answered."""
    rec = _PostRecorder(message)
    observation = await run(rec)
    if session is not None and rec.last is not None and _POSTED_NOTE not in observation:
        observation += "\n" + _POSTED_NOTE
    if session is not None and rec.last is not None:
        session.posted_note = session.posted_note or \
            "the tool's full result was already posted to the chat above the answer"
        session.last_post = rec.last
    return observation


def _listing_only(session, action_input: str) -> bool:
    """The model finished with NO text after a tool posted its card: it judged
    the card the whole answer, so no closing line is sent. The model decides -
    not a regex over the question (tried 2026-10-05: "які матеріали" is not a
    "judgment", so the real materials answer was dropped)."""
    return bool(session.posted_note and session.last_post is not None and not (action_input or "").strip())


def _leaked_observation(session, answer: str) -> bool:
    """True when the draft answer contains a tool observation verbatim.
    Observations are INTERNAL - they carry instructions to the model ("never
    state an amount"), not text for a player. Live 2026-10-05, told by REVIEW to
    "list what drops", the model pasted the monuments observation, instructions
    and all, as its whole reply. Matched on an observation's opening line, long
    enough that an ordinary quoted value cannot trigger it."""
    text = answer or ""
    for obs in session.seen_calls.values():
        head = str(obs or "").strip().split("\n", 1)[0][:60]
        if len(head) >= 40 and head in text:
            return True
    return False


def _evidence_ceiling(session) -> tuple:
    """(ceiling, why) - the highest confidence the EVIDENCE supports, whatever
    the model claims.

    The model cannot be the only judge of its own certainty: the loop already
    knows whether any tool returned anything. No call at all means nothing was
    verified; every call dead-ending means it was verified and came back empty.
    Either way a confident answer can only have come from memory."""
    observations = [str(v or "") for v in session.seen_calls.values()]
    if not observations:
        return 40, "no tool was called, so nothing in this answer was verified"
    lowered = [o.lower() for o in observations]
    useful = [o for o in lowered
              if not any(m.lower() in o for m in _DEAD_END_MARKERS)]
    if not useful:
        return 60, "every tool call came back empty or refused"
    return 100, ""


def _parse_confidence(step: dict) -> Optional[int]:
    """The model's own confidence out of finish()'s args, 0-100, or None.

    Accepts it at the top level or nested in args (the same wrong-placement
    drift _advance_inner already tolerates for action_input), and a 0-1 float
    as well as a percentage, because both get written."""
    raw = None
    for holder in (step, step.get("args") if isinstance(step.get("args"), dict) else None):
        if not isinstance(holder, dict):
            continue
        for key in ("confidence", "confidence_pct", "certainty"):
            if holder.get(key) is not None:
                raw = holder[key]
                break
        if raw is not None:
            break
    if raw is None:
        return None
    try:
        value = float(str(raw).strip().rstrip("%"))
    except (TypeError, ValueError):
        return None
    if 0.0 < value <= 1.0:
        value *= 100                    # "0.9" means 90%, not 1%
    return max(0, min(100, int(round(value))))


def _confidence_gate(session, step: dict, answer: str) -> tuple:
    """-> (answer_to_send, effective_confidence).

    The whole confidence decision in one pure-ish place so it can be tested
    without driving the model: read the claim, clamp it by the evidence, and
    below the floor prepend the admission. Extracted from the finish branch
    because inline it was untestable - there is no way to inject a step into
    _advance_inner."""
    claimed = _parse_confidence(step)
    # Explaining the bot's own commands needs no tool - the fixed reference in
    # the prompt IS the source (often translated, so match its command names,
    # not its wording), and "no tool was called" must not cap it.
    if not session.seen_calls and any(
            re.search(re.escape(c) + r"\b", answer)
            for c in set(re.findall(r"/[a-z_]+", _capabilities_text()))):
        return answer, max(claimed or 0, 90)
    ceiling, why = _evidence_ceiling(session)
    effective = min(claimed, ceiling) if claimed is not None else ceiling
    if effective >= _CONFIDENCE_FLOOR:
        return answer, effective
    user_text = next((m.get("content", "") for m in getattr(session, "messages", [])
                      if isinstance(m, dict) and m.get("role") == "user"), "")
    ukrainian = bool(_CYRILLIC_RE.search(user_text or ""))
    reason = why or ("модель не впевнена" if ukrainian else "the model itself was unsure")
    logger.info("orna: low confidence %s (claimed=%s ceiling=%s) - %s",
                effective, claimed, ceiling, why or "model-reported")
    return _low_confidence_banner(effective, reason, ukrainian) + "\n\n" + answer, effective


def _low_confidence_banner(pct: Optional[int], why: str, ukrainian: bool) -> str:
    """The "I don't know" lead-in. The partial answer is kept BELOW it rather
    than discarded: the user asked for the bot to say it does not know, and a
    clearly-labelled partial beats a blank refusal - but the label has to come
    first so it cannot be skim-read past.

    MARKDOWN, not HTML - this is the one thing to keep right here. The finish
    answer is sent through _reply_markdown, which runs telegram_go.
    _markdown_to_html and therefore ESCAPES <, > and & before Telegram sees
    them. A `<b>` written here arrives as `&lt;b&gt;` and renders as the
    literal text "<b>" (live report 2026-10-05: the user saw
    "<b>I don't know this reliably</b>" in the chat). `**bold**` is what this
    pipeline turns into real bold."""
    shown = f"~{pct}%" if pct is not None else "низька" if ukrainian else "low"
    if ukrainian:
        tail = f" Причина: {why}." if why else ""
        return (f"⚠️ **Не можу відповісти впевнено** (впевненість {shown}, "
                f"поріг {_CONFIDENCE_FLOOR}%).{tail} Нижче — лише те, що вдалося зібрати; "
                "це не перевірена відповідь.")
    tail = f" Reason: {why}." if why else ""
    return (f"⚠️ **I don't know this reliably** (confidence {shown}, bar is "
            f"{_CONFIDENCE_FLOOR}%).{tail} Below is only what I could gather - treat it as "
            "unverified.")


_CYRILLIC_RE = re.compile(r"[Ѐ-ӿ]")


# ENGLISH-FIRST PIPELINE (design ask 2026-09-26). The loop used to reason in
# whatever language the user wrote, enforced by a per-request lock. Every one of
# its knowledge sources is ENGLISH (codex, aussies, the sheets, the guides, the
# dev corpus, the Q&A threads), and so is every rule in this prompt, so a
# Ukrainian request meant the model reasoned across a language boundary on every
# step and read English evidence to write Ukrainian thoughts. Now: translate IN
# at the edge, reason and finish in English, translate OUT at the edge. The user
# still only ever sees their own language.
#
# Costs two extra model calls on a non-English request (one per gate), which is
# why both degrade to the untranslated text on any failure rather than erroring -
# a slightly-wrong-language answer beats no answer.
_LOOP_LANGUAGE = "English"


def _detect_lang(text: str) -> str:
    """"Ukrainian" | "English". This guild writes only those two, the same
    assumption the old per-request language lock already made."""
    return "Ukrainian" if text and _CYRILLIC_RE.search(text) else "English"


# Words that start a sentence or are simply capitalised in English prose - not
# identifiers, so they must not be pinned or the "keep these exact" list becomes
# noise the model ignores.
_NOT_A_NAME = frozenset("""
the a an and or but if then this that these those you your i it is are was were be been
for with from into onto about over under per each every all any none no not only also
what which when where why how who whom whose there here they them their our we us
in on at to of as by so than that's don't can cannot could should would may might must
base total stats stat level tier quality rarity class classes item items weapon weapons
yes maybe note reason answer question example see use using used get gets got give gives
""".split())

_NAME_RE = re.compile(r"\b[A-Z][A-Za-z0-9'’\-]*(?:\s+(?:of|the|de)?\s*[A-Z][A-Za-z0-9'’\-]*)*")


def _proper_nouns(text: str) -> list:
    """Candidate IDENTIFIERS in English text - item/class/spell names and the
    like - longest first.

    A general "never translate a proper noun" instruction is not enough: live
    2026-09-26 the very first Ukrainian answer rendered the class `Duelist` as
    "Дулїст", which resolves to nothing in the data and is exactly the failure
    the old language lock also existed for. Enumerating the names explicitly in
    the prompt, and then CHECKING they survived, is far stronger than a rule."""
    found = []
    for m in _NAME_RE.finditer(text or ""):
        words = m.group(0).split()
        # Strip common words from the EDGES only. Dropping interior ones broke
        # "Altar of Ascension" into "Altar Ascension", which then appears
        # nowhere in the source - so the survived-check could never pass and
        # every translation burned a pointless retry.
        while words and words[0].lower() in _NOT_A_NAME:
            words.pop(0)
        while words and words[-1].lower() in _NOT_A_NAME:
            words.pop()
        cleaned = " ".join(words)
        if len(cleaned) < 3 or cleaned.lower() in _NOT_A_NAME:
            continue
        # Must be verbatim in the source, or "keep this exactly" is unsatisfiable.
        if cleaned not in (text or "") or cleaned in found:
            continue
        found.append(cleaned)
    return sorted(found, key=len, reverse=True)


# Letters Russian has and Ukrainian does not. Their presence means the model
# slid into Russian mid-sentence - live 2026-10-05: "экіпіровку" for
# "спорядження". For a Ukrainian guild that is not a style nit.
# Russian letters can't hide, but Russian WORDS spelled with letters the two
# languages share can - and they were the more common slip live: "время",
# "начали", "затем". These are high-frequency Russian words that are NOT valid
# Ukrainian (so "все", "завтра", "так" are deliberately absent - they are).
# The model for every TRANSLATION the bot makes (the /orna input and output
# gates, announcements). Measured 2026-10-05, same announcement, 3 runs each,
# all confirmed served by the cloud: nemotron-3-super (the loop's model)
# produced non-words and rendered "redeem code" as "код для викупу" (RANSOM
# code); gemma4:31b gave natural, near-identical Ukrainian every time. The
# reasoning loop keeps its own model - this is only translation.
TRANSLATION_MODEL = "gemma4:31b"
# Scripts that have no business in a Ukrainian/English reply - live: "Demeter"
# came back as "Де미터", Hangul mid-word.
_FOREIGN_SCRIPT_RE = re.compile(r"[\u1100-\u11FF\u3040-\u30FF\u3130-\u318F\u4E00-\u9FFF\uAC00-\uD7AF]")
_RUSSIAN_ONLY_RE = re.compile(
    r"[ыэъёЫЭЪЁ]|\b(?:время|начали|начать|затем|потом|сейчас|только|нужно|также|если|когда|чтобы|что|"
    r"или|еще|это|очень|сегодня|здесь|теперь|потому|который|которые|будет|нет)\b", re.I)


async def _translate(text: str, target: str, source: str = "", pin: Optional[list] = None,
                     model: Optional[str] = None) -> str:
    """`text` in `target`, or the original text unchanged on any failure.

    Proper nouns are pinned: an Orna item/class/spell name is an IDENTIFIER, and
    a translated one matches nothing in the data - the live failure that lock
    already existed for ("Дудар", "Гільгармос" resolve to nothing).

    `pin` overrides WHICH words are pinned. The default, _proper_nouns, takes
    every capitalised word - right for /orna's answers, which are dense with
    item names, but wrong for prose, where every sentence starts with a capital:
    a game announcement came back as "Thank всім за гру", "Технічне
    обслуговування Server", "Happy свята" because "Thank", "Server" and "Happy"
    had been ORDERED to stay English. telegram_announce passes only names that
    exist in the game data instead."""
    text = (text or "").strip()
    if not text:
        return text
    names = pin if pin is not None else (_proper_nouns(text) if target != "English" else [])
    keep = ""
    if names:
        keep = (" These are IDENTIFIERS and must appear in your output EXACTLY as written here, unchanged and "
                "not transliterated: " + "; ".join(names[:25]) + ".")

    async def _once(extra: str) -> Optional[str]:
        # Same language in and out is a REWRITE, not a translation: the loop
        # model sometimes writes the user's language itself, badly, and the
        # translation model then polishes it rather than being asked to
        # "translate Ukrainian into Ukrainian".
        task = (f"Rewrite the user's message in natural, correct {target}: fix awkward phrasing, calques and "
                f"any Russian words or spellings, without changing its meaning or facts."
                if source and source == target else
                f"Translate the user's message into {target}." + (f" It is written in {source}." if source else ""))
        prompt = (
            task
            + " Reply with JSON only: {\"text\": \"<the translation>\"}. Rules: translate the MEANING, not word "
              "by word. NEVER translate or transliterate a proper noun - Orna item, class, specialization, "
              "monster, spell, guild, event and material names keep their original spelling exactly (they are "
              "identifiers; a translated name matches nothing in the game data). Keep numbers, percentages and "
              "any HTML tags exactly as they are. Do not answer the message, add anything, or omit anything."
            + (" Write natural UKRAINIAN, never Russian: Ukrainian has no letters ы, э, ъ or ё, and Russian words "
               "or spellings in the reply are a mistake." if target == "Ukrainian" else "")
            + keep + extra
        )
        try:
            got = await chat_json_with_fallback(
                model or ORNA_CLOUD_MODEL, LOCAL_OLLAMA_HOST, LOCAL_OLLAMA_MODEL,
                [{"role": "system", "content": prompt}, {"role": "user", "content": text}],
                api_key=OLLAMA_API_KEY, timeout=STEP_MODEL_TIMEOUT, local_timeout=LOCAL_MODEL_TIMEOUT,
            )
        except Exception as e:
            logger.warning("orna: translation to %s failed (%s)", target, e)
            return None
        out = got.get("text") if isinstance(got, dict) else None
        return out.strip() if isinstance(out, str) and out.strip() else None

    out = await _once("")
    if out is None:
        logger.warning("orna: translation to %s produced nothing - using the original", target)
        return text
    # VERIFY the identifiers survived, and retry ONCE naming the ones that did
    # not. Checked rather than trusted: "Duelist" came back as "Дулїст" on the
    # first live Ukrainian answer even with the rule above in the prompt.
    lost = [n for n in names if n not in out]
    if lost:
        logger.info("orna: translation dropped %s - retrying once", lost[:5])
        retry = await _once(" Your previous attempt WRONGLY changed these names; reproduce each one character for "
                            "character: " + "; ".join(lost[:15]) + ".")
        if retry is not None:
            still = [n for n in lost if n not in retry]
            if len(still) < len(lost):
                out = retry
            if still:
                logger.warning("orna: translation still dropped %s", still[:5])
    bad_re = _RUSSIAN_ONLY_RE if target == "Ukrainian" else None
    if _FOREIGN_SCRIPT_RE.search(out) or (bad_re and bad_re.search(out)):
        bad = sorted(set(_FOREIGN_SCRIPT_RE.findall(out) + (bad_re.findall(out) if bad_re else [])))
        logger.info("orna: Ukrainian translation contains Russian %s - retrying once", bad)
        retry = await _once(" Your previous attempt used text that is not " + target + " (" + ", ".join(bad[:8])
                            + ") - Russian words, or another script entirely. Rewrite it in correct " + target
                            + ", keeping every name exactly as written in the source.")
        if retry is not None and not _FOREIGN_SCRIPT_RE.search(retry) and \
                not (bad_re and bad_re.search(retry)):
            out = retry
    return out


def _uk_material_names(text: str) -> list:
    """[(ukrainian, english)] for every material named in `text`, inflection-
    tolerant: each word matches on its stem ("червоного драконіту" finds
    "Червоний драконіт"). A short stem can over-match ("камінь" inside "камінь
    ночі"); harmless, the model reads the list as candidates."""
    low = (text or "").lower()
    found = []
    for uk_low, en in UK_TO_EN.items():
        stems = [w[:max(4, len(w) - 2)] for w in uk_low.split()]
        if re.search(r"\b" + r"\w*\s+".join(re.escape(st) for st in stems), low):
            found.append((EN_TO_UK[en], en))
    return found


async def build_loop_messages(text: str, allow_ask: bool = True) -> tuple:
    """-> (messages, user_lang). The INPUT GATE.

    A non-English request is translated for the loop, and the ORIGINAL is kept
    alongside it verbatim: the translation is what the model reasons over, but
    an item or material name is safer in the spelling the user actually typed -
    `need`'s extraction and every codex name lookup work on those tokens."""
    user_lang = _detect_lang(text)
    body = text
    if user_lang != _LOOP_LANGUAGE:
        english = await _translate(text, _LOOP_LANGUAGE, source=user_lang, model=TRANSLATION_MODEL)
        if english != text:
            body = (f"{english}\n\n[The user wrote this in {user_lang}. Original, verbatim - prefer THIS "
                    f"spelling for any item/material/class name you pass to a tool: {text}]")
        known = _uk_material_names(text)
        if known:
            # From the game's own name table - exact, unlike the translation
            # (live: "червоний драконіт" came back "Red Dragonite").
            body += "\n[Game names in the original: " + "; ".join(f"{uk} = {en}" for uk, en in known) + "]"
    return ([{"role": "system", "content": _orna_system_prompt(text, allow_ask=allow_ask)},
             {"role": "user", "content": f"{_USER_QUESTION}\n{body}"}], user_lang)


# The loop's transcript is TAGGED so the model can tell what the user said from
# what a tool returned, and so _working_state can index it. None of this is ever
# sent to Telegram - the user sees only tool cards and the final answer.
_USER_QUESTION = "[USER QUESTION]"
_USER_FOLLOW_UP = "[USER FOLLOW-UP]"
_USER_ANSWER = "[USER ANSWER to your question]"
_SYSTEM_NOTE = "[SYSTEM NOTE]"
_TOOL_RESULT_RE = re.compile(r"^\[TOOL RESULT #(\d+) \u00b7 ([^\]]*)\]")
_TAG_RE = re.compile(r"^\[(USER QUESTION|USER FOLLOW-UP|USER ANSWER to your question)\]\n?")


def _tool_result(n: int, action: str, action_input: str, args: dict, observation: str) -> str:
    call = action + (f" {action_input!r}" if action_input else "")
    if args:
        call += " " + json.dumps(args, ensure_ascii=False)[:160]
    return f"[TOOL RESULT #{n} \u00b7 {call}]\n{observation}"


def _working_state(messages: list) -> str:
    """A compact index of the conversation, appended (never stored) as the last
    message of every step call. Live 2026-09-29: the loop assessed a 200%
    godforged item, then on "+20%" re-ran codex lookups and added 20% to the
    BASE stat - the right number was in its context, buried among raw JSON
    turns, and nothing pointed it there. This names every user turn, the
    model's own previous answers, and every tool result by number, and asks
    the thought to reason FROM them before choosing a tool."""
    convo, results, n_results = [], [], 0
    for m in messages:
        content = str(m.get("content") or "")
        if m.get("role") == "user":
            tag = _TAG_RE.match(content)
            hit = _TOOL_RESULT_RE.match(content)
            if tag:
                convo.append(f"- {tag.group(1)}: {content[tag.end():][:400]}")
            elif hit:
                n_results += 1
                body = content[hit.end():].strip().replace("\n", " ")
                results.append(f"- #{hit.group(1)} {hit.group(2)} -> {body[:220]}")
        elif m.get("role") == "assistant":
            try:
                step = json.loads(content)
            except (ValueError, TypeError):
                continue
            if isinstance(step, dict) and step.get("action") in ("finish", "ask"):
                label = "YOUR ANSWER (already shown to the user)" if step["action"] == "finish" else "YOUR QUESTION"
                convo.append(f"- {label}: {str(step.get('action_input') or '')[:400]}")
    return ("[WORKING STATE - internal, never shown to the user]\n"
            "Conversation so far (respond to the LAST user line):\n" + "\n".join(convo or ["- (none)"])
            + "\nTool results already in this context - the full text is above; read it, do not fetch it again:\n"
            + ("\n".join(results[-25:]) if results else "- (none yet)")
            + "\nIn \"thought\", before choosing an action: (1) say what the latest user line asks; (2) name the "
            "result numbers (#n) or earlier answer that already contain the data and quote the exact values; "
            "(3) do any arithmetic on THOSE values (e.g. \"+20%\" applies to the number you already reported, not "
            "to a base stat); (4) call a tool ONLY for data none of them contain - otherwise finish.")


def _orna_system_prompt(user_text: str = "", allow_ask: bool = True) -> str:
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M %A")
    actions = "|".join(f'"{a}"' for a in _ACTIONS)
    # Deterministic per-request language lock. Prompt-only "reply in the
    # user's language" guidance kept losing, for build/class_guide answers, to
    # the prompt's Ukrainian examples plus the long ENGLISH guide excerpt the
    # model reads right before finishing (live bug: English build questions
    # answered in Ukrainian ~half the time even after that guidance). Detecting
    # the script the user actually wrote in (this guild writes English or
    # Ukrainian) and stating the required language up front, per request, is
    # far more reliable than making the model infer it.
    # ENGLISH-FIRST: the loop reasons and writes in English, ALWAYS, and an
    # output gate translates the finished answer back if the user wrote in
    # something else (see _translate / build_loop_messages). This replaced a
    # per-request lock that forced the model into the user's language: every
    # knowledge source here is English, so that made it reason across a language
    # boundary on every step and read English evidence to write Ukrainian
    # thoughts. One language for all reasoning is both simpler and, per the
    # design ask, less likely to lose context.
    lang_lock = (
        f"LANGUAGE: think, plan and write EVERY thought, tool argument, ask and finish answer in "
        f"{_LOOP_LANGUAGE}. Do this even when the user wrote in another language - their message has been "
        f"translated for you above, and your answer is translated back for them automatically, so you never "
        f"need to write their language yourself. Every source you can read (codex, community sheets, guides, "
        f"developer comments, player Q&A) is in English too, so staying in English keeps your reasoning and "
        f"your evidence in one language.\n"
        "PROPER NAMES ARE NEVER TRANSLATED, in any direction: items, classes, specializations, monsters and "
        "spells are IDENTIFIERS and keep their English spelling - a translated name matches nothing in the "
        "data (live failure: a class question offered \"Дудар\" and \"Гільгармос\", which resolve to "
        "nothing at all).\n\n"
    )
    # The clarification example below is the ask text the model is most likely
    # to imitate, so it is written in the language the lock just demanded. A
    # fixed Ukrainian example sitting next to the instruction to answer in
    # English is the same fight _CLASS_GUIDE_RULE already lost - live
    # 2026-09-25, "/orna calculate my stats" came back in Ukrainian.
    # Always English now. It used to be built in the user's language so the model
    # would imitate the right one; under English-first there is only one, which
    # removes that whole failure mode (the prompt's own examples repeatedly beat
    # the language instruction - see CLAUDE.md).
    ask_example = ("tell me your specialization and AL (for base stats); for a full estimate, also your gear and "
                   "each item's quality")
    no_ask = "" if allow_ask else (
        "INLINE MODE: this request has NO reply channel - there are no buttons and the user cannot answer "
        "you. NEVER call ask here. If something is missing, pick the most reasonable assumption, ANSWER "
        "anyway, and state plainly in finish() what you assumed and which detail would change it.\n\n"
    )
    return (
        lang_lock + no_ask +
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
        f"{_COMPLETENESS_RULE}\n\n"
        f"{_TAXONOMY_RULE}\n\n"
        f"{_REASONING_RULE}\n\n"
        f"{_CONFIDENCE_RULE}\n\n"
        "FIXED REPLIES - copy verbatim into finish()'s action_input, do not paraphrase or write your own version:\n"
        f"- a meta \"what can you do\"/\"help\"/\"допоможи\" ask with no real Orna subject: {_capabilities_text()!r}\n"
        "- a question about how THIS BOT or one of its commands works (\"how does /amity work\", \"what is "
        "/clarify\", \"how do I share an amity\", \"як поділитися amity\" - sharing amities IS this bot's feature): answer from that same text only, no tools - it is the complete list of commands; a command "
        "not in it does not exist for the user, so never mention or guess at others.\n"
        f'- a message shaped like a reminder request ("нагадай мені...", "remind me to..."): {_REMINDER_NUDGE!r}\n\n'
        "CONTEXT FORMAT: user turns are tagged [USER QUESTION] / [USER FOLLOW-UP] / [USER ANSWER to your question]; "
        "every tool's output is a numbered [TOOL RESULT #n \u00b7 call] block; [SYSTEM NOTE] is the loop talking; the "
        "final [WORKING STATE] message indexes all of it. Tool results are INTERNAL - the user never sees them, only "
        "your finish() answer, so a number you rely on must be restated there - EXCEPT a codex item's own stat "
        "card (1-2 entries looked at via open_entry/search_codex/query), which posts to the chat on its own, "
        "visible to the user already; do not copy its stats/effects/tags into finish(), just answer the question "
        "in one short sentence. Reuse earlier results instead of repeating a lookup.\n\n"
        "Each turn, reply with strict JSON only, no other text: "
        f'{{"thought":"<brief reasoning>","action":{actions},"action_input":"<string, unused for query/today>",'
        '"args":{"...only for action \\"query\\", see above..."},"options":["<opt1>","<opt2>"]}. "options" is only '
        "used with action \"ask\". Don't call finish before you have enough information, don't ask more than "
        "once, and don't repeat a tool call you've already made with the same input.\n\n"
        # This used to insist "you have NO callable functions" to stop
        # gpt-oss emitting a native tool call, because an undeclared one
        # could 500 the request. The action names ARE declared now
        # (_STEP_TOOLS) precisely so that can't happen, which makes that
        # claim both false and contradicted by the tool list Ollama's own
        # template injects - so it now just states the preference. Either
        # channel parses: ollama_client._from_tool_calls translates a native
        # call back into this same object.
        "CLARIFICATION - when a request is missing something that would CHANGE the answer, ask instead of "
    "guessing. estimate_stats is the clearest case, but mind what each mode actually needs: BASE stats need "
    "only the SPECIALIZATION and ASCENSION LEVEL (class is optional, and you do NOT need items or PVP for base "
    "stats - PVP defaults to PVE); a FULL loadout also needs the ITEMS and each one's QUALITY. Only ask for "
    "what the mode requires: for \"my base stats\" ask just spec + AL, NOT for gear. If a genuinely required "
    "piece is absent and the user hasn't said to assume, call ask() ONCE listing what you still need in the "
    f"question text (e.g. \"{ask_example}\") - a "
    "\"Своя відповідь\" button is added automatically, so they can type all of it in one message, and you "
    "must NOT add an \"Інше\"/\"Своя відповідь\" option yourself. **The user can also TYPE their answer "
    "to any question you ask (a hint telling them how is appended automatically).** Every option you "
    "give must be a possible ANSWER, not a category of answer: \"Magus\", \"Godforged\", \"PVP\" are "
    "answers; \"I'll provide details\", \"Specialization/Class\", \"Equipment and quality\" are NOT - "
    "tapping one of those tells you nothing and you will just have to ask again (live failure: three asks in a "
    "row, each answered by a tap that carried no information). When what you need is several details at once, "
    "ask for them ALL in the question text and give either real shortcut answers or no options at all. Options "
    "must also never be invented pairings (live failure: \"Маг(Gilgamesh)\", \"Ловець(deity)\", which are "
    "not real class/specialization pairs). Whatever is still unknown after the answer must be "
    "stated as an assumption in finish(), never silently defaulted. The "
    "same applies anywhere else a missing detail materially changes the result. Do NOT ask about something "
    "you can look up yourself, and do NOT ask when the user has already given a reasonable default.\n\n"
    "OUTPUT FORMAT: send that single JSON object as ordinary message content - that is the preferred "
        "channel, and the only one every backend agrees on. Emitting a native tool call for one of the action "
        "names above is understood too, but never mix the two or send anything besides the JSON object."
    )


@dataclass
class OrnaSession:
    messages: list
    steps_left: int
    created: float = field(default_factory=time.monotonic)
    ask_options: list = field(default_factory=list)
    cloud_calls: int = 0  # cloud attempts made this request (bounded by MAX_CLOUD_CALLS)
    # Every tool call already made this request, signature -> its observation.
    # The prompt tells the model not to repeat a call with the same input; it
    # does anyway (live 2026-09-24, the 11-item orn-bonus request: 4 of 16
    # steps were the IDENTICAL search_codex, which is what exhausted the step
    # budget before it could answer, and posted the same result card to the
    # chat 4 times). Replaying the cached observation instead of re-running
    # costs no step-budget-worth of new information either way, but it skips
    # the duplicate Telegram card and tells the model outright that it is
    # going in a circle.
    seen_calls: dict = field(default_factory=dict)
    # (label, url) for everything the answer was actually built from, in the
    # order it was consulted - surfaced as a "Джерела" button on finish().
    sources: list = field(default_factory=list)
    status: object = None  # the ephemeral _Status message, owned by _advance
    # Set when a tool refused for want of a USER-supplied input (see
    # _NEEDS_INPUT). A finish() while this is set is really a question, so the
    # typed-answer wait stays armed past it.
    needs_input: bool = False
    # The language the user wrote in, so the OUTPUT GATE knows what to translate
    # the finished answer into. Defaults to the loop's own language, i.e. no
    # translation - a caller that does not set it keeps the old behaviour.
    user_lang: str = _LOOP_LANGUAGE
    # Codex entries the loop READ this request. open_entry no longer posts a card
    # per entry (that buried the answer); finish() offers them behind one button.
    viewed_entries: list = field(default_factory=list)
    # INLINE mode can show no buttons and receive no reply - the answer is one
    # edited message in a chat the bot isn't in - so "ask" is turned off there
    # and the model is told to answer from what it has, stating assumptions,
    # rather than stalling on a question nobody can answer.
    allow_ask: bool = True
    asks_made: int = 0   # capped by MAX_ASKS_PER_REQUEST
    # Who asked - set by handle_orna, NOT taken from the message _advance was
    # handed: on a button-resumed step that is the BOT's own message.
    user_id: Optional[int] = None
    # The last reply was a question (ask(), or a finish() that really was one),
    # so the next /clarify is its ANSWER rather than a new follow-up question.
    awaiting: bool = False
    running: bool = False   # a /clarify mid-run would double-drive the loop
    # PLAN-WORK-REVIEW-RELEASE (see MAX_REVIEW_ROUNDS). use_plan_review gates
    # the REVIEW call in finish() - set by _maybe_plan, based on
    # _looks_complex_request, so a plain simple lookup never pays for it.
    # review_rounds counts REDO rounds actually spent, bounded by
    # MAX_REVIEW_ROUNDS. original_request is the user's own English-pipeline
    # text (see build_loop_messages), kept here so the REVIEW call has the
    # actual ask to check against without re-parsing session.messages.
    use_plan_review: bool = False
    review_rounds: int = 0
    original_request: str = ""
    # One-shot: a count/aggregation question that tried to finish with no tool
    # call at all has been sent back once already. See _forced_evidence_note.
    pushed_for_evidence: bool = False
    # Set by a tool that POSTED its own deliverable to the chat (the monuments
    # chart). REVIEW is told, or it demands the list the user already has -
    # live 2026-10-05 that made the model paste the raw observation as its answer.
    posted_note: str = ""
    pushed_for_leak: bool = False
    # The Telegram message a listing tool posted last - where the /clarify hint
    # goes when the closing line is dropped (see _listing_only).
    last_post: object = None


_ORNA_SESSIONS: dict[str, OrnaSession] = {}


def _new_orna_session(messages: list, steps_left: int, allow_ask: bool = True) -> str:
    now = time.monotonic()
    for sid in [s for s, sess in _ORNA_SESSIONS.items() if now - sess.created > SESSION_TTL_SECONDS]:
        _ORNA_SESSIONS.pop(sid, None)
    if len(_ORNA_SESSIONS) >= MAX_SESSIONS:
        oldest = min(_ORNA_SESSIONS, key=lambda s: _ORNA_SESSIONS[s].created)
        _ORNA_SESSIONS.pop(oldest, None)
    sid = uuid.uuid4().hex[:10]
    _ORNA_SESSIONS[sid] = OrnaSession(messages=messages, steps_left=steps_left, allow_ask=allow_ask)
    return sid


async def _maybe_plan(sid: str, user_text: str) -> None:
    """PLAN phase entry point, called once right after a fresh session is
    created (NOT on an ask()/clarify resume - that is a continuation of
    work already planned, not a new request). Scoped to
    _looks_complex_request so a simple lookup never pays the extra model
    call; any PLAN-call failure just leaves use_plan_review set with an
    empty plan note, never blocks the request that follows."""
    session = _ORNA_SESSIONS.get(sid)
    if session is None or not _looks_complex_request(user_text):
        return
    session.use_plan_review = True
    session.original_request = user_text
    plan_lines = await _call_plan_model(user_text)
    if plan_lines:
        note = "[SYSTEM NOTE] PLAN (made before any tool call): " + " | ".join(plan_lines)
        session.messages.append({"role": "user", "content": note})


async def _run_tool(message, action: str, action_input: str, args: dict, sources: Optional[list] = None,
                    session=None) -> str:
    """Dispatch one tool call. Wrapped in a broad except so a bug in any
    single tool ends that step with an observation the model can react to,
    instead of killing the whole loop (defense in depth alongside
    telegram_bot.py's global error handler - the loop itself should never
    need that safety net to produce a reply)."""
    usage_stats.record_tool_call(action)
    if sources is None:
        sources = []
    try:
        if action == "today":
            return await _run_listing(session, message, lambda m: _run_today_tool(m, _uk(session)))
        if action == "next":
            return await _run_listing(session, message, lambda m: _run_next_tool(m, action_input, _uk(session)))
        if action == "need":
            return await _run_need_tool(message, action_input)
        if action == "search_codex":
            return await _run_codex_search(message, action_input, sources=sources, session=session)
        if action == "query":
            return await _run_query_tool(
                message, args.get("conditions") or [], str(args.get("combinator") or "and"),
                str(args.get("category") or ""), str(args.get("sort_by") or ""), str(args.get("sort_dir") or "desc"),
                session=session, count=bool(args.get("count")), group_by=str(args.get("group_by") or ""),
            )
        if action == "sql":
            return await _run_sql_tool(message, action_input or str(args.get("sql") or ""), args, session)
        if action == "events":
            return await _run_events_tool(message, action_input)
        if action == "open_entry":
            return await _run_open_entry_tool(message, action_input, sources, session)
        if action == "research":
            return await _run_research_tool(message, action_input, args, sources, session)
        if action == "knowledge_search":
            return await _run_knowledge_tool(message, action_input, sources)
        if action == "monuments":
            return await _run_listing(session, message,
                                      lambda m: _run_monuments_tool(m, action_input, args, sources, session=session))
        if action == "estimate_stats":
            return await _run_estimate_stats_tool(message, args, sources)
        if action == "releases":
            return await _run_releases_tool(message, action_input, sources)
        if action == "web_search":
            return await _run_web_search_tool(message, action_input, sources)
        if action == "calculate":
            return await _run_calculate_tool(message, action_input)
        if action == "assess":
            # item sometimes arrives in action_input with args empty (live
            # 2026-09-24) - same wrong-field drift as the nested action_input
            # above, and it cost a step on "assess needs both an item name
            # and a quality" before the model retried with proper args.
            return await _run_assess_tool(message, str(args.get("item") or action_input or ""),
                                          str(args.get("quality") or ""))
        if action == "compare":
            return await _run_compare_tool(message, args.get("items") or [], str(args.get("quality") or ""))
        if action == "build_optimize":
            return await _run_build_optimize_tool(
                message, args.get("slots") or [], str(args.get("stat") or ""),
                str(args.get("useable_by") or ""), str(args.get("quality") or ""),
            )
        if action == "towers":
            return await _run_listing(session, message, lambda m: _run_towers_tool(m, _uk(session)))
        if action == "class_guide":
            return await _run_class_guide_tool(message, str(args.get("topic") or ""), str(args.get("query") or ""))
    except Exception as e:
        logger.warning("orna: tool %r failed", action, exc_info=True)
        return f"{action} failed: {e}"
    return (f"unknown action {action!r}; valid actions are today, next, need, search_codex, query, events, "
            "open_entry, knowledge_search, web_search, calculate, assess, compare, build_optimize, towers, "
            "class_guide, ask, finish.")


async def _close_out(session: "OrnaSession", message, limit_hit: str, fallback: str) -> None:
    """Take ONE more model call to answer with whatever the loop already
    gathered, instead of ending on a bare "couldn't do it".

    Both ways a request can end without finish() - out of steps, out of
    wall-clock time - hit after the loop has usually already collected what
    it needed and simply never got a turn to SAY it. Live 2026-09-24, the
    six-item orn-bonus request: runs that ended here had assessed five of
    the six items and had every number in context, and the user was shown
    none of it. This call is beyond MAX_STEPS and (for the timeout path)
    beyond LOOP_TIMEOUT_SECONDS, but it cannot loop - whatever comes back
    is the reply, and `fallback` is sent if it fails. It is bounded by
    _call_step_model's own retry-once, i.e. at most 2 x STEP_MODEL_TIMEOUT
    (~90s) past whichever limit was hit."""
    session.messages.append({
        "role": "user",
        "content": f"{_SYSTEM_NOTE} {limit_hit} - no further tool calls are possible. Reply NOW with action "
                   '"finish", putting the best answer you can give from everything gathered so far into '
                   "action_input, and say plainly which parts you could not confirm.",
    })
    try:
        final = str((await _call_step_model(session, MAX_STEPS)).get("action_input") or "").strip()
    except (OllamaError, UnsupportedMultimodal):
        logger.warning("orna: forced closing answer failed (%s)", limit_hit, exc_info=True)
        final = ""
    try:
        await _reply_markdown(message, final or fallback)
    except Exception:
        logger.warning("orna: failed to send closing answer", exc_info=True)


# What each tool is shown as while it runs. Anything missing falls back to a
# generic "working" line rather than leaking the internal action name.
_ACTION_LABELS = {
    "today": "📅 Дивлюся, що сьогодні в гільдіях…",
    "next": "📅 Шукаю, коли з'явиться матеріал…",
    "need": "🧮 Рахую, скільки потрібно…",
    "search_codex": "🔎 Шукаю в кодексі…",
    "query": "🔎 Підбираю за характеристиками…",
    "sql": "🗃 Запит до бази кодексу…",
    "monuments": "🏛 Дивлюся нагороди монументів…",
    "events": "🎪 Дивлюся календар подій…",
    "open_entry": "📖 Читаю сторінку кодексу…",
    "calculate": "🧮 Рахую…",
    "assess": "⚒️ Прораховую прокачку предмета…",
    "compare": "⚖️ Порівнюю предмети…",
    "build_optimize": "🧩 Підбираю найкращий білд…",
    "towers": "🗼 Перевіряю вежі…",
    "class_guide": "📚 Читаю гайд…",
    "knowledge_search": "📚 Шукаю в базі знань…",
    "releases": "🆕 Перевіряю патч-ноти…",
    "web_search": "🌐 Шукаю в інтернеті…",
    "research": "🔗 Збираю дані з кодексу…",
    "estimate_stats": "📊 Рахую характеристики…",
}
_THINKING_LABEL = "🤔 Думаю…"


def _status_detail(action: str, action_input: str, args: dict) -> str:
    """A short, human summary of WHAT a step is doing - its key argument(s) - to
    append to the action label in the ephemeral status, so the user (and an
    admin watching the log) sees the tool AND what it was called with, not just
    "searching…". Best-effort and capped; purely cosmetic (the status message is
    deleted when the request ends), so it never needs to be exhaustive."""
    args = args or {}

    def clip(s, n=64):
        s = " ".join(str(s).split())
        return s if len(s) <= n else s[:n - 1] + "…"

    if action == "query":
        conds = args.get("conditions") or []
        first = conds[0] if conds and isinstance(conds[0], dict) else {}
        cond = " ".join(str(first.get(k)) for k in ("field", "cmp", "value")
                        if first.get(k) not in (None, ""))
        extra = f" +{len(conds) - 1}" if len(conds) > 1 else ""
        return clip(", ".join(p for p in (args.get("category"), cond + extra) if p))
    if action == "assess":
        return clip(" ".join(str(args.get(k)) for k in ("item", "quality") if args.get(k)))
    if action == "compare":
        items = args.get("items") or []
        return clip(", ".join(map(str, items)) if isinstance(items, list) else items)
    if action == "build_optimize":
        return clip(args.get("stat", ""))
    if action == "estimate_stats":
        return clip(" ".join(str(args.get(k)) for k in ("specialization", "class", "ascension_level")
                             if args.get(k) not in (None, "")))
    if action == "research":
        ents = args.get("entities")
        return clip(", ".join(map(str, ents)) if isinstance(ents, list) and ents else action_input)
    if action == "class_guide":
        return clip(" ".join(str(args.get(k)) for k in ("topic", "query") if args.get(k)))
    # search_codex / open_entry / knowledge_search / web_search / releases /
    # need / next / calculate: the free-text input carries the argument.
    return clip(action_input)


async def _advance(sid: str, message, with_status: bool = True) -> None:
    """Wraps _advance_inner in a hard wall-clock deadline - see
    LOOP_TIMEOUT_SECONDS. No matter what happens inside (a hung call, a
    pathologically slow chain of fallbacks, anything), this guarantees a
    reply within a bounded time instead of the request just going quiet.

    Also owns the ephemeral status message's whole lifetime: created here and
    cleared in `finally`, so it can't be orphaned by the timeout path (which
    cancels _advance_inner mid-step), by an "ask" that returns to wait for a
    button, or by an unexpected exception.

    with_status=False is for INLINE mode (see handle_chosen_inline_result):
    there's no live chat to post an ephemeral progress message into - the
    loop's replies are collected and edited into the single inline message at
    the end - so the _Status message is skipped entirely."""
    session = _ORNA_SESSIONS.get(sid)
    status = _Status(message) if with_status else None
    if session is not None:
        session.status = status
        session.running = True
        last_user = next((str(m.get("content") or "") for m in reversed(session.messages)
                          if m.get("role") == "user" and _TAG_RE.match(str(m.get("content") or ""))), "")
        logger.info("orna: turn sid=%s user=%s\n  %s", sid, session.user_id,
                    last_user[:800].replace("\n", "\n  "))
        # "A card already answered" is per TURN. Live 2026-10-05 a /clarify
        # after a `next` card inherited it, so the follow-up's real answer was
        # dropped as "the card answers it" (and the hint edit hit "not modified").
        session.posted_note, session.last_post, session.pushed_for_leak = "", None, False
    try:
        await asyncio.wait_for(_advance_inner(sid, message), timeout=LOOP_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        logger.warning("orna: loop exceeded %ss, cut off (sid=%s)", LOOP_TIMEOUT_SECONDS, sid)
        session = _ORNA_SESSIONS.get(sid)
        fallback = "Запит триває надто довго — спробуйте ще раз або сформулюйте простіше."
        if session is None:
            try:
                await message.reply_text(fallback)
            except Exception:
                logger.warning("orna: failed to notify user about loop timeout", exc_info=True)
            return
        usage_stats.record_tool_call("_loop_timeout")
        await _close_out(session, message, "the time limit for this request was reached", fallback)
    finally:
        if session is not None:
            session.running = False
        if status is not None:
            await status.clear()


def _with_state(messages: list) -> list:
    return messages + [{"role": "user", "content": _working_state(messages)}]


async def _call_step_model(session: "OrnaSession", step_number: int):
    """One model call for one loop turn, with a single retry on failure.
    Live report: a long multi-tool-call request (several query/
    knowledge_search calls gathering numbers for a final calculation)
    died on ONE "Ollama returned non-JSON content: ''" - empty output,
    most likely the local model (this step's small context routed it local)
    running out of its own generation budget mid-"thought" under a long
    accumulated tool-call history, not a systematic failure. Discarding
    every step of reasoning already done over one blip is a bad trade -
    retrying the identical call once before giving up on the whole
    request costs a few seconds and matches the retry-once convention
    telegram_nlp._local_chat_json already uses for the same reason.

    EVERY step tries cloud first and falls back to local mid-turn if the
    cloud call fails (out of credits, network, ...) - see MAX_CLOUD_CALLS,
    which is now only a runaway guard rather than a routing decision. The
    two legs run on different deadlines (STEP_MODEL_TIMEOUT for cloud,
    LOCAL_MODEL_TIMEOUT for local) - see those constants."""
    for attempt in range(2):
        try:
            if session.cloud_calls < MAX_CLOUD_CALLS:
                session.cloud_calls += 1
                return await chat_json_with_fallback(
                    ORNA_CLOUD_MODEL, LOCAL_OLLAMA_HOST, LOCAL_OLLAMA_MODEL, _with_state(session.messages),
                    api_key=OLLAMA_API_KEY, timeout=STEP_MODEL_TIMEOUT, local_timeout=LOCAL_MODEL_TIMEOUT,
                    tools=_STEP_TOOLS,
                )
            # Only past the runaway guard (or MAX_CLOUD_CALLS=0, i.e. the
            # harness's FORCE_LOCAL): local is all there is.
            return await chat_json(LOCAL_OLLAMA_HOST, LOCAL_OLLAMA_MODEL, _with_state(session.messages),
                                   timeout=LOCAL_MODEL_TIMEOUT, tools=_STEP_TOOLS)
        except (OllamaError, UnsupportedMultimodal) as e:
            if attempt == 0:
                # Light log here on purpose (no exc_info) - this is an
                # anticipated, handled retry, not a crash; the full
                # traceback is logged once, where it's actually useful, if
                # the retry below also fails and the caller gives up.
                logger.warning("orna: step %d model call failed (%s), retrying once", step_number, e)
                continue
            raise


# PLAN-WORK-REVIEW-RELEASE: scoping PLAN/REVIEW to complex requests only.
# Deliberately a CHEAP heuristic, not an LLM classification call - asking a
# model "is this complex" just to decide whether to make MORE model calls
# would burn the very latency this gate exists to protect simple lookups
# from. Three cheap signals, any one of which is enough: a long request (most
# single-item lookups are under ~10 words), several quality/level markers
# (a multi-item loadout/compare, each item carrying its own "185%"/"lv10"),
# several comma/"and"-joined clauses (several named things at once), or a
# request naming one of the genuinely multi-step tool shapes (estimate_stats/
# compare/build_optimize-shaped asks). False positives just cost one extra
# PLAN + REVIEW call on a request that turns out simple - never a wrong
# answer - so the thresholds lean a little generous on purpose.
_COMPLEX_KEYWORDS_RE = re.compile(
    r"\b(estimate|optimi[sz]e|optimal|max(?:imi[sz]e)?|best\s+build|loadout|порахуй|мої\s+стати|"
    r"базові\s+стати|білд|compare|порівняй)\b", re.IGNORECASE)
_QUALITY_OR_LEVEL_RE = re.compile(r"\d+\s*%|\blv\s*\d+|\+\d+\b|масterforged|демонforged|godforged",
                                   re.IGNORECASE)
_CLAUSE_SPLIT_RE = re.compile(r",|\band\b|\bта\b|\bі\b", re.IGNORECASE)


def _looks_complex_request(text: str) -> bool:
    """Whether a /orna request is worth the extra PLAN + REVIEW model calls.
    See the MAX_REVIEW_ROUNDS comment above for why this is scoped at all."""
    text = text or ""
    if len(text.split()) >= 20:
        return True
    if len(_QUALITY_OR_LEVEL_RE.findall(text)) >= 2:
        return True
    if len(_CLAUSE_SPLIT_RE.findall(text)) >= 2:
        return True
    return bool(_COMPLEX_KEYWORDS_RE.search(text))


async def _call_plan_model(user_text: str) -> list[str]:
    """The PLAN phase: ONE separate model call, before any tool runs, with
    nothing to do but plan - list the constraints the request states and the
    ordered tool calls that would satisfy them. This is intentionally an
    independent call rather than more in-line "thought" text (see the
    MAX_REVIEW_ROUNDS comment for why that lever is weak), but it must never
    block or slow down the request it can't help: any failure here just
    returns [] and the loop proceeds exactly as it did before this existed."""
    prompt = (
        f"A user asked (about the mobile game Orna): {user_text!r}\n\n"
        "Before any tool call is made, write a short PLAN for answering this with the tools below - do NOT "
        "answer the question itself here, only plan.\n\n"
        f"{_TOOLS_TEXT}\n"
        'Reply with strict JSON only: {"constraints": ["<every explicit constraint stated - items, quality, '
        'level, class, spec, Ascension Level, PVE/PVP, slots, quantities, language, ...>"], '
        '"plan": ["<step 1: which tool, and why>", "<step 2: ...>", ...]}'
    )
    try:
        result = await chat_json_with_fallback(
            ORNA_CLOUD_MODEL, LOCAL_OLLAMA_HOST, LOCAL_OLLAMA_MODEL, [{"role": "user", "content": prompt}],
            api_key=OLLAMA_API_KEY, timeout=STEP_MODEL_TIMEOUT, local_timeout=LOCAL_MODEL_TIMEOUT,
        )
    except (OllamaError, UnsupportedMultimodal):
        logger.warning("orna: PLAN call failed, continuing without a plan", exc_info=True)
        return []
    lines = []
    constraints = result.get("constraints")
    if isinstance(constraints, list) and constraints:
        lines.append("Constraints to satisfy: " + "; ".join(str(c) for c in constraints if str(c).strip()))
    plan = result.get("plan")
    if isinstance(plan, list) and plan:
        lines.append("Planned steps: " + " -> ".join(str(p) for p in plan if str(p).strip()))
    return lines


async def _call_review_model(original_request: str, transcript: str, draft_answer: str, card_note: str) -> dict:
    """The REVIEW phase: ONE separate model call, after a draft finish() is
    proposed and before it is allowed to post (RELEASE), checking the draft
    against the evidence already gathered - a fresh call with nothing else to
    do but check, not the same step's own thought. Deliberately does NOT
    reuse session.messages/the full system prompt as its own context - that
    would hand it the very instructions that let the draft go wrong in the
    first place (see MAX_REVIEW_ROUNDS); it gets a short, independent brief
    instead.

    Returns {"verdict": "approve"|"revise"|"redo", "answer": "...", "feedback": "..."}.
    MUST NEVER cost the answer: any failure here degrades to "approve" with
    the model's own draft, same reliability contract as the confidence gate."""
    prompt = (
        "REVIEW the DRAFT answer below against the EVIDENCE already gathered for this request, before it is "
        "sent to the user. Checklist: (1) does it answer every explicit constraint in the original request; "
        "(2) does every number/claim trace to the evidence, not memory; (3) does it avoid repeating stats/"
        "effects/tags that a codex card ALREADY posted to the chat on its own"
        + (f" ({card_note})" if card_note else " - no card was posted this time, so stating codex facts here is "
                                                "fine") + "; (4) is anything the evidence warned about (a "
        "refusal, a partial list, an assumption) missing from the draft. If the draft is fine, approve it. If "
        "it is wrong only in WORDING - e.g. it repeats numbers already visible in a posted card, or is needlessly "
        "long - rewrite it short and correct, adding NO new claims beyond what the evidence already supports. If "
        "it is substantively wrong or incomplete (missing a constraint, an unverified number), send it back with "
        "concrete feedback naming what tool call would fix it.\n\n"
        f"ORIGINAL REQUEST: {original_request!r}\n\n"
        f"EVIDENCE GATHERED SO FAR:\n{transcript}\n\n"
        f"DRAFT ANSWER: {draft_answer!r}\n\n"
        'Reply with strict JSON only: {"verdict": "approve"|"revise"|"redo", '
        '"answer": "<only for revise - the corrected short answer, in English>", '
        '"feedback": "<only for redo - what is missing/wrong and which tool would fix it>"}'
    )
    try:
        return await chat_json_with_fallback(
            ORNA_CLOUD_MODEL, LOCAL_OLLAMA_HOST, LOCAL_OLLAMA_MODEL, [{"role": "user", "content": prompt}],
            api_key=OLLAMA_API_KEY, timeout=STEP_MODEL_TIMEOUT, local_timeout=LOCAL_MODEL_TIMEOUT,
        )
    except (OllamaError, UnsupportedMultimodal):
        logger.warning("orna: REVIEW call failed, approving the draft as-is", exc_info=True)
        return {"verdict": "approve"}


def _normalize_options(raw) -> list:
    """The "ask" options as a real list of strings.

    The model sometimes hands back the whole list packed into ONE string -
    seen live: `["['Клас та одяг', 'Тільки класс', 'Інше']"]`, which rendered
    as a single button labelled with a Python list repr. Same wrong-shape
    drift as action_input arriving inside args; translate rather than reject.
    A bare string is also unpacked, since iterating it would otherwise make
    one button per CHARACTER."""
    if raw is None:
        return []
    if isinstance(raw, str):
        raw = [raw]
    out = []
    for item in raw if isinstance(raw, (list, tuple)) else [raw]:
        text = str(item).strip()
        if not text:
            continue
        if text.startswith(("[", "(")) and text.endswith(("]", ")")) and "," in text:
            try:
                parsed = ast.literal_eval(text)
            except (ValueError, SyntaxError):
                parsed = None
            if isinstance(parsed, (list, tuple)):
                out.extend(str(x).strip() for x in parsed if str(x).strip())
                continue
        out.append(text)
    return out


async def _advance_inner(sid: str, message) -> None:
    session = _ORNA_SESSIONS.get(sid)
    if session is None:
        return

    while session.steps_left > 0:
        session.steps_left -= 1
        step_number = MAX_STEPS - session.steps_left
        if session.status is not None:
            await session.status.update(_THINKING_LABEL)
        try:
            step = await _call_step_model(session, step_number)
        except (OllamaError, UnsupportedMultimodal) as e:
            logger.warning("orna: model call failed twice, giving up", exc_info=True)
            await message.reply_text(f"Не вдалося обробити запит: {e}")
            return

        action = step.get("action")
        args = step.get("args") if isinstance(step.get("args"), dict) else {}
        # The model sometimes nests action_input INSIDE args instead of
        # alongside it (live 2026-09-24: calculate arrived as
        # {"action":"calculate","args":{"action_input":"1.575 * 1.65 * ..."}}).
        # The expression was right there and perfectly usable, but the tool
        # got "" and spent a step answering "calculate needs a numeric
        # expression". Accept either placement.
        action_input = str(step.get("action_input") or args.get("action_input") or "").strip()
        if action_input.lower() in ("none", "null", "undefined"):
            action_input = ""   # a model's "no value" written as text, for every tool
        # The call chain, one line per step, grouped by sid - grep "orna: step"
        # to read a whole request's trace. The harness prints this for a request
        # you run yourself; in production this log was the only thing missing.
        logger.info("orna: step %s/%s sid=%s action=%s input=%r args=%s\n  thought: %s",
                    step_number, MAX_STEPS, sid, action, action_input[:120],
                    {k: str(v)[:80] for k, v in args.items() if k != "action_input"} or "{}",
                    str(step.get("thought") or "")[:600])

        if action == "finish" or not action:
            usage_stats.record_tool_call("finish")
            # REVIEW (PLAN-WORK-REVIEW-RELEASE, see MAX_REVIEW_ROUNDS): a
            # separate model call checks the draft BEFORE anything is posted -
            # no card, no reply, nothing user-visible yet - so a "redo"
            # verdict costs nothing beyond the model call itself. Scoped to
            # use_plan_review (complex requests only) and bounded to one
            # round; past that, or on any call failure, this is a no-op and
            # the draft proceeds exactly as it would have without REVIEW.
            draft_answer = action_input or "Не вдалося сформувати відповідь."

            # A count answered with zero tool calls goes back once - see
            # _forced_evidence_note. Placed before REVIEW because REVIEW is
            # gated to complex requests and "how many items are in the codex"
            # is 7 words, so it would never reach that check.
            if not session.pushed_for_leak and _leaked_observation(session, draft_answer):
                session.pushed_for_leak = True
                logger.info("orna: draft answer pasted a tool observation sid=%s - sending it back", sid)
                session.messages.append({"role": "assistant", "content": json.dumps(step, ensure_ascii=False)})
                session.messages.append({"role": "user", "content": (
                    f"{_SYSTEM_NOTE} Your draft answer pasted a TOOL RESULT verbatim - that is internal text with "
                    "instructions meant for you, never something to send a player. Write the answer in your own "
                    "words." + (f" Note: {session.posted_note}, so a short takeaway is enough."
                                if session.posted_note else ""))})
                continue

            evidence_note = _forced_evidence_note(session)
            if evidence_note:
                logger.info("orna: forcing evidence for aggregation ask sid=%s", sid)
                session.messages.append({"role": "assistant", "content": json.dumps(step, ensure_ascii=False)})
                session.messages.append({"role": "user", "content": f"{_SYSTEM_NOTE} {evidence_note}"})
                continue

            listing_only = _listing_only(session, action_input)
            # No REVIEW once a tool has POSTED the deliverable. REVIEW never sees
            # the posted card, so it judges a one-line closing remark without the
            # data that remark is about - and it made it worse in all three
            # measured cases (2026-10-05): "Materials are those listed in the
            # monument table" (a tautology), a full answer cut to "Demeter", and
            # a CORRECT "Demeter - 8 floors" revised into "impossible to
            # determine which monument gives the most".
            if session.use_plan_review and session.review_rounds < MAX_REVIEW_ROUNDS and not session.posted_note:
                opened_preview = [e for e in session.viewed_entries if e.get("opened")]
                candidates_preview = opened_preview or session.viewed_entries
                card_note = ("a codex card for this will auto-post above your answer" if
                             session.allow_ask and 0 < len(candidates_preview) <= _AUTO_CARD_MAX_ENTRIES else "")
                card_note = "; ".join(x for x in (card_note, session.posted_note) if x)
                session.review_rounds += 1
                verdict = await _call_review_model(session.original_request, _working_state(session.messages),
                                                   draft_answer, card_note)
                kind = verdict.get("verdict")
                if kind == "redo" and str(verdict.get("feedback") or "").strip():
                    logger.info("orna: review REDO sid=%s feedback=%s", sid, str(verdict["feedback"])[:300])
                    session.messages.append({"role": "assistant", "content": json.dumps(step, ensure_ascii=False)})
                    session.messages.append({
                        "role": "user",
                        "content": f"{_SYSTEM_NOTE} REVIEW sent your draft answer back: {verdict['feedback']} "
                                   "Make the tool call this needs, then finish again.",
                    })
                    continue
                if kind == "revise" and str(verdict.get("answer") or "").strip():
                    logger.info("orna: review REVISED sid=%s", sid)
                    action_input = str(verdict["answer"]).strip()
            # finish() is the one place the model's own free-form prose
            # reaches the user (every other reply is a tool-built,
            # already-HTML message) - nothing in the prompt asks for
            # Markdown, but it writes it anyway often enough (**bold**,
            # headings, and - since class_guide started handing back
            # long-form guide excerpts - full pipe tables) that a plain
            # reply_text was showing that syntax completely literally.
            # Same fix /go already has for its own model-authored replies.
            markup = None
            rows: list = []
            # The entries the loop READ, behind one button. open_entry used to
            # post a card each - twelve of them on one real request - so the
            # answer arrived below a wall of them. This keeps the answer at the
            # bottom of the chat where the user is looking, and still one tap
            # from any entry.
            # A small lookup posts its card(s) in full - stats, effects and the
            # "Dropped by"/"Gives"/"Causes"/"Upgrade materials" sections, all as
            # text now - right above the answer. Inline mode has no chat to
            # post into (see _InlineSink), so it keeps the text-only shape.
            carded: list = []
            opened = [e for e in session.viewed_entries if e.get("opened")]
            candidates = opened or session.viewed_entries
            if session.allow_ask and 0 < len(candidates) <= _AUTO_CARD_MAX_ENTRIES:
                for entry in candidates:
                    try:
                        if await _send_entry(message, entry, "en"):
                            carded.append(entry)
                    except TelegramError:
                        # A card must never cost the answer it introduces.
                        logger.warning("orna: failed to post entry card %s",
                                       entry.get("url"), exc_info=True)
            # Everything already posted as a card is reachable in the chat, so
            # the button only lists what is not.
            remaining = [e for e in session.viewed_entries if e not in carded]
            if remaining:
                ekey = _remember({"entries": list(remaining), "lang": "en"})
                rows.append([InlineKeyboardButton(
                    f"📄 Записи кодексу ({len(remaining)})",
                    callback_data=f"orna|entries|{ekey}|0")])
            # The "📚 Джерела" button was removed (2026-10-01 ask): in a
            # 180-person group chat it's another dead button every member can
            # tap, each tap posting a new message. session.sources keeps being
            # collected (every tool that cites something still calls
            # _add_source) - only the button/_SOURCES-dict storage that
            # surfaced it in chat is gone; nothing else currently reads it,
            # but it's cheap bookkeeping and other code builds on it (e.g. the
            # _run_tool call below still threads it through).
            if rows:
                markup = InlineKeyboardMarkup(rows)
            # CONFIDENCE GATE. The model's own number, clamped by what the loop
            # actually verified, and below the floor the answer LEADS with an
            # admission instead of stating itself flatly.
            # OUTPUT GATE: the loop reasons in English, the user reads their own
            # language. Only translate when the answer is not ALREADY in the
            # target language - the fixed capability/reminder replies the prompt
            # tells the model to copy verbatim are Ukrainian, and re-translating
            # them would both cost a call and paraphrase deterministic text.
            answer_text = action_input or "Не вдалося сформувати відповідь."
            # The loop is meant to answer in English, but the loop model often
            # writes the user's language directly (live 2026-10-05: every draft
            # in Ukrainian) - and the old "only translate when not already in
            # that language" rule then let its Russian-tinged Ukrainian ("зелья")
            # straight through, untouched by the translation model. So a draft
            # already in the target language is REWRITTEN by TRANSLATION_MODEL,
            # except the fixed replies the prompt says to copy verbatim.
            # Skipped entirely when the closing line is being dropped anyway.
            fixed = any(f.strip()[:40] in answer_text for f in (_capabilities_text(), _REMINDER_NUDGE))
            if session.user_lang != _LOOP_LANGUAGE and not fixed and not listing_only:
                from telegram_announce import game_names
                same = _detect_lang(answer_text) == session.user_lang
                answer_text = await _translate(answer_text, session.user_lang,
                                               source=session.user_lang if same else _LOOP_LANGUAGE,
                                               pin=game_names(answer_text), model=TRANSLATION_MODEL)
            answer, effective = _confidence_gate(session, step, answer_text)
            logger.info("orna: finish sid=%s confidence=%s\n  %s", sid, effective, answer[:1500].replace("\n", "\n  "))
            # Keep the answer in the transcript: a /clarify follow-up is read
            # against it, and without it the model could not see what it said.
            session.messages.append({"role": "assistant", "content": json.dumps(step, ensure_ascii=False)})
            # The model often states what it still needs as an ANSWER rather
            # than as an ask() (live 2026-09-25: 2 of 3 runs of "порахуй мої
            # стати"). Two narrow signals that a finish is really a question: a
            # tool refused for want of a user input (_NEEDS_INPUT), or the loop
            # called no tool at all. Counted as an ask so the same cap bounds
            # it; the user's /clarify is then framed as the answer.
            if session.allow_ask:
                asked = bool((session.needs_input or not session.seen_calls)
                             and session.asks_made < MAX_ASKS_PER_REQUEST)
                session.asks_made += asked
                _arm_text_wait(sid, asked)
                if not listing_only:
                    answer += "\n\n" + _clarify_hint(session)
            # The card already answered and there is no low-confidence banner to
            # show: send no closing line. The /clarify hint goes ONTO the card,
            # so a follow-up still works without an extra message in the chat.
            if listing_only and answer == answer_text and not markup:
                logger.info("orna: closing line dropped sid=%s - the posted card answers it", sid)
                if session.allow_ask:
                    try:
                        await session.last_post.edit_text(
                            session.last_post.text_html + "\n\n" + html.escape(_clarify_hint(session)),
                            parse_mode="HTML", disable_web_page_preview=True)
                    except Exception:
                        logger.warning("orna: could not attach the clarify hint to the card", exc_info=True)
                return
            if listing_only and session.allow_ask:
                answer += "\n\n" + _clarify_hint(session)
            await _reply_markdown(message, answer, reply_markup=markup)
            return

        if action == "ask":
            usage_stats.record_tool_call("ask")
            if session.asks_made >= MAX_ASKS_PER_REQUEST:
                session.messages.append({
                    "role": "user",
                    "content": f"{_SYSTEM_NOTE} you have already asked {session.asks_made} times and must not "
                               "ask again. Use what the user has ALREADY told you - re-read their messages, "
                               "the answer is usually there - CALL the tool you were collecting inputs for, "
                               "and only then finish, stating any remaining assumption explicitly. Do not "
                               "describe what the tool would have computed: run it.",
                })
                continue
            if not session.allow_ask:
                session.messages.append({
                    "role": "user",
                    "content": f"{_SYSTEM_NOTE} you cannot ask anything here - this request came from INLINE mode, "
                               "where there are no buttons and no reply channel. Answer NOW from what you "
                               "already have, state the assumptions you made for any missing detail, and say "
                               "which detail would change the answer.",
                })
                continue
            options = [o for o in _normalize_options(step.get("options"))
                       if not _OTHER_OPTION_RE.search(o)][:4]
            if not options:
                session.messages.append({
                    "role": "user",
                    "content": _SYSTEM_NOTE + ' "ask" needs 2-4 short "options" to tap - '
                               "there's no free-text reply channel here. Retry with options, or finish.",
                })
                continue
            session.asks_made += 1
            session.ask_options = options
            session.messages.append({"role": "assistant", "content": json.dumps(step, ensure_ascii=False)})
            # Accept a TYPED answer to this question, not only a tapped one.
            # Live: the bot asked, the user typed the full answer, and nothing
            # happened - the wait was armed only by the escape-hatch button,
            # so a perfectly good reply fell through to the other handlers and
            # the request looked stuck. Tapping a real option clears this
            # again (see orna_callback), so it cannot swallow an unrelated
            # message once the question has been answered.
            _arm_text_wait(sid, True)
            # One option per row. Four 30-char labels in a single row is the
            # same shape that made the old UTC picker unreadable on a phone -
            # Telegram shrinks buttons to fit and clips the text with no
            # ellipsis, and a clarification option is a phrase, not "+3".
            rows = [[InlineKeyboardButton(opt[:60], callback_data=f"orna|ask|{sid}|{i}")]
                    for i, opt in enumerate(options)]
            # Always an escape hatch, so answering in your own words never
            # depends on the model having thought to offer "Інше".
            rows.append([InlineKeyboardButton("✍️ Своя відповідь", callback_data=f"orna|askfree|{sid}|0")])
            keyboard = InlineKeyboardMarkup(rows)
            # An ask is user-facing too, so it goes through the same gate. The
            # OPTIONS are deliberately left alone: they are matched back by
            # exact text when tapped, and several are proper nouns (class and
            # specialization names) that must not be translated at all.
            ask_text = action_input or "Уточніть, будь ласка:"
            if (session.user_lang != _LOOP_LANGUAGE
                    and _detect_lang(ask_text) != session.user_lang):
                ask_text = await _translate(ask_text, session.user_lang, source=_LOOP_LANGUAGE,
                                            model=TRANSLATION_MODEL)
            await _reply_markdown(message, ask_text + "\n\n" + _clarify_hint(session), reply_markup=keyboard)
            return

        if session.status is not None:
            _label = _ACTION_LABELS.get(action, "⏳ Працюю…")
            _detail = _status_detail(action, action_input, args)
            await session.status.update(f"{_label} «{_detail}»" if _detail else _label)
        session.messages.append({"role": "assistant", "content": json.dumps(step)})
        sig = json.dumps([action, action_input, args], sort_keys=True, ensure_ascii=False)
        if sig in session.seen_calls:
            observation = (f"You already made this exact call earlier and it returned: "
                           f"{session.seen_calls[sig]} - it was NOT run again. Stop repeating it: use that "
                           f"result, try a DIFFERENT tool or input, or finish with what you have.")
        else:
            observation = await _run_tool(message, action, action_input, args, session.sources, session)
            session.seen_calls[sig] = observation
        session.needs_input = observation.startswith(_NEEDS_INPUT)
        logger.info("orna: result sid=%s action=%s\n  %s", sid, action, observation[:1500].replace("\n", "\n  "))
        n_result = sum(1 for m in session.messages
                       if m.get("role") == "user" and _TOOL_RESULT_RE.match(str(m.get("content") or ""))) + 1
        session.messages.append({"role": "user", "content": _tool_result(
            n_result, action, action_input, args if isinstance(args, dict) else {}, observation)})

    usage_stats.record_tool_call("_step_budget_exhausted")
    await _close_out(session, message, "the step budget is exhausted",
                     "Не вдалося сформувати відповідь за відведену кількість кроків — спробуйте уточнити запит.")


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

    messages, user_lang = await build_loop_messages(text)
    sid = _new_orna_session(messages, MAX_STEPS)
    _ORNA_SESSIONS[sid].user_lang = user_lang
    user_id = getattr(getattr(message, "from_user", None), "id", None)
    _ORNA_SESSIONS[sid].user_id = user_id
    if user_id is not None:   # one conversation per user: a new /orna replaces the old one
        _ORNA_SESSIONS.pop(_USER_SESSIONS.get(user_id, ""), None)
        _USER_SESSIONS[user_id] = sid
    await _maybe_plan(sid, messages[1]["content"])
    await _advance(sid, message)


# -----------------------------------------------------------------------------
# inline mode: @bot <query> in ANY chat, including groups the bot isn't in
# -----------------------------------------------------------------------------

class _InlineSink:
    """A stand-in "message" for the /orna loop in INLINE mode.

    Inline mode can't stream several messages into a chat the bot isn't a
    member of - the user's chosen result posts ONE message that we
    editMessageText afterwards. So instead of posting, this COLLECTS every
    reply_text the loop makes and joins them into that single message; photos
    and inline keyboards are dropped (an inline message is one text block).
    __getattr__ makes any other method the loop calls (reply_photo, a returned
    message's edit_reply_markup, ...) a harmless async no-op. The loop only
    ever touches reply_text / chat_id / reply_photo on its message, all
    covered here."""

    def __init__(self) -> None:
        self.parts: list = []
        self.chat_id = 0  # the typed-clarification flow can't run inline; harmless

    async def reply_text(self, text=None, *args, **kwargs):
        if text is None and args:
            text = args[0]
        if text:
            self.parts.append(str(text))
        return self

    async def edit_text(self, *args, **kwargs):
        return self

    async def delete(self, *args, **kwargs):
        return None

    def __getattr__(self, name):
        async def _noop(*args, **kwargs):
            return self
        return _noop

    def rendered(self) -> str:
        return "\n\n".join(p for p in self.parts if p and p.strip())


# The placeholder result carries an inline keyboard because Telegram only
# reports the chosen result's inline_message_id (which we must have to edit the
# answer in) when the sent message has one.
_INLINE_PLACEHOLDER_KB = InlineKeyboardMarkup(
    [[InlineKeyboardButton("⏳ обробляю…", callback_data="orna_inline_wait")]]
)
_INLINE_MAX_LEN = 4096  # Telegram message hard limit


async def handle_inline_query(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """@bot <query> typed in any chat. Answers with ONE cheap placeholder
    result and does NO work here - inline queries fire on every keystroke. The
    real /orna loop runs once, when the user PICKS this result, in
    handle_chosen_inline_result."""
    iq = update.inline_query
    if iq is None:
        return
    query = (iq.query or "").strip()
    if not query:
        await iq.answer([InlineQueryResultArticle(
            id="orna-help",
            title="Запит про Orna",
            description="Напишіть питання, напр.: balor sword або адамантій 2000",
            input_message_content=InputTextMessageContent("/orna"),
        )], cache_time=5, is_personal=True)
        return
    result = InlineQueryResultArticle(
        id=uuid.uuid4().hex,
        title=f"Orna: {query}",
        description="Натисніть, щоб отримати відповідь",
        input_message_content=InputTextMessageContent(
            f"🔎 <b>{html.escape(query)}</b>\n⏳ обробляю…", parse_mode="HTML"
        ),
        reply_markup=_INLINE_PLACEHOLDER_KB,
    )
    # cache_time=0 + is_personal so each user's pick re-runs the loop freshly,
    # rather than Telegram serving one user's cached placeholder to another.
    await iq.answer([result], cache_time=0, is_personal=True)


async def handle_chosen_inline_result(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """The user picked the inline result - run the SAME /orna loop as a chat
    request and edit its answer into the posted (inline) message. Requires
    BotFather inline feedback (/setinlinefeedback) or this update is never
    delivered; inline_message_id is present only because the placeholder had a
    keyboard."""
    cir = update.chosen_inline_result
    if cir is None:
        return
    query = (cir.query or "").strip()
    inline_message_id = cir.inline_message_id
    if not query or not inline_message_id:
        return
    usage_stats.record_command_for(update, "orna_inline", query)

    messages, user_lang = await build_loop_messages(query, allow_ask=False)
    sid = _new_orna_session(messages, MAX_STEPS, allow_ask=False)
    _ORNA_SESSIONS[sid].user_lang = user_lang
    sink = _InlineSink()
    try:
        await _advance(sid, sink, with_status=False)
    except Exception:
        logger.warning("orna inline: loop failed for %r", query, exc_info=True)

    answer = sink.rendered() or "Не вдалося сформувати відповідь. Спробуйте /orna у чаті з ботом."
    header = f"🔎 <b>{html.escape(query)}</b>\n\n"
    text = (header + answer)[:_INLINE_MAX_LEN]
    try:
        await context.bot.edit_message_text(
            inline_message_id=inline_message_id, text=text,
            parse_mode="HTML", disable_web_page_preview=True,
        )
    except TelegramError:
        # Collected HTML can be malformed once truncated mid-tag - fall back to
        # a tag-stripped plain version so the "обробляю…" placeholder never
        # stays stuck.
        plain = re.sub(r"<[^>]+>", "", f"🔎 {query}\n\n{answer}")[:_INLINE_MAX_LEN]
        try:
            await context.bot.edit_message_text(
                inline_message_id=inline_message_id, text=plain, disable_web_page_preview=True,
            )
        except TelegramError:
            logger.warning("orna inline: failed to edit answer for %r", query, exc_info=True)


def build_inline_query_handler() -> InlineQueryHandler:
    return InlineQueryHandler(handle_inline_query)


def build_chosen_inline_result_handler() -> ChosenInlineResultHandler:
    return ChosenInlineResultHandler(handle_chosen_inline_result)


async def _await_ask_text(query, sid: str) -> None:
    """"Своя відповідь" was tapped: the answer comes as /clarify."""
    _arm_text_wait(sid, True)
    session = _ORNA_SESSIONS.get(sid)
    await query.message.reply_text("✍️ " + _clarify_hint(session))


def _arm_text_wait(sid: str, asked: bool) -> None:
    """The bot just replied in `sid`: restart its idle clock and record whether
    that reply was a question, so /clarify is framed right."""
    session = _ORNA_SESSIONS.get(sid)
    if session is not None:
        session.awaiting = asked
        session.created = time.monotonic()


def _clarify_hint(session) -> str:
    uk = getattr(session, "user_lang", "") == "Ukrainian"
    return (f"💬 Уточнити: /clarify <текст> (протягом {SESSION_TTL_SECONDS // 60} хв)" if uk else
            f"💬 Follow up: /clarify <text> (within {SESSION_TTL_SECONDS // 60} min)")


def _user_session(user_id) -> tuple:
    """(sid, session) of the user's live conversation, or (None, None)."""
    sid = _USER_SESSIONS.get(user_id)
    session = _ORNA_SESSIONS.get(sid) if sid else None
    if session is None or time.monotonic() - session.created > SESSION_TTL_SECONDS:
        _USER_SESSIONS.pop(user_id, None)
        _ORNA_SESSIONS.pop(sid or "", None)
        return None, None
    return sid, session


async def handle_clarify(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/clarify <text>: the user's follow-up, or their typed answer to a
    question, fed back into THEIR current /orna conversation."""
    message = update.effective_message
    if not message:
        return
    said = " ".join(context.args or []).strip()[:500]
    user_id = getattr(getattr(message, "from_user", None), "id", None)
    sid, session = _user_session(user_id)
    if session is None:
        await message.reply_text("Немає активної розмови (вона живе 15 хв після відповіді) — почніть з /orna <запит>.")
        return
    if not said:
        await message.reply_text("Використання: /clarify <уточнення>, наприклад /clarify а для шолома?")
        return
    if session.running:
        await message.reply_text("⏳ Ще обробляю попередній запит — зачекайте на відповідь.")
        return
    if _is_pleasantry(said):
        # A closer, not a question - acknowledge and DON'T resume the loop
        # (live 2026-09-27: it re-ran tools and re-stated the same answer).
        await message.reply_text("Будь ласка! 🙂" if _CYRILLIC_RE.search(said) else "You're welcome! 🙂")
        return
    if session.awaiting:
        follow_up = f"{_USER_ANSWER}\n{said}"
    else:
        # A new turn of the same conversation - NOT an answer to anything.
        # A new turn of the same conversation: it may correct, narrow or extend
        # the previous answer, which is now in the transcript (see finish).
        follow_up = f"{_USER_FOLLOW_UP}\n{said}"
    session.messages.append({"role": "user", "content": follow_up})
    # Stale once the user has supplied something - left set, the next finish
    # would be framed as a question again.
    session.needs_input = False
    # An answer only unblocks the tool that wanted it; a follow-up is a fresh
    # question. _advance's wall-clock ceiling bounds both.
    session.steps_left = max(session.steps_left, _RESUME_STEPS if session.awaiting else MAX_STEPS)
    session.awaiting = False
    session.created = time.monotonic()
    await _advance(sid, message)


def build_clarify_handler() -> CommandHandler:
    return CommandHandler("clarify", handle_clarify)


async def orna_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    parts = (query.data or "").split("|")
    if len(parts) < 4 or parts[0] != "orna":
        return
    kind, key, arg = parts[1], parts[2], parts[3]

    if kind == "src":
        entries = _SOURCES.get(key)
        if not entries:
            await query.message.reply_text("Джерела для цієї відповіді більше недоступні.")
            return
        # url= buttons rather than a text list: tappable, and Telegram renders
        # the destination itself so there's nothing to escape or truncate wrong.
        rows = [[InlineKeyboardButton(f"{i}. {label}"[:64], url=url)]
                for i, (label, url) in enumerate(entries[:_MAX_SOURCE_BUTTONS], 1)]
        more = len(entries) - len(rows)
        text = "📚 <b>Джерела цієї відповіді</b>" + (f"\n(+{more} не показано)" if more > 0 else "")
        await query.message.reply_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(rows))
        return

    if kind == "towerrem":
        state = _STATE.get(key)
        if state is None or "reminders" not in state:
            await query.answer("Ця сесія застаріла — запросіть поверхи веж ще раз.", show_alert=True)
            return
        idx = int(arg) if arg.isdigit() else -1
        reminders = state["reminders"]
        if not (0 <= idx < len(reminders)):
            return
        if idx in state["scheduled"]:
            await query.answer("Вже встановлено.", show_alert=True)
            return
        r = reminders[idx]
        # The stored `eta` is an ABSOLUTE UTC instant, re-read at TAP time
        # (not "hours" captured when the tool ran) so a button tapped an
        # hour later still fires at the real moment, not early. The delay
        # itself needs no timezone conversion (an elapsed duration is the
        # same real wait everywhere) - only schedule_reminder's own
        # datetime.now()-relative `fire_at` needs to be naive/server-local,
        # so the UTC delay is re-applied on top of a fresh `datetime.now()`.
        eta = datetime.datetime.fromisoformat(r["eta"])
        delay = (eta - datetime.datetime.now(datetime.timezone.utc)).total_seconds()
        fire_at = datetime.datetime.now() + datetime.timedelta(seconds=max(delay, 1))
        schedule_reminder(context.application, query.message.chat_id, r["text"], fire_at)
        state["scheduled"].add(idx)

        remaining_rows = [
            [InlineKeyboardButton(f"🔔 {rr['kind'].capitalize()} — 50 поверх (за {rr['hours']} год)",
                                  callback_data=f"orna|towerrem|{key}|{i}")]
            for i, rr in enumerate(reminders) if i not in state["scheduled"]
        ]
        try:
            await query.edit_message_reply_markup(
                reply_markup=InlineKeyboardMarkup(remaining_rows) if remaining_rows else None)
        except TelegramError:
            pass  # e.g. "message not modified" on a double-tap race - harmless
        return

    if kind == "ask":
        session = _ORNA_SESSIONS.get(key)
        if session is None:
            await query.message.reply_text("Ця сесія застаріла — спробуйте /orna ще раз.")
            return
        idx = int(arg) if arg.isdigit() else -1
        if not (0 <= idx < len(session.ask_options)):
            return
        choice = session.ask_options[idx]
        session.awaiting = False   # answered by this tap
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
        if _OTHER_OPTION_RE.search(choice):
            # "Інше" carries no information - resuming on it is what made the
            # loop guess. Wait for the real answer (/clarify).
            await _await_ask_text(query, key)
            return
        session.messages.append({"role": "user", "content": f"{_USER_ANSWER}\n{choice}"})
        await _advance(key, query.message)
        return

    if kind == "askfree":
        session = _ORNA_SESSIONS.get(key)
        if session is None:
            await query.message.reply_text("Ця сесія застаріла — спробуйте /orna ще раз.")
            return
        session.ask_options = []  # consume, same double-tap guard as above
        try:
            await query.edit_message_text(f"{query.message.text}\n\n→ ✍️", reply_markup=None)
        except TelegramError:
            pass
        await _await_ask_text(query, key)
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

    if kind == "entries":
        # Renders the read-entries list as the SAME paged keyboard a search
        # result list uses, so tapping one goes through the existing "open"
        # branch above and posts the normal card - no second rendering path.
        entries = state.get("entries") or []
        if not entries:
            return
        await query.message.reply_text(
            f"📄 <b>Записи, які я відкривав</b> — {len(entries)}",
            parse_mode="HTML",
            reply_markup=_result_list_keyboard(entries, key, 0),
        )
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

    # Patch notes are on their own week-long TTL, and the reason to run this
    # command at all is "a patch just landed" - refreshing one source and not
    # the other is exactly the stale mix this command exists to avoid.
    try:
        rel = await asyncio.to_thread(orna_releases.refetch_now)
    except Exception as e:
        logger.warning("update_codex: releases refetch failed", exc_info=True)
        rel = {"error": str(e)}

    lines = ["✅ Кодекс оновлено:"]
    for cat, count in stats["categories"].items():
        lines.append(f"  {cat}: {count}")
    lines.append(f"stats: {stats['stats_vocab']}, status: {stats['status_vocab']}")
    lines.append("playorna codex cache cleared")

    # The SQLite mirror is DERIVED from the dump we just refetched, so it has
    # to be rebuilt in the same command - otherwise the `sql` tool keeps
    # answering from pre-patch data while every other tool is current, which
    # is exactly the stale mix this command exists to prevent. The rebuild is
    # atomic (temp file + rename) and refuses to replace a good DB with an
    # empty one, so a failure here leaves the previous DB serving.
    try:
        db = await asyncio.to_thread(orna_codex_db.build)
        lines.append(f"SQL-база: {db['records']} записів, {db['stats']} статів, "
                     f"{db['links']} зв'язків, {db['terms']} термінів")
    except Exception as e:
        logger.warning("update_codex: sqlite rebuild failed", exc_info=True)
        lines.append(f"SQL-база: не вдалося перебудувати ({e})")
    if "error" in rel:
        lines.append(f"патч-ноти: не вдалося оновити ({rel['error']})")
    else:
        lines.append(f"патч-ноти: {rel['notes']} (останній: {rel['latest']}, {rel['newest']})")

    # The community sheets too - they are hand-maintained and drift
    # independently of any game patch, so "refresh everything" has to include
    # them. This is the slow leg (one request per tab), hence it goes last.
    try:
        kb = await asyncio.to_thread(orna_knowledge.refetch_now)
        lines.append(f"база знань: {kb['sections']} розділів, {kb['lines']} рядків")
    except Exception as e:
        logger.warning("update_codex: knowledge refetch failed", exc_info=True)
        lines.append(f"база знань: не вдалося оновити ({e})")
    if orna_pinecone.enabled():
        try:
            n = await asyncio.to_thread(orna_pinecone.index_corpus, "knowledge")
            lines.append(f"Pinecone: {n} фрагментів бази знань")
        except Exception as e:
            logger.warning("update_codex: pinecone reindex failed", exc_info=True)
            lines.append(f"Pinecone: не вдалося переіндексувати ({e})")

    try:
        bon = await asyncio.to_thread(orna_bonuses.refetch_now)
        lines.append(f"амітіси/крусібли: {bon['amities']} / {bon['crucibles']}")
    except Exception as e:
        logger.warning("update_codex: bonuses refetch failed", exc_info=True)
        lines.append(f"амітіси/крусібли: не вдалося оновити ({e})")

    try:
        mon = await asyncio.to_thread(orna_monuments.refetch_now)
        lines.append(f"монументи: тиждень {mon['week']}, {mon['rewards']} нагород")
    except Exception as e:
        logger.warning("update_codex: monuments refetch failed", exc_info=True)
        lines.append(f"монументи: не вдалося оновити ({e})")

    await message.reply_text("\n".join(lines))


def build_orna_handler() -> CommandHandler:
    return CommandHandler("orna", handle_orna)


def build_orna_callback_handler() -> CallbackQueryHandler:
    return CallbackQueryHandler(orna_callback, pattern=r"^orna\|")


def build_update_codex_handler() -> CommandHandler:
    return CommandHandler("update_codex", handle_update_codex)


def _demo() -> None:
    """Assert-checks for this module's PURE helpers - the ones with a bug
    history. Everything else here needs Telegram/Ollama/the codex, which is
    what the verifying-orna-changes harness is for. Run:
    `set -a && source .env && set +a && python3 telegram_orna.py`."""
    # _as_bool: bool("false") is True, which silently put a PVP request into
    # PVP mode and doubled the user's HP (live probe 2026-09-24).
    for value, want in ((True, True), (False, False), ("true", True), ("false", False),
                        ("False", False), ("0", False), ("no", False), ("ні", False),
                        ("", False), (None, False), (1, True), ("yes", True)):
        assert _as_bool(value) is want, (value, _as_bool(value), want)

    # _normalize_options: the model packs the whole list into one string, or
    # sends a bare string that would otherwise become one button per CHARACTER.
    assert _normalize_options(["['Клас', 'Тільки клас']"]) == ["Клас", "Тільки клас"]
    assert _normalize_options("Mage") == ["Mage"]
    assert _normalize_options(["Mage", "Thief"]) == ["Mage", "Thief"]
    assert _normalize_options(None) == [] and _normalize_options([]) == []
    assert _normalize_options(["  ", "Mage"]) == ["Mage"]
    assert _normalize_options(["plain, with a comma"]) == ["plain, with a comma"]

    # _reassign_class_pools: orna_classes' pool names are INVERTED from the
    # game's, so the model (taught the game's words) fills the two keys the
    # other way round. Both conventions must land in the right pool; the
    # returned pair is in orna_classes' OWN terms (spec = the tier-10 class).
    # Live 2026-09-27: an answer called Ranger and Sequencer "classes" and
    # listed them beside Summoner, which is a real one.
    tier10 = orna_classes.all_names("specialization")[0]      # e.g. "Beowulf"
    special = orna_classes.all_names("class")[0]              # e.g. "Apprentice"
    assert _reassign_class_pools(tier10, special) == (tier10, special)        # already right
    assert _reassign_class_pools(special, tier10) == (tier10, special)        # swapped -> fixed
    assert _reassign_class_pools(tier10, "") == (tier10, "")                  # class alone
    assert _reassign_class_pools("", tier10) == (tier10, "")                  # class in the spec key
    assert _reassign_class_pools(special, "") == ("", special)                # spec in the class key
    assert _reassign_class_pools("", special) == ("", special)                # spec alone
    assert _reassign_class_pools("", "") == ("", "")
    # "none" is an ANSWER for the tier-10 slot, never a misfiled name...
    assert _reassign_class_pools("none", special) == ("none", special)
    # ...and a name in NEITHER pool stays put, so the caller's own "that is not
    # a real name" message still names the field the model actually filled.
    assert _reassign_class_pools("Гільгармос", "") == ("Гільгармос", "")
    assert _reassign_class_pools("", "Дудар") == ("", "Дудар")

    # The two pools have to be DISJOINT for the above to be unambiguous at all.
    assert not (set(orna_classes.all_names("class")) & set(orna_classes.all_names("specialization")))

    # /clarify: continues only the CALLER's own session, frames an answer and a
    # follow-up differently (a follow-up read as "the answer to my question"
    # sent the loop hunting a question it never asked), budgets them
    # differently, and refuses an expired, missing or still-running session.
    advanced, replies = [], []
    real_advance = globals()["_advance"]

    async def _spy(sid_, message_, with_status=True):
        advanced.append(sid_)

    class _Msg:
        chat_id = 43
        from_user = type("U", (), {"id": 7})()

        async def reply_text(self, text, *a, **k):
            replies.append(text)

    class _Upd:
        effective_message = _Msg()

    class _Ctx:
        args = ["and", "for", "the", "head", "slot?"]

    globals()["_advance"] = _spy
    try:
        for asked, want_steps, want_phrase in ((False, MAX_STEPS, "FOLLOW-UP"),
                                               (True, _RESUME_STEPS, "USER ANSWER")):
            sid = _new_orna_session([{"role": "user", "content": "Q"}], 0)
            _ORNA_SESSIONS[sid].user_id = 7
            _USER_SESSIONS[7] = sid
            _arm_text_wait(sid, asked)
            advanced.clear()
            asyncio.run(handle_clarify(_Upd(), _Ctx()))
            sess = _ORNA_SESSIONS[sid]
            assert advanced == [sid], (asked, advanced)
            assert want_phrase in sess.messages[-1]["content"], (asked, sess.messages[-1]["content"][:90])
            assert sess.steps_left == want_steps, (asked, sess.steps_left, want_steps)
            assert sess.awaiting is False
            # still running -> refused, not double-driven
            sess.running = True
            advanced.clear()
            asyncio.run(handle_clarify(_Upd(), _Ctx()))
            assert advanced == [] and "обробляю" in replies[-1], replies[-1]
            sess.running = False
            # idle past the TTL -> gone
            sess.created -= SESSION_TTL_SECONDS + 1
            asyncio.run(handle_clarify(_Upd(), _Ctx()))
            assert advanced == [] and sid not in _ORNA_SESSIONS and 7 not in _USER_SESSIONS, replies[-1]
        # someone else's /clarify never reaches user 7's session
        sid = _new_orna_session([], 0)
        _USER_SESSIONS[7] = sid
        _Msg.from_user = type("U", (), {"id": 8})()
        asyncio.run(handle_clarify(_Upd(), _Ctx()))
        assert advanced == [] and "/orna" in replies[-1], replies[-1]
    finally:
        globals()["_advance"] = real_advance
        _USER_SESSIONS.clear()

    # the always-appended escape hatch must not be duplicated by the model's
    # own "Інше"/"Своя відповідь" option (live: two near-identical buttons).
    kept = [o for o in _normalize_options(["['Клас', 'Своя відповідь', 'Інше']"])
            if not _OTHER_OPTION_RE.search(o)]
    assert kept == ["Клас"], kept

    # _add_source: dedupe by URL, and reject anything that isn't http(s).
    src: list = []
    _add_source(src, "a", "https://example.com/x")
    _add_source(src, "same url", "https://example.com/x")
    _add_source(src, "bad scheme", "javascript:alert(1)")
    _add_source(src, "b", "https://example.com/y")
    assert [u for _l, u in src] == ["https://example.com/x", "https://example.com/y"], src

    # estimate_stats refuses a PARTIAL call. Every one of its wrong answers
    # came from quietly defaulting an input, so "missing" must survive as
    # missing - including the two that look falsy: AL 0 and pvp False are
    # real ANSWERS, and `or` would have collapsed both back into "not given".
    class _Silent:
        chat_id = 0

        async def reply_text(self, *a, **k):
            raise AssertionError("estimate_stats must post NOTHING on a partial call")

    class _Collect:
        chat_id = 0

        def __init__(self):
            self.posted = []

        async def reply_text(self, text, *a, **k):
            self.posted.append(text)

    # What is REQUIRED is `ascension_level` AND at least one of (a real
    # specialization -> base stats | items -> gear stats). `class`, `pvp` and
    # `items` are OPTIONAL and must NOT be demanded (relaxed 2026-09-25 when
    # the tool learned to do base stats without gear). These asserts drifted
    # from the code once already - the old version looped over all five fields
    # expecting a refusal for each, which made the whole module's self-check
    # unrunnable rather than catching anything.
    # class = the tier-10 CLASS, specialization = the passive package on top -
    # the game's words, which is what the tool now documents and reads.
    full = {"items": [{"name": "Lost Helmet"}], "class": "Gilgamesh",
            "specialization": "Duelist", "ascension_level": 0, "pvp": False}
    for field in ("items", "specialization", "pvp"):
        sink = _Collect()
        out = asyncio.run(_run_estimate_stats_tool(sink, {k: v for k, v in full.items() if k != field}))
        assert not out.startswith(_NEEDS_INPUT), f"{field} is OPTIONAL: {out[:120]}"
        assert len(sink.posted) == 1, (field, sink.posted)
    # ...and dropping the CLASS (the base-stats source) is fine too, as long as
    # items remain
    sink = _Collect()
    assert not asyncio.run(_run_estimate_stats_tool(
        sink, {k: v for k, v in full.items() if k != "class"})).startswith(_NEEDS_INPUT)

    # the two genuine refusals. The marker matters as much as the text:
    # _advance_inner keys the keep-listening-after-finish behaviour off it
    for label, partial, want in (
        ("no AL", {k: v for k, v in full.items() if k != "ascension_level"}, "- ascension_level:"),
        ("nothing to compute", {"class": "none", "specialization": "Duelist",
                                "ascension_level": 0, "pvp": False}, "and/or items"),
        ("empty args", {}, "- ascension_level:"),
    ):
        out = asyncio.run(_run_estimate_stats_tool(_Silent(), partial))
        assert out.startswith(_NEEDS_INPUT), (label, out[:80])
        assert want in out, (label, out[:200])
    # ...and a name that is not in the real pool is missing, not a warning
    # each bad name is reported under the field the model actually filled
    for field, bad in (("class", "Гільгармос"), ("specialization", "Дудар")):
        out = asyncio.run(_run_estimate_stats_tool(_Silent(), {**full, field: bad}))
        assert f"- {field}: {bad!r} is not" in out, out[:200]
    # an unparseable AL is reported, never silently 0
    out = asyncio.run(_run_estimate_stats_tool(_Silent(), {**full, "ascension_level": "AL 100"}))
    assert "- ascension_level:" in out and "'AL 100' is not one" in out, out[:200]
    # the ONE default: no quality means exactly "100%", not a forged item
    # quality and level are independent axes: every percentage used to come
    # back as level 1, so a player's upgraded gear was projected unupgraded.
    assert _parse_quality_spec("100") == (100, 1)          # the tool's default pair
    assert _parse_quality_spec("185% lv10") == (185, 10)
    assert _parse_quality_spec("legendary +10") == (140, 10)
    assert _parse_quality_spec("lv10") == (100, 10)        # level alone -> quality 100%
    assert _parse_quality_spec("godforged") == (100, 13)   # a forge name IS a level
    assert _parse_quality_spec("godforged lv12") == (100, 12), "an explicit level wins"
    assert _parse_quality_spec("185") == (185, 1) and _parse_quality_spec("zzz") is None

    # _name_candidates: the possessive goes BOTH ways. The codex spells some
    # names possessively and some not, and before 2026-09-25 a head that
    # already carried an apostrophe only produced garbage variants.
    assert "Ymir Brilliant Feathers" in _name_candidates("Ymir's Brilliant Feathers")
    assert not [c for c in _name_candidates("Ymir's Brilliant Feathers") if "''" in c or "'s's" in c]
    assert "Cupid's Locket" in _name_candidates("Cupid Locket")
    assert "Heretic's Robe" in _name_candidates("Heretics Robe")
    # a curly apostrophe (what a phone types) must reach the straight-quote codex name
    assert "Cupid's Locket" in _name_candidates("Cupid\u2019s Locket")

    # _names_observation: the observation a search/query hands the MODEL must
    # never be silently shorter than the count it states. Live 2026-09-25:
    # "13 results for 'Judge Trifecta'" listed 5 names, the model opened those
    # 5, and answered for all 13 - missing 4 valhallan_summoner pieces.
    few = [{"name": f"n{i}", "url": f"/u/{i}/"} for i in range(3)]
    assert _names_observation(few).count(";") == 2 and "more" not in _names_observation(few)
    many = [{"name": f"n{i}", "url": f"/u/{i}/"} for i in range(_MAX_NAMES_IN_OBSERVATION + 8)]
    obs = _names_observation(many)
    assert "+8 MORE not listed" in obs and "PARTIAL" in obs, obs[-120:]
    assert obs.count(";") == _MAX_NAMES_IN_OBSERVATION - 1
    assert _names_observation([]) == ""

    # GUARDRAIL for the rule this violated ("a tool puts what the model must
    # reason with in its OBSERVATION, not only in the message it posted" -
    # CLAUDE.md). The rule was already written down and got violated anyway, so
    # pin it structurally instead: every tool that posts a result LIST must
    # build its observation through the one honest helper, and must not
    # re-introduce a bare truncating slice of its own. A new list-returning
    # tool belongs in this tuple.
    import inspect
    import re as _re
    for fn in (_run_codex_search, _run_query_tool):
        src = inspect.getsource(fn)
        assert "_names_observation(" in src, (
            f"{fn.__name__} must build its name list via _names_observation - see CLAUDE.md's "
            "observation rule; a silent truncation there is invisible to the model")
        # e.g. `for r in results[:5]` / `for e in entries[:10]` feeding a join
        bad = _re.findall(r"for \w+ in (?:results|entries|matches|desc_entries)\[:\d+\]", src)
        assert not bad, (fn.__name__, bad, "truncate inside _names_observation, not here")

    # _check_loadout: a stat total for a character nobody can build is worse
    # than no answer. Live 2026-09-25: a two-handed archistaff was summed
    # together with an off-hand. The 0.65 dual-wield factor is the guild's
    # statement of game behaviour, not derivable from the codex - pinned here
    # for the same reason orna_classes pins the AL/PVP rules.
    w = lambda n, place, th=False: {"name": n, "place": place, "two_handed": th}
    conflicts, dual = _check_loadout([w("Celestial Archistaff", "weapon", True), w("North Star", "off-hand")])
    assert conflicts and not dual and "TWO-HANDED" in conflicts[0], conflicts
    assert _check_loadout([w("A", "weapon"), w("B", "weapon")]) == ([], True), "two 1H weapons dual-wield"
    assert _check_loadout([w("Celestial Archistaff", "weapon", True)]) == ([], False), "a 2H alone is legal"
    assert _check_loadout([w("A", "weapon"), w("S", "off-hand")]) == ([], False), "1H + off-hand is legal"
    over = _check_loadout([w("R1", "accessory"), w("R2", "accessory"), w("R3", "accessory")])[0]
    assert over and "only 2 fit" in over[0], over
    assert _check_loadout([w("H1", "head"), w("H2", "head")])[0], "two helmets must conflict"
    assert _check_loadout([w("X", "weapon", True), w("Y", "weapon", True)])[0]
    assert _check_loadout([]) == ([], False)
    # Only ONE celestial weapon per player - a top-stat-per-slot search reaches
    # for two, since celestials head most rankings.
    cel = lambda n: {"name": n, "place": "weapon", "two_handed": False, "celestial": True}
    two_cel = _check_loadout([cel("Celestial Staff"), cel("Celestial Quarterstaff")])
    assert two_cel[0] and "only ONE celestial" in two_cel[0][0], two_cel
    assert not two_cel[1], "an illegal pair must not also be reported as dual-wielding"
    assert _check_loadout([cel("Celestial Staff"), w("Fey Macha Pillar", "weapon")]) == ([], True), \
        "one celestial + one ordinary weapon is a legal dual wield"
    assert _check_loadout([cel("Celestial Archistaff")]) == ([], False)
    assert _DUAL_WIELD_FACTOR == 0.65

    # Confidence gate: the model's number is clamped by what the loop verified,
    # so a confident answer built on nothing cannot present itself as one.
    assert _parse_confidence({"confidence": 90}) == 90
    assert _parse_confidence({"args": {"confidence": "82%"}}) == 82, "nested + percent sign"
    assert _parse_confidence({"confidence": 0.9}) == 90, "a 0-1 float means a fraction"
    assert _parse_confidence({"confidence": "not a number"}) is None
    assert _parse_confidence({}) is None
    assert _parse_confidence({"confidence": 140}) == 100 and _parse_confidence({"confidence": -5}) == 0

    class _S:
        def __init__(self, calls, request="", pushed=False):
            self.seen_calls = calls
            self.original_request = request
            self.pushed_for_evidence = pushed

    # _forced_evidence_note: a HOW-MANY question that made no tool call at all
    # is sent back ONCE. Live 2026-10-04: "how many items are in the codex?"
    # finished with a confident, memory-sourced "2773" and an empty action
    # trace 3/3 with sql() fully described in the prompt.
    def _agg_session(request, seen=None, pushed=False):
        return _S(seen or {}, request, pushed)

    assert _looks_aggregation_request("how many items are in the codex?")
    assert _looks_aggregation_request("скільки предметів у кодексі?")
    assert _looks_aggregation_request("what's the average attack of celestials")
    # Must NOT fire where no tool call is legitimate, or the guard would force
    # a pointless step onto every greeting and capability question.
    assert not _looks_aggregation_request("what can you do?")
    assert not _looks_aggregation_request("дякую!")
    assert not _looks_aggregation_request("balor sword")
    assert _forced_evidence_note(_agg_session("how many items are in the codex?")), "a bare count must be pushed back"
    # ...but only once, and never when evidence already exists.
    assert _forced_evidence_note(_agg_session("how many items?", pushed=True)) is None, "must be one-shot"
    assert _forced_evidence_note(_agg_session("how many items?", seen={"sql …": "2773"})) is None, \
        "a count WITH a tool observation must pass straight through"
    assert _forced_evidence_note(_agg_session("balor sword")) is None, "non-aggregation asks are untouched"

    # The LIVE failure of 2026-10-04, reproduced from its own log: the model
    # was refused on an empty-conditions query, added sort_by=attack to make
    # the call legal, got a capped 50-row listing, and answered "there are 50
    # items in the Orna codex" at confidence 82. A tool HAD been called, so
    # the original zero-tool-call guard let it straight through.
    capped = {"query items attack": "2773 matches for [items] attack (PARTIAL: showing the first 50 of 2773): "
                                    "Celestial Axe [attack=410]; Celestial Bow [attack=410]"}
    assert _forced_evidence_note(_agg_session("how many items are in the codex?", seen=capped)), \
        "counting a PARTIAL listing must be sent back - this is the 50-vs-2773 bug"
    # ...but a REAL count passes through, however it was obtained.
    for ok in ("COUNT for [items] everything in the codex = 2773.",
               "1 row(s) for: SELECT count(*) FROM records\n2773",
               "9 row(s) for: SELECT category, count(*) FROM records GROUP BY category"):
        assert _forced_evidence_note(_agg_session("how many items?", seen={"c": ok})) is None, ok
    # And a COMPLETE (non-PARTIAL) observation is not second-guessed, so a
    # "how much defense does X have" ask does not burn a step on a count.
    assert _forced_evidence_note(_agg_session(
        "how much defense does the Lost Helmet have?",
        seen={"assess": "Lost Helmet: defense 318 at lv1, 726 at lv10"})) is None, \
        "a complete observation must not be pushed back"
    _once = _agg_session("how many items are in the codex?")
    assert _forced_evidence_note(_once) and _forced_evidence_note(_once) is None, "flag must latch"

    # monuments: a category cell must NOT carry its "Reward N" slot label - live
    # 2026-10-05 "Reward 3: Proofs" was read as "3 proofs" and the answer
    # invented quantities the chart does not have. Specific items keep theirs.
    _ml = _monument_line([
        {"slot": "Reward 3", "kind": "category", "name": "Proofs", "value": "Proofs"},
        {"slot": "Material", "kind": "material", "name": "Perfect Runestone", "value": "P Runestone"}])
    assert _ml == "Proofs, Material: Perfect Runestone (P Runestone)", _ml
    assert "monuments" in _ACTIONS
    _fl = _monument_floor_cells([
        {"slot": "Reward 1", "kind": "category", "name": "Arena Tokens", "value": "Arena Tokens"},
        {"slot": "Reward 2", "kind": "category", "name": "Materials", "value": "Materials"},
        {"slot": "Material", "kind": "material", "name": "Red Draconite", "value": "Red Draconite"},
        {"slot": "Potion", "kind": "potion", "name": "Nostrum", "value": "Nostrum"}], uk=True)
    assert _fl == "Жетон арени, Червоний драконіт, Прополіс", _fl
    assert [_mon_label(c, True) for c in ("Skeleton Keys", "Orns", "Armor", "Proofs")] == \
        ["Ключ", "Орни", "Обладунки", "Відзнаки"]
    assert _mon_label("Skeleton Keys", False) == "Skeleton Keys"
    assert _is_pure_product("(1 + 0.575) * (1 + 0.575)") and _is_pure_product("21.757 * 1.25 * 2")
    assert not _is_pure_product("(1 + 0.575) * (1 + 0.575) - 1"), "already the bonus, not a multiplier"
    assert not _is_pure_product("((1 + 57.5/100) * (1 + 57.5/100) - 1) * 100"), "already a percent"
    assert not _is_pure_product("3 + 4")
    assert _uk_date("October 5") == "5 жовтня" and _uk_date("Febuary 3") == "Febuary 3"
    assert ("Червоний драконіт", "Red Draconite") in _uk_material_names("де взяти червоний драконіт?")
    assert ("Червоний драконіт", "Red Draconite") in _uk_material_names("скільки червоного драконіту треба")
    assert not _uk_material_names("що зараз в монументах?")
    assert _plain("<b>a &amp; b</b>") == "a & b"
    # the closing line is the model's call: empty finish after a posted card -> none
    class _LO:
        posted_note, last_post = "posted", object()
    assert _listing_only(_LO, "") and _listing_only(_LO, "  ")
    assert not _listing_only(_LO, "Demeter gives proofs on the most floors - 8.")
    _LO.last_post = None
    assert not _listing_only(_LO, ""), "nothing posted -> an empty finish is not an answer"
    # a pasted observation is caught; a normal answer quoting a value is not
    class _LS:
        seen_calls = {"m": "MONUMENT REWARDS, week 41 (floorchart.top, community-entered each week; matched "
                           "everything).\n- Ithra floor 2: Materials"}
    assert _leaked_observation(_LS, "MONUMENT REWARDS, week 41 (floorchart.top, community-entered each week; x")
    assert not _leaked_observation(_LS, "Ithra floor 2 gives Materials this week (week 41).")
    assert _FOREIGN_SCRIPT_RE.search("Де미터") and not _FOREIGN_SCRIPT_RE.search("Demeter, Деметра ✓ 41%")
    # Every action must have its "- name(" bullet in the prompt. monuments lost
    # its description when a neighbouring tool's span was cut out (2026-10-05),
    # and nothing noticed: the model still saw the bare name in _STEP_TOOLS,
    # called it with no arguments, and answered with nothing to show.
    _described = set(re.findall(r"- (\w+)\(", _orna_system_prompt("x")))
    _undescribed = [a for a in _ACTIONS if a not in _described]
    assert not _undescribed, f"actions with no description in the prompt: {_undescribed}"

    assert _evidence_ceiling(_S({}))[0] == 40, "no tool call means nothing was verified"
    assert _evidence_ceiling(_S({"a": "0 results for 'x'"}))[0] == 60, "all dead ends"
    assert _evidence_ceiling(_S({"a": "open_entry did NOT run: bad url"}))[0] == 60
    assert _evidence_ceiling(_S({"a": "0 results", "b": "Vritra Charm: Tier 6"}))[0] == 100
    # a real observation must not be mistaken for a dead end
    assert _evidence_ceiling(_S({"a": "posted a stats estimate [base+items]"}))[0] == 100
    banner_uk = _low_confidence_banner(40, "no tool was called", True)
    banner_en = _low_confidence_banner(40, "no tool was called", False)
    assert "Не можу відповісти впевнено" in banner_uk and "поріг" in banner_uk
    assert "I don't know this reliably" in banner_en and str(_CONFIDENCE_FLOOR) in banner_en
    assert _CYRILLIC_RE.search(banner_uk) and not _CYRILLIC_RE.search(banner_en)
    # It must RENDER as bold, not merely contain a bold marker. Live report
    # 2026-10-05: the banner was written with a literal `<b>` and the user saw
    # "<b>I don't know this reliably</b>" in the chat, because the finish
    # answer goes through _reply_markdown -> telegram_go._markdown_to_html,
    # which escapes <, > and & before Telegram ever sees them. Asserting the
    # banner's TEXT (as the two lines above do) could not catch that - only
    # running it through the real converter can, which is why this pins the
    # PIPELINE. If _markdown_to_html ever stops supporting **bold**, this
    # fails instead of the chat quietly filling with tag soup.
    from telegram_go import _markdown_to_html as _md
    for _b in (banner_uk, banner_en):
        _rendered = _md(_b)
        assert "<b>" in _rendered and "&lt;b&gt;" not in _rendered, _rendered
    # ...and the same through the whole gate, which is what actually ships.
    _gated, _ = _confidence_gate(_S({}), {"confidence": 20}, "Partial answer.")
    _gated_html = _md(_gated)
    assert "<b>" in _gated_html and "&lt;" not in _gated_html.split("</b>")[0], _gated_html
    assert _gated_html.rstrip().endswith("Partial answer."), "the partial answer must stay BELOW the banner"

    # The BROWSE tools must post nothing and record instead: a request that read
    # twelve entries used to post twelve cards and bury its own answer. A card
    # arriving from a tool is the regression to catch, so this asserts on a
    # message stub that records every send.
    class _Spy:
        chat_id = 0

        def __init__(self):
            self.sent = []

        def __getattr__(self, name):
            async def rec(*a, **k):
                self.sent.append(name)
                return _Spy()
            return rec

    class _Sess:
        def __init__(self):
            self.viewed_entries = []
            self.sources = []

    spy, sess = _Spy(), _Sess()
    obs = asyncio.run(_run_tool(spy, "open_entry", "/codex/items/vritra-charm/", {}, sess.sources, sess))
    assert spy.sent == [], f"open_entry must post nothing, sent {spy.sent}"
    assert obs.startswith("Vritra Charm"), obs[:60]
    assert len(sess.viewed_entries) == 1 and sess.viewed_entries[0]["url"].endswith("vritra-charm/")
    # ...and a second read of the same url must not duplicate the button entry
    asyncio.run(_run_tool(spy, "open_entry", "/codex/items/vritra-charm/", {}, sess.sources, sess))
    assert len(sess.viewed_entries) == 1, sess.viewed_entries
    # ...and it is flagged READ, so finish() cards it ahead of names a search
    # merely listed (a "what is this item" lookup shows the item, not every
    # near-name hit).
    assert sess.viewed_entries[0].get("opened") is True, sess.viewed_entries

    spy2, sess2 = _Spy(), _Sess()
    obs2 = asyncio.run(_run_tool(spy2, "search_codex", "Judge Trifecta", {}, sess2.sources, sess2))
    assert spy2.sent == [], f"search_codex must post nothing, sent {spy2.sent}"
    assert "results for" in obs2 and sess2.viewed_entries, obs2[:60]
    assert not any(e.get("opened") for e in sess2.viewed_entries), "a search LISTS, it does not read"
    # the recorder is capped, or a 50-row query builds an unusable keyboard
    big = [{"name": f"n{i}", "url": f"/codex/items/n{i}/"} for i in range(200)]
    sess3 = _Sess()
    _remember_entries(sess3, big)
    assert len(sess3.viewed_entries) == _MAX_VIEWED_ENTRIES, len(sess3.viewed_entries)
    _remember_entries(None, big)          # must not raise without a session

    # A misspelled codex name must be CORRECTED, not answered with "no such item
    # exists". Live: "what crest of feeling does?" - the real item is `Crest of
    # the Felling`, one letter away - and the loop denied it existed while its
    # own search for "crest" had listed the name.
    spy4, sess4 = _Spy(), _Sess()
    obs4 = asyncio.run(_run_tool(spy4, "search_codex", "crest of feeling", {}, sess4.sources, sess4))
    assert "Crest of the Felling" in obs4, obs4[:150]
    assert "misspelling" in obs4 and "SAY that is how you read" in obs4, obs4[:150]
    assert sess4.viewed_entries, "the corrected result must still reach the entries button"
    # ...and a query that is not a name at all must NOT be corrected into one
    import orna_aussies as _aussies
    for not_a_name in ("what is the best weapon", "how do i level up", "mag > 250", "sword"):
        assert _aussies.fuzzy_codex_name(not_a_name) == "", not_a_name
    # a one-letter typo is corrected near-exactly (before the word-dropping
    # ladder can turn "Red Dragonite" into plain Dragonite), case-insensitively;
    # a real name is never "corrected"
    assert _aussies.fuzzy_codex_name("red dragonite", _NEAR_EXACT_CUTOFF) == "Red Draconite"
    for real in ("Dragonite", "dragonite", "Red Draconite", "Balor Sword"):
        assert _aussies.fuzzy_codex_name(real, _NEAR_EXACT_CUTOFF) == "", real

    # English-first pipeline: the loop reasons in English and the gates sit at
    # the edges. _detect_lang is what both keys off.
    assert _detect_lang("що сьогодні є") == "Ukrainian"
    assert _detect_lang("what is today") == "English"
    assert _detect_lang("") == "English" and _detect_lang("balor sword") == "English"
    # a mixed message counts as Ukrainian - any Cyrillic means the user wrote it
    assert _detect_lang("покажи Judge Trifecta Falx") == "Ukrainian"
    # the prompt must now demand English, unconditionally, and must NOT carry the
    # old per-request Ukrainian lock
    for probe in ("що сьогодні є", "what is today"):
        prompt = _orna_system_prompt(probe)
        assert "write EVERY thought, tool argument, ask and finish answer in English" in prompt, probe
        assert "MUST be written in Ukrainian" not in prompt, probe
    assert "PROPER NAMES ARE NEVER TRANSLATED" in _orna_system_prompt("x")

    # _proper_nouns: what the translator is told to keep character-for-character.
    # Every name it returns MUST be verbatim in the source, or "keep this exactly"
    # is unsatisfiable and every translation burns a retry - which is what
    # "Altar of Ascension" -> "Altar Ascension" did.
    for probe in ("You should buy Grand Summoner first, then the Altar of Ascension.",
                  "Judge Trifecta Maximus drops items for Warrior and Thief classes.",
                  "Duelist (tier 5 class) gives defense -5%"):
        for name in _proper_nouns(probe):
            assert name in probe, (name, probe)
    assert "Altar of Ascension" in _proper_nouns("then the Altar of Ascension.")
    assert "Duelist" in _proper_nouns("Duelist (tier 5 class) gives defense -5%")
    assert "Judge Trifecta Maximus" in _proper_nouns("Judge Trifecta Maximus drops 12 items")
    # prose with no identifier must pin nothing, so the instruction stays signal
    assert _proper_nouns("I do not know this reliably. The tools came back empty.") == []
    assert _proper_nouns("") == []

    # the gate itself: claim vs evidence, and which language the admission takes
    class _Sess:
        def __init__(self, calls, text="what is X?"):
            self.seen_calls = calls
            self.messages = [{"role": "system", "content": "s"}, {"role": "user", "content": text}]
    fin = lambda c: {"action": "finish", "action_input": "The answer is 42.", "confidence": c}
    real = {"a": "Vritra Charm: Tier 6; Rarity: Legendary"}
    out, eff = _confidence_gate(_Sess({}), fin(95), "The answer is 42.")
    assert eff == 40 and "I don't know this reliably" in out and out.endswith("The answer is 42."), out[:80]
    out, eff = _confidence_gate(_Sess({"a": "0 results for 'x'"}), fin(95), "A.")
    assert eff == 60 and "I don't know" in out, (eff, out[:60])
    out, eff = _confidence_gate(_Sess(real), fin(95), "A.")
    assert eff == 95 and out == "A.", "good evidence + high claim passes through untouched"
    out, eff = _confidence_gate(_Sess(real), fin(50), "A.")
    assert eff == 50 and "I don't know" in out, "the model's own low claim is honoured"
    out, eff = _confidence_gate(_Sess(real), {"action": "finish"}, "A.")
    assert eff == 100 and out == "A.", "no number + real evidence must not be penalised"
    out, eff = _confidence_gate(_Sess({}, "що таке X?"), fin(90), "Відповідь.")
    assert "Не можу відповісти впевнено" in out, "the admission follows the user's language"

    # open_entry only opens playorna codex pages, and the model twice invented
    # something else - a path built from an item name (space included) and a
    # playerecho citation url from knowledge_search. Both raised a traceback and
    # wasted a step; one answered from invention afterwards.
    assert _codex_path_problem("/codex/items/vritra-charm/") == ""
    assert _codex_path_problem("/codex/items/vritra-charm") == ""
    assert _codex_path_problem("https://playorna.com/codex/items/vritra-charm/") == ""
    assert "not a codex path" in _codex_path_problem("/codex/items/vritra charm/")
    assert "not a playorna codex page" in _codex_path_problem("https://playerecho.com/orna/circle-of-anguish")
    assert "citation link" in _codex_path_problem("https://playerecho.com/orna/ward-guide")
    assert _codex_path_problem("") and _codex_path_problem("Vritra Charm")

    # Class/spec abilities must be DISCOVERED from the codex, not hand-written
    # per specialization: orna_classes.json has passiveEffects for 13 classes
    # and none of the tier-10 specs, so an estimate for Gilgamesh/Deity/Heretic
    # listed no passives at all before this.
    import orna_aussies as _aussies
    for spec in ("Heretic Ara", "Gilgamesh", "Deity", "Grand Summoner", "Beowulf"):
        found = _aussies.class_abilities(spec)
        assert found, f"no abilities discovered for {spec}"
        assert any(a["description"] for a in found), f"{spec}: abilities carry no descriptions"
    # a gendered-pair name resolves from either side ("Beowulf / Bestla")
    assert _aussies.class_abilities("Bestla"), "the second name of a gendered pair must resolve"
    assert _aussies.class_abilities("no such class at all") == []

    # --- shared knowledge aggregator (research + knowledge_search reuse it) ---
    kg = asyncio.run(_gather_knowledge("factions"))
    assert "GAME MECHANICS" in kg, kg[:200]                 # mechanics corpus still wired
    assert asyncio.run(_gather_knowledge("xyzzy plugh frobnicate")) == ""   # honest empty

    # --- research tool: wiring + one-call observation ---
    # Self-contained stubs (the _Spy/_Sess names above are shadowed by later
    # redefinitions in this _demo, so define fresh ones here).
    class _RSpy:
        def __init__(self): self.sent = []
        def __getattr__(self, name):
            async def rec(*a, **k): self.sent.append(name); return None
            return rec
    class _RSess:
        def __init__(self): self.viewed_entries = []; self.sources = []

    assert "research" in _ACTIONS
    assert "research" in [t["function"]["name"] for t in _STEP_TOOLS]
    spyR, sessR = _RSpy(), _RSess()
    obsR = asyncio.run(_run_tool(spyR, "research", "Fallen King Centaurus", {}, sessR.sources, sessR))
    assert "Cretan Compound Bow" in obsR and "useable_by=all_classes" in obsR, obsR[:400]
    assert "Drops" in obsR and "Skills" in obsR, obsR[:400]
    assert spyR.sent == [], f"research must post nothing, sent {spyR.sent}"
    assert sessR.viewed_entries, "research must record entities for finish() buttons"
    # capped relation is marked PARTIAL, never reads complete
    sessC = _RSess()
    obsR2 = asyncio.run(_run_tool(_RSpy(), "research", "Fallen King Centaurus",
                                  {"per_relation_cap": 2}, sessC.sources, sessC))
    assert "PARTIAL" in obsR2, obsR2[:400]
    # unresolved subject is honest, no crash
    sessU = _RSess()
    obsR3 = asyncio.run(_run_tool(_RSpy(), "research", "zzzptqx nothing here", {}, sessU.sources, sessU))
    assert "could not resolve" in obsR3.lower(), obsR3[:200]
    # a class/spec name sent to research (it collides with a same-named boss:
    # "Gilgamesh" -> the boss) is flagged and redirected, not answered as codex.
    sessG = _RSess()
    obsG = asyncio.run(_run_tool(_RSpy(), "research", "Gilgamesh", {}, sessG.sources, sessG))
    assert "class/specialization" in obsG and "estimate_stats" in obsG, obsG[:200]
    # research renders a follower's bestial_bond (its defining data)
    _rb = _render_supergraph({"entities": [{"category": "followers", "id": "x", "name": "X",
        "facts": {}, "alternatives": [], "relations": [],
        "bond": ["tier 1: orn bonus +50, grants Rainsong"]}], "unresolved": []})
    assert "Bestial Bond" in _rb and "orn bonus +50" in _rb, _rb
    # a bare closer in the follow-up window must NOT resume the loop; a real
    # follow-up (even one that starts with "thanks,") still must.
    for _p in ("thank you", "thanks", "дякую", "thanks a lot", "ok cool",
               "thank you very much", "ty!", "🙏", "спасибі 🙂"):
        assert _is_pleasantry(_p), _p
    for _q in ("when does the event end", "thanks, and when does it end",
               "last martyr", "балоріт 100"):
        assert not _is_pleasantry(_q), _q
    _msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": f"{_USER_QUESTION}\nmagic of 200% godforged staff?"},
             {"role": "assistant", "content": json.dumps({"action": "assess", "action_input": "Staff"})},
             {"role": "user", "content": _tool_result(1, "assess", "Staff", {"quality": "200% godforged"}, "magic=500")},
             {"role": "assistant", "content": json.dumps({"action": "finish", "action_input": "Magic is 500."})},
             {"role": "user", "content": f"{_USER_FOLLOW_UP}\nadd 20% to it"}]
    _ws = _working_state(_msgs)
    assert "USER QUESTION: magic of 200%" in _ws and "#1 assess 'Staff'" in _ws and "magic=500" in _ws
    assert "YOUR ANSWER (already shown to the user): Magic is 500." in _ws
    assert _ws.index("Magic is 500") < _ws.index("USER FOLLOW-UP: add 20%")
    assert _with_state(_msgs)[-1]["content"] == _ws and len(_msgs) == 6   # brief is never stored
    assert _parse_quality_spec("200% godforged") == (200, 13) == _parse_quality_spec("godforged 200")
    assert _parse_quality_spec("185% lv10") == (185, 10) and _parse_quality_spec("200 junk") is None
    _pr = type("R", (), {"stats": {"magic": type("SR", (), {"base": 100, "values": [150.0, 200.0, 250.0]})()}})()
    assert "magic=250" in _projection_observation(_pr, 3) and "base 100" in _projection_observation(_pr, 3)
    # the command reference is its own evidence; memory-only prose is still gated
    _cs = type("S", (), {"seen_calls": {}, "messages": [], "user_lang": "English"})()
    _ref = "Send the screenshot captioned Red 4, then /amity lists it."
    assert _confidence_gate(_cs, {"confidence": 95}, _ref)[0] == _ref
    assert _confidence_gate(_cs, {"confidence": 95}, "Balor Sword has 300 attack")[1] < _CONFIDENCE_FLOOR
    assert not any(c in _capabilities_text() for c in ("/go", "/stats", "/ban", "/update_codex"))
    # ephemeral status shows the tool's key argument, not just "searching…"
    assert _status_detail("search_codex", "Judge Trifecta Falx", {}) == "Judge Trifecta Falx"
    assert _status_detail("research", "", {"entities": ["Fallen King Centaurus", "X"]}).startswith("Fallen King Centaurus")
    assert _status_detail("query", "", {"category": "items",
        "conditions": [{"field": "magic", "cmp": ">", "value": 250}]}) == "items, magic > 250"
    assert _status_detail("assess", "", {"item": "Lost Helmet", "quality": "185%"}) == "Lost Helmet 185%"
    assert _status_detail("towers", "", {}) == ""       # no args -> label only
    # a real entity name can contain "," or "and" - resolve the WHOLE string
    # first, don't shred it (live bug: "Arisen Thor, the Storm God" split into a
    # wrong item + an unresolved half; "Sword and Shield" was never found).
    for whole in ("Arisen Thor, the Storm God", "Sword and Shield"):
        sW = _RSess()
        oW = asyncio.run(_run_tool(_RSpy(), "research", whole, {}, sW.sources, sW))
        assert "could not resolve" not in oW.lower(), (whole, oW[:160])
        assert len(sW.viewed_entries) == 1, (whole, sW.viewed_entries)
    # ...but two genuine entities joined by "and" still split into both
    sT = _RSess()
    asyncio.run(_run_tool(_RSpy(), "research",
                          "Fallen King Centaurus and Judge Trifecta Maximus", {}, sT.sources, sT))
    assert len(sT.viewed_entries) == 2, sT.viewed_entries
    # research must NOT silently drop leaf stats/effects - real items have up to
    # 18 stats / 21 effects, and a "which class benefits" answer needs them all.
    fat = {"category": "items", "id": "x", "name": "X", "useable_by": "all_classes",
           "place": "weapon", "item_type": "weapon", "tier": 10, "rarity": "rare",
           "stats": {f"s{i}": i for i in range(15)}, "effects": [f"gives:E{i}" for i in range(9)]}
    line = _leaf_line(fat)
    assert "s14" in line and "s9" in line, "all 15 stats must show (no silent [:10] cap)"
    assert "E8" in line, "all 9 effects must show (no silent [:6] cap)"
    # truncation cuts at a LINE boundary, never mid-stat: "attack 244" clipped to
    # "attack 24" would be a WRONG number, worse than missing data.
    _orig = "• a — attack 244\n• b — magic 300\n• c — hp 500"
    _tr = _truncate_lines(_orig, 20, "\n[MORE]")
    assert all(ln in _orig.split("\n") for ln in _tr.split("\n[MORE]")[0].split("\n")), _tr

    # research is advertised in the system prompt (a tool undescribed is unused)
    _p = _orna_system_prompt("what does Fallen King Centaurus drop")
    assert "research(action_input=" in _p, "research must be described in the prompt or the model won't use it"

    print("telegram_orna: all checks passed")


if __name__ == "__main__":
    _demo()
