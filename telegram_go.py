"""
telegram_go.py
================
Hidden `/go` command. Never registered in any bot command menu (the bot
doesn't call setMyCommands at all), so it's invisible to the Orna users this
bot is otherwise for.

Built for the "Telegram is the only thing that works" case - flaky/metered
plane wifi - so on top of the ReAct loop (search / youtube / open / ask /
finish) it:
  - never downloads a video blind: it looks up title+duration first and
    makes the user tap Video/Audio/Skip before spending any bandwidth,
  - offers audio-only, which is a fraction of the size of 360p video,
  - lets the model ask a clarifying question via tappable buttons instead
    of guessing (no free-text follow-up there - see _advance's "ask"
    branch for why),
  - replays already-fetched search images/sources via buttons instead of
    re-querying Tavily/Ollama/DuckDuckGo,
  - lets the user tap "Continue" on a finished reply to send free-text
    and/or a photo back into that same conversation (see
    _PENDING_CONTINUE below for how this coexists safely with the
    Orna bot's own text/photo handlers).

Only GO_ALLOWED_USER_IDS may use it - anyone else's /go is silently
ignored, so a curious Orna user poking at slash commands can't spend your
Tavily/Ollama Cloud credits or bandwidth.
"""
from __future__ import annotations

import ast
import asyncio
import base64
import html
import json
import logging
import operator
import os
import re
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote, urljoin

import httpx
from bs4 import BeautifulSoup
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import TelegramError
from telegram.ext import CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

from ollama_client import OllamaError, UnsupportedMultimodal, chat_json, chat_json_with_fallback, drop_images
from telegram_nlp import OLLAMA_HOST as LOCAL_OLLAMA_HOST, OLLAMA_MODEL as LOCAL_OLLAMA_MODEL
import usage_stats

# Kept as an alias so the rest of this file's existing _UnsupportedMultimodal
# references don't all need renaming - see ollama_client.py for why a non-
# dict JSON parse result is also now a hard OllamaError, not a silent {}.
_UnsupportedMultimodal = UnsupportedMultimodal

logger = logging.getLogger(__name__)

OLLAMA_API_KEY = os.environ.get("OLLAMA_API_KEY")
GO_MODEL = os.environ.get("GO_MODEL", "gemma4:31b")
# /go nsfw ...: stays fully local (this GGUF isn't a cloud model, and a
# hosted service would likely refuse this content anyway) and searches via
# DuckDuckGo instead of Tavily, so it needs neither cloud API key.
NSFW_MODEL = os.environ.get("NSFW_MODEL", "igorls/gemma-4-12B-it-heretic-GGUF")
TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY")
YTDLP_PATH = os.environ.get("YTDLP_PATH", os.path.expanduser("~/yt-dlp_macos"))
FFMPEG_PATH = os.environ.get("FFMPEG_PATH", "/opt/homebrew/bin/ffmpeg")
FFPROBE_PATH = os.environ.get("FFPROBE_PATH", "/opt/homebrew/bin/ffprobe")

# Telegram user ids allowed to use /go (comma-separated). Get yours from
# @userinfobot. Empty = anyone who discovers the hidden command can use it.
GO_ALLOWED_USER_IDS = {
    int(x) for x in os.environ.get("GO_ALLOWED_USER_IDS", "").split(",") if x.strip()
}

MAX_STEPS = 6
MAX_VIDEO_MB = 50
MAX_AUDIO_MB = 20
AUDIO_BITRATE_KBPS = 64
# (height, minimum kbps to still look watchable at that height, bitrate
# ceiling so a short clip doesn't get bloated up to the full budget).
QUALITY_LADDER = [(360, 250, 1200), (240, 150, 700), (144, 80, 400)]
# No duration can fit MAX_VIDEO_MB below the ladder's bottom tier's
# minimum bitrate - so anything longer than this can never succeed no
# matter what, and _propose_youtube checks it *before* downloading
# anything: the single biggest lever on disk/bandwidth use is simply
# never starting a download that was always going to be rejected.
MAX_VIDEO_DURATION_SECONDS = int((MAX_VIDEO_MB * 8 * 1024) / (QUALITY_LADDER[-1][1] + AUDIO_BITRATE_KBPS))
# Worst-case bandwidth/disk breaker for the initial download - generous
# but no longer unbounded, since the duration gate above already rules out
# anything that couldn't fit. yt-dlp's --max-filesize checks each
# video/audio fragment independently before merging, and a cap that's too
# tight here silently drops just one side of the merge (see _has_audio's
# docstring) - so this stays well above what a <=480p, <=MAX_VIDEO_DURATION
# source should realistically need, as a backstop rather than the real cap.
FRAGMENT_SAFETY_MB = 500
# ponytail: fixed TTL + a hard cap, pruned opportunistically on each new
# /go call. No persistence, no real LRU - add if session volume ever
# outgrows one process's memory between bot restarts.
SESSION_TTL_SECONDS = 15 * 60
MAX_SESSIONS = 50
# Continuing a finished /go reply keeps this many of its most recent
# messages (system prompt always kept, plus this many of the rest) rather
# than the full unbounded history.
MAX_CONTEXT_MESSAGES = 20
# How long a tapped "Continue" button stays armed before a stray later
# message in the chat stops being treated as a continuation.
CONTINUE_TTL_SECONDS = 10 * 60

_TAVILY_TIMEOUT = httpx.Timeout(connect=10.0, read=45.0, write=10.0, pool=10.0)

# Nothing in the system prompt asks for Markdown, but the model writes it
# anyway often enough (**bold**, "# Heading", bullet lists) - and every
# reply_text() in this file sends plain text with no parse_mode, so that
# syntax was showing up completely literally instead of rendering. Convert
# to Telegram's own HTML parse mode instead of MarkdownV2: HTML only needs
# a handful of characters escaped (<, >, &) versus MarkdownV2's much wider
# escape set, which is the same reasoning every other HTML-rendering reply
# in this codebase (telegram_orna.py, telegram_resources.py) already uses.
_CODE_BLOCK_RE = re.compile(r"```(?:\w+\n)?(.*?)```", re.S)
_INLINE_CODE_RE = re.compile(r"`([^`\n]+?)`")
_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^\s)]+)\)")
_HEADING_RE = re.compile(r"^#{1,6}[ \t]+(.+)$", re.M)
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*|__(.+?)__", re.S)
# `_` emphasis only at WORD BOUNDARIES ((?<![\w]) / (?![\w])), so intraword
# underscores in identifiers/filenames ("my_var", "orna_guides.py") aren't
# treated as italic delimiters. Both alternatives use [^\n] (not . under re.S)
# so an unpaired * / _ can't swallow across lines into the next list item.
_ITALIC_RE = re.compile(r"(?<!\*)\*(?!\*)([^\n]+?)(?<!\*)\*(?!\*)|(?<![\w])_(?!_)([^\n]+?)(?<!_)_(?![\w])")
_BULLET_RE = re.compile(r"^[ \t]*[-*][ \t]+", re.M)
# A Markdown table's separator row - "|---|:--:|--:|" etc, dashes/colons
# only per cell.
_TABLE_SEP_RE = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)+\|?\s*$")


def _stash_markdown_tables(text: str, keep) -> str:
    """Telegram's HTML mode has no <table> tag at all, so a Markdown pipe
    table ("| a | b |\\n|---|---|\\n| 1 | 2 |") otherwise shows up
    completely literally - the exact same "model writes Markdown, nothing
    renders it" gap the rest of this converter exists for, just needing
    real per-column width alignment instead of a simple regex swap.
    Converts each detected table block to a column-aligned plain-text
    table wrapped in <pre> (cell content escaped here, once, same as the
    code-block stashing above) and stashes it via the caller's `keep` so
    it survives the html.escape() pass untouched, like a code block does."""
    lines = text.split("\n")
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.count("|") >= 2 and i + 1 < len(lines) and _TABLE_SEP_RE.match(lines[i + 1]):
            header = [c.strip() for c in line.strip().strip("|").split("|")]
            j = i + 2
            body: list[list[str]] = []
            while j < len(lines) and lines[j].count("|") >= 2 and not _TABLE_SEP_RE.match(lines[j]):
                body.append([c.strip() for c in lines[j].strip().strip("|").split("|")])
                j += 1
            widths = [len(c) for c in header]
            for row in body:
                for k, c in enumerate(row):
                    if k < len(widths):
                        widths[k] = max(widths[k], len(c))
            table_lines = [" | ".join(c.ljust(widths[k]) for k, c in enumerate(header))]
            table_lines.append("-+-".join("-" * w for w in widths))
            for row in body:
                cells = [(row[k] if k < len(row) else "").ljust(widths[k]) for k in range(len(widths))]
                table_lines.append(" | ".join(cells))
            out.append(keep(f"<pre>{html.escape(chr(10).join(table_lines))}</pre>"))
            i = j
        else:
            out.append(line)
            i += 1
    return "\n".join(out)


def _markdown_to_html(text: str) -> str:
    """Best-effort GitHub-flavored-Markdown -> Telegram HTML. Telegram's
    HTML mode only understands b/i/u/s/code/pre/a - no headings, no lists -
    so a heading becomes its own bold line and a bullet becomes a plain
    "•", the closest real equivalent each has.

    Code spans are pulled out and stashed BEFORE the rest of the text gets
    html.escape()'d, so code content is escaped on its own (once) and never
    re-interpreted as further Markdown syntax; everything else is escaped
    first and only then gets its tags added back in, so a literal "<"/"&"
    the model wrote never gets mistaken for one of ours.
    """
    stash: list[str] = []

    def _keep(snippet: str) -> str:
        stash.append(snippet)
        return f"\x00{len(stash) - 1}\x00"

    text = _CODE_BLOCK_RE.sub(lambda m: _keep(f"<pre>{html.escape(m.group(1).strip())}</pre>"), text)
    text = _INLINE_CODE_RE.sub(lambda m: _keep(f"<code>{html.escape(m.group(1))}</code>"), text)
    text = _stash_markdown_tables(text, _keep)

    text = html.escape(text)

    # A heading line is already made bold as a whole, so strip any **bold**
    # markers inside it first - otherwise the later _BOLD_RE pass re-wraps
    # them and produces <b><b>..</b></b>.
    text = _HEADING_RE.sub(
        lambda m: f"<b>{_BOLD_RE.sub(lambda mm: mm.group(1) or mm.group(2), m.group(1))}</b>", text)
    text = _LINK_RE.sub(lambda m: f'<a href="{m.group(2)}">{m.group(1)}</a>', text)
    text = _BOLD_RE.sub(lambda m: f"<b>{m.group(1) or m.group(2)}</b>", text)
    # Bullets BEFORE italics: a "*"-marked list ("* item") otherwise has its
    # leading "*"s paired up as italic delimiters and consumed before they're
    # ever recognized as bullets. Converting "* "/"- " to "• " first leaves
    # only genuine inline *emphasis* for the italic pass.
    text = _BULLET_RE.sub("• ", text)
    text = _ITALIC_RE.sub(lambda m: f"<i>{m.group(1) or m.group(2)}</i>", text)

    for i, snippet in enumerate(stash):
        text = text.replace(f"\x00{i}\x00", snippet)
    return text


async def _reply_markdown(message, text: str, **kwargs):
    """reply_text with _markdown_to_html applied, falling back to the
    original plain text (no parse_mode) if Telegram rejects the converted
    HTML as malformed - a converter edge case should degrade back to the
    original bug (literal ** in the message) rather than the message
    failing to send at all."""
    try:
        return await message.reply_text(_markdown_to_html(text), parse_mode="HTML", **kwargs)
    except TelegramError:
        logger.warning("go: HTML-formatted reply rejected, falling back to plain text", exc_info=True)
        return await message.reply_text(text, **kwargs)

_TOOLS_BASE = (
    "- search(query): web search, returns a short answer plus a few "
    "snippets from the web, each with its url.\n"
    "- youtube(query): looks up a matching YouTube video. The bot shows "
    "the user its title/duration and lets them confirm before any "
    "download happens, so just give your best search query.\n"
    "- open(url): fetches a page (one of the urls search gave you), returns its text, "
    "and shows its main image to the user directly in the chat if it has one.\n"
    "- calculate(expression): evaluates a numeric expression (+ - * / ** % and "
    "parentheses) and returns the result - use this instead of doing arithmetic "
    "yourself, you will get it wrong.\n"
)
# nsfw mode's search (DuckDuckGo) only returns links + short snippets, no
# synthesized answer or images the way Tavily gives non-nsfw mode - search
# alone can never show the user anything visual there. open() is the only way
# an image reaches the chat in nsfw mode (it pulls the page's og:image), so
# the prompt has to say that explicitly or the model just paraphrases
# snippets into a links list and never opens anything (observed behavior
# without this). Non-nsfw mode doesn't need this nudge: Tavily's search
# already returns its own image directly.
_NSFW_OPEN_NOTE = (
    "IMPORTANT about open(): search here only returns text snippets and links - it never "
    "shows the user anything visual by itself. If the user wants to see something rather "
    "than just read about it, you must open() at least one promising result before "
    "finishing. Pasting raw links into your final answer instead of opening them does not "
    "show the user any image.\n"
)
_NSFW_GUIDANCE = (
    "If the user names a specific website (a domain like \"cats.com\", or a site name), "
    "scope the search to it using DuckDuckGo's site: operator instead of searching the open "
    "web - e.g. for \"cats.com british cat\" search \"site:cats.com british cat\", not just "
    "\"british cat\". After that search, look at what came back: if the results are "
    "meaningfully different pages and it's genuinely unclear which one the user wants, ask() "
    "with each candidate (title or a short description) as an option so they can pick. But if "
    "the results are basically duplicates of each other, or only one is actually relevant, "
    "don't bother asking - just open() the best one yourself.\n"
)


def _system_prompt(nsfw: bool) -> str:
    guidance = (_NSFW_OPEN_NOTE + _NSFW_GUIDANCE) if nsfw else ""
    actions = '"search"|"youtube"|"open"|"calculate"|"ask"|"finish"'
    now = datetime.now().strftime("%Y-%m-%d %H:%M %A")
    return (
        "You are a ReAct agent for a Telegram bot used over slow/expensive "
        f"airplane wifi, so be frugal with tool calls. Current date/time: {now} "
        "(server local time) - use this for \"today\", \"this week\", or other relative "
        f"dates. You have these tools:\n{_TOOLS_BASE}"
        f"{guidance}"
        "Each turn, reply with strict JSON only, no other text: "
        f'{{"thought": "<brief reasoning>", "action": {actions}, '
        '"action_input": "<string>", "options": ["<opt1>", "<opt2>"]}. '
        '"options" is only used with action "ask": 2-4 short reply choices for '
        "a clarifying question when the request is genuinely ambiguous (e.g. "
        "a company vs. a person, or news vs. a tutorial) - the user can only "
        "tap a button, not type a free-text reply, so always give options. "
        'For "finish", action_input is the final answer to send the user, '
        "written from the observations you already gathered. Don't call "
        "finish before you have enough information, don't ask more than once, "
        "and don't repeat a tool call you've already made with the same input."
    )


@dataclass
class GoSession:
    messages: list
    steps_left: int
    nsfw: bool = False
    created: float = field(default_factory=time.monotonic)
    image_urls: list = field(default_factory=list)
    image_idx: int = 0
    sources: list = field(default_factory=list)
    yt_url: str = ""
    ask_options: list = field(default_factory=list)


_SESSIONS: dict[str, GoSession] = {}

# chat_id -> (session id, expiry). Set when "Continue" is tapped; the
# custom filter below only claims a message for a chat present here (and
# not expired), so it's a no-op - falls through to the Orna bot's own
# handlers - for every chat that hasn't just tapped Continue. This is what
# lets free-text/photo continuation coexist with telegram_assess's own
# photo handler without the ordering fragility a blanket MessageHandler
# would risk (see CLAUDE.md's "Handler registration order is load-bearing").
_PENDING_CONTINUE: dict[int, tuple[str, float]] = {}


def _new_session(messages: list, steps_left: int, nsfw: bool = False) -> str:
    now = time.monotonic()
    for sid in [s for s, sess in _SESSIONS.items() if now - sess.created > SESSION_TTL_SECONDS]:
        _SESSIONS.pop(sid, None)
    if len(_SESSIONS) >= MAX_SESSIONS:
        oldest = min(_SESSIONS, key=lambda s: _SESSIONS[s].created)
        _SESSIONS.pop(oldest, None)
    sid = uuid.uuid4().hex[:10]
    _SESSIONS[sid] = GoSession(messages=messages, steps_left=steps_left, nsfw=nsfw)
    return sid


def _trim_context(messages: list) -> list:
    """Keep the system prompt plus the most recent MAX_CONTEXT_MESSAGES-1
    entries, so a long-running continued conversation doesn't grow the
    per-turn model call unbounded."""
    if len(messages) <= MAX_CONTEXT_MESSAGES:
        return messages
    return [messages[0]] + messages[-(MAX_CONTEXT_MESSAGES - 1):]


class _PendingContinueFilter(filters.MessageFilter):
    """Matches only a chat that just tapped "Continue" - see
    _PENDING_CONTINUE above for why this needs to be this narrow."""

    def filter(self, message) -> bool:
        pending = _PENDING_CONTINUE.get(message.chat_id)
        return bool(pending and time.monotonic() < pending[1])


_pending_continue_filter = _PendingContinueFilter()


async def _call_model(messages: list[dict], nsfw: bool = False) -> dict:
    """Ask the model for the next ReAct step.

    nsfw mode always uses the local NSFW_MODEL and never touches Ollama
    Cloud - that model isn't a cloud offering, and a hosted service would
    likely refuse this content anyway. Otherwise delegates to
    ollama_client.chat_json_with_fallback: try Ollama Cloud first, falling
    back to the same local Ollama model/host the Orna bot already uses
    (telegram_nlp.py) if the cloud call fails for any reason (e.g. out of
    cloud credits, or the flaky plane wifi just times out) - including,
    either way, dropping a "Continue"-attached image and retrying once if
    the model has no vision support (none of GO_MODEL/LOCAL_OLLAMA_MODEL/
    NSFW_MODEL support images today).
    """
    if nsfw:
        try:
            return await chat_json(LOCAL_OLLAMA_HOST, NSFW_MODEL, messages)
        except _UnsupportedMultimodal:
            logger.warning("go: %s has no vision support, dropping attached image(s)", NSFW_MODEL)
            if drop_images(messages):
                return await chat_json(LOCAL_OLLAMA_HOST, NSFW_MODEL, messages)
            raise

    return await chat_json_with_fallback(GO_MODEL, LOCAL_OLLAMA_HOST, LOCAL_OLLAMA_MODEL, messages, api_key=OLLAMA_API_KEY)


async def _tavily_search(query: str) -> dict:
    payload = {
        "api_key": TAVILY_API_KEY,
        "query": query,
        "max_results": 5,
        "include_images": True,
    }
    async with httpx.AsyncClient(timeout=_TAVILY_TIMEOUT) as client:
        resp = await client.post("https://api.tavily.com/search", json=payload)
        resp.raise_for_status()
        data = resp.json()

    results = data.get("results") or []
    lines = []
    answer = data.get("answer")
    if answer:
        lines.append(answer)
    for r in results:
        title = r.get("title") or r.get("url", "Untitled")
        snippet = (r.get("content") or "")[:200]
        lines.append(f"{title} ({r.get('url', '')}): {snippet}")
    text = "\n".join(lines) if lines else f"No results for: {query}"

    images = [img.get("url") if isinstance(img, dict) else img for img in (data.get("images") or [])]
    sources = [{"title": r.get("title") or r.get("url", "Untitled"), "url": r.get("url", "")} for r in results]
    return {"text": text, "images": [u for u in images if u], "sources": sources}


_BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",  # no "br": httpx can't decode Brotli without an optional extra
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
}


async def _ddg_search(query: str) -> dict:
    """DuckDuckGo's html.duckduckgo.com endpoint: plain server-rendered
    HTML, no JS/API key needed (the duckduckgo.com/?q= UI is a JS SPA and
    won't return results to a plain request). Must be POST with a
    browser-like User-Agent/Accept - a bare GET (or a minimal UA) gets a
    202 "anomaly" bot-check page instead of real results."""
    async with httpx.AsyncClient(timeout=_TAVILY_TIMEOUT, headers=_BROWSER_HEADERS) as client:
        # "!safeoff" (DDG's safe-search-off bang) 303-redirects to
        # internal-search.duckduckgo.com, an internal hostname that isn't
        # publicly resolvable - only reachable from inside DDG's own
        # infrastructure, not from a plain HTTP client. kp=-2 is the actual
        # parameter that bang maps to, so set it directly against the
        # endpoint that's already confirmed reachable instead.
        resp = await client.post("https://html.duckduckgo.com/html/", data={"q": query, "kp": "-2"})
        resp.raise_for_status()
        html = resp.text

    soup = BeautifulSoup(html, "html.parser")
    results = []
    for r in soup.select(".result")[:5]:
        a = r.select_one(".result__a")
        if not a:
            continue
        href = a.get("href", "")
        # DDG's html results wrap outbound links in a redirect:
        # //duckduckgo.com/l/?uddg=<encoded-url>&rut=...
        m = re.search(r"uddg=([^&]+)", href)
        url = unquote(m.group(1)) if m else href
        snippet_el = r.select_one(".result__snippet")
        results.append({
            "title": a.get_text(strip=True),
            "url": url,
            "content": snippet_el.get_text(strip=True) if snippet_el else "",
        })

    lines = [f"{r['title']} ({r['url']}): {r['content'][:200]}" for r in results]
    text = "\n".join(lines) if lines else f"No results for: {query}"
    sources = [{"title": r["title"], "url": r["url"]} for r in results]
    return {"text": text, "images": [], "sources": sources}


async def _run_search(query: str, nsfw: bool = False) -> tuple[str, dict]:
    if nsfw:
        try:
            data = await _ddg_search(query)
            return data["text"], {"images": data["images"], "sources": data["sources"]}
        except httpx.HTTPError as e:
            return f"search failed: {e}", {"images": [], "sources": []}

    if not TAVILY_API_KEY:
        return "search tool unavailable: TAVILY_API_KEY is not set.", {"images": [], "sources": []}
    try:
        data = await _tavily_search(query)
        return data["text"], {"images": data["images"], "sources": data["sources"]}
    except httpx.HTTPError as e:
        return f"search failed: {e}", {"images": [], "sources": []}


_OPEN_MAX_CHARS = 4000


async def _open_url(url: str) -> tuple[str, str | None]:
    async with httpx.AsyncClient(timeout=_TAVILY_TIMEOUT, headers=_BROWSER_HEADERS, follow_redirects=True) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        html = resp.text

    soup = BeautifulSoup(html, "html.parser")
    # DuckDuckGo's own image search JSON endpoint (i.js) actively blocks
    # non-browser clients (tried several header/token combos - consistent
    # 403s), so nsfw mode has no real image *search*. og:image is a
    # reasonable substitute: most content pages already carry one, and we
    # need no extra request since we're already fetching this page.
    image = None
    tag = soup.find("meta", attrs={"property": "og:image"}) or soup.find("meta", attrs={"name": "twitter:image"})
    if tag and tag.get("content"):
        image = urljoin(url, tag["content"])

    for tag in soup(["script", "style", "nav", "header", "footer", "noscript"]):
        tag.decompose()
    text = " ".join(soup.get_text(separator=" ").split())
    return text[:_OPEN_MAX_CHARS] or "(page had no readable text)", image


async def _run_open(url: str) -> tuple[str, str | None]:
    try:
        return await _open_url(url)
    except httpx.HTTPError as e:
        return f"couldn't open {url}: {e}", None


_CALC_OPS = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
    ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod, ast.Pow: operator.pow,
    ast.USub: operator.neg, ast.UAdd: operator.pos,
}

# ast.Pow is CPU-unbounded (big-int math): "9 ** (10**7)" runs for seconds
# and, since _calculate is sync, would stall the whole event loop for every
# chat. Cap the exponent - anything past this is never a real calc question.
_MAX_POW_EXP = 10000


def _safe_eval(node: ast.AST) -> float:
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _CALC_OPS:
        left, right = _safe_eval(node.left), _safe_eval(node.right)
        if isinstance(node.op, ast.Pow) and abs(right) > _MAX_POW_EXP:
            raise ValueError(f"exponent too large (max {_MAX_POW_EXP})")
        return _CALC_OPS[type(node.op)](left, right)
    if isinstance(node, ast.UnaryOp) and type(node.op) in _CALC_OPS:
        return _CALC_OPS[type(node.op)](_safe_eval(node.operand))
    raise ValueError("only numbers and + - * / ** % () are allowed")


def _calculate(expression: str) -> str:
    """Synchronous, no I/O - ast-restricted eval (never Python's own eval)
    so a model-supplied expression can't execute arbitrary code."""
    try:
        tree = ast.parse(expression, mode="eval")
        return str(_safe_eval(tree.body))
    except Exception as e:
        return f"couldn't calculate {expression!r}: {e}"


async def _yt_lookup(query: str) -> dict:
    """Metadata-only: title/duration/url, no download. Fast and cheap so
    we can show the user what they'd get before spending bandwidth."""
    cmd = [YTDLP_PATH, "-J", "--no-warnings", "--skip-download", "--no-playlist", f"ytsearch1:{query}"]
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=30.0)
    except asyncio.TimeoutError:
        proc.kill()
        raise RuntimeError("YouTube lookup timed out")
    if proc.returncode != 0:
        raise RuntimeError(err.decode(errors="replace")[-500:])

    data = json.loads(out.decode(errors="replace"))
    entry = data["entries"][0] if "entries" in data else data
    duration = entry.get("duration") or 0
    mins, secs = divmod(int(duration), 60)
    return {
        "title": entry.get("title", query),
        "url": entry.get("webpage_url") or entry.get("original_url") or entry.get("id", ""),
        "duration": f"{mins}:{secs:02d}" if duration else "?",
        "duration_seconds": duration,
        "thumbnail": entry.get("thumbnail"),
    }


class VideoTooLong(RuntimeError):
    """Can't fit this video into MAX_VIDEO_MB without dropping below the
    bottom of QUALITY_LADDER - not a bug, just too much content for the cap."""


async def _download_source(url: str, out_dir: str) -> Path:
    """Grab the best available video+audio up to a moderate ceiling - no
    point pulling a big source when _fit_to_size re-encodes it down to
    MAX_VIDEO_MB anyway. Container/codec don't matter here: ffmpeg
    re-encodes unconditionally below, so none of yt-dlp's format-matching
    quirks (VP9-in-mp4, a fragment losing the --max-filesize race, ...)
    can produce a broken final file the way they used to - this download
    only has to succeed structurally, not be directly playable."""
    out_tmpl = os.path.join(out_dir, "%(id)s.%(ext)s")
    cmd = [
        YTDLP_PATH, url,
        # Prefer an already-combined progressive format (single file,
        # audio+video baked in by YouTube) over a split bv*+ba merge: a
        # split download needs two separate fetches to both succeed, and
        # YouTube has been increasingly serving 403s for one specific
        # adaptive audio track while the matching video track still works
        # fine (an anti-bot/PO-token thing, not a size or format issue) -
        # a progressive format sidesteps that whole failure class.
        "-f", "b[height<=480]/bv*[height<=480]+ba/best",
        "--merge-output-format", "mkv",
        # yt-dlp needs its own ffmpeg to merge bv+ba, found via PATH unless
        # told explicitly - under launchd, PATH is minimal and doesn't
        # include Homebrew's /opt/homebrew/bin, so without this yt-dlp
        # silently leaves the two fragments unmerged (still exits 0) and
        # we'd pick whichever fragment happens to be bigger as "the"
        # download - explains multiple past "missing audio/video" bugs.
        "--ffmpeg-location", FFMPEG_PATH,
        "--max-filesize", f"{FRAGMENT_SAFETY_MB}m",
        "--no-playlist", "-o", out_tmpl,
    ]
    logger.info("go: downloading source for %s (cmd: %s)", url, " ".join(cmd))
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await proc.communicate()
    if proc.returncode != 0:
        logger.warning("go: yt-dlp source download failed (rc=%s) for %s\n%s", proc.returncode, url, out.decode(errors="replace")[-2000:])
        raise RuntimeError(out.decode(errors="replace")[-1500:])

    files = list(Path(out_dir).glob("*"))
    if not files:
        logger.warning("go: yt-dlp exited 0 but produced no file for %s", url)
        raise RuntimeError("yt-dlp reported success but produced no file")

    # A real merge leaves one "<id>.<ext>" file; if merging failed (e.g.
    # yt-dlp couldn't find ffmpeg) the separate "<id>.f<format>.<ext>"
    # fragments are left behind instead - "biggest file" would then just
    # pick whichever fragment (video or audio) happens to be bigger.
    is_fragment = re.compile(r"\.f[\w-]+$")
    clean = [f for f in files if not is_fragment.search(f.stem)]
    if not clean:
        logger.warning("go: no merged file for %s, only fragments: %s", url, [f.name for f in files])
        raise RuntimeError(f"video+audio didn't merge (got only: {', '.join(f.name for f in files)}) - try again")
    source = max(clean, key=lambda f: f.stat().st_size)
    logger.info("go: source for %s -> %s (%.1fMB), dir had: %s", url, source.name, source.stat().st_size / (1024 * 1024), [f.name for f in files])
    return source


async def _probe_duration(path: Path) -> float:
    probe = await asyncio.create_subprocess_exec(
        FFPROBE_PATH, "-v", "error", "-show_entries", "format=duration",
        "-of", "csv=p=0", str(path),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    out, _ = await probe.communicate()
    try:
        duration = float(out.decode(errors="replace").strip())
    except ValueError:
        logger.warning("go: couldn't parse duration for %s, ffprobe said: %r", path.name, out.decode(errors="replace"))
        return 0.0
    logger.info("go: %s duration=%.1fs", path.name, duration)
    return duration


async def _has_audio(path: Path) -> bool:
    """yt-dlp's --max-filesize checks the video-only and audio-only
    fragments independently before merging - if the audio side alone
    trips FRAGMENT_SAFETY_MB, it gets silently dropped and _download_source
    is left picking the surviving video-only file as "the" download.
    _fit_to_size's "-map 0:a:0?" would then quietly skip the (absent)
    audio rather than failing, shipping a silent video-with-no-sound file -
    so check for audio explicitly and fail loudly instead."""
    probe = await asyncio.create_subprocess_exec(
        FFPROBE_PATH, "-v", "error", "-select_streams", "a",
        "-show_entries", "stream=index", "-of", "csv=p=0", str(path),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    out, _ = await probe.communicate()
    has_audio = bool(out.decode(errors="replace").strip())
    logger.info("go: %s has_audio=%s", path.name, has_audio)
    return has_audio


async def _fit_to_size(path: Path, duration: float) -> Path:
    """Re-encode to H.264/AAC at whatever resolution/bitrate fits
    MAX_VIDEO_MB for this duration, stepping down QUALITY_LADDER only as
    far as needed. Refuses rather than shipping an unwatchably low-quality
    result - the caller surfaces that as "try a shorter video"."""
    if duration <= 0:
        raise RuntimeError("couldn't read video duration")

    budget_kbps = (MAX_VIDEO_MB * 8 * 1024) / duration - AUDIO_BITRATE_KBPS
    for height, min_kbps, max_kbps in QUALITY_LADDER:
        if budget_kbps >= min_kbps:
            video_kbps = min(budget_kbps, max_kbps)
            break
    else:
        bottom = QUALITY_LADDER[-1][0]
        raise VideoTooLong(
            f"this video is too long to fit in {MAX_VIDEO_MB}MB at watchable quality "
            f"(would need to drop below {bottom}p) - try a shorter video"
        )

    fixed = path.with_name(path.stem + f".{height}p.mp4")
    cmd = [
        FFMPEG_PATH, "-y", "-i", str(path),
        "-map", "0:v:0", "-map", "0:a:0?",
        "-vf", f"scale=-2:{height}",
        "-c:v", "libx264", "-preset", "veryfast",
        "-b:v", f"{video_kbps:.0f}k", "-maxrate", f"{video_kbps * 1.2:.0f}k", "-bufsize", f"{video_kbps * 2:.0f}k",
        "-c:a", "aac", "-b:a", f"{AUDIO_BITRATE_KBPS}k",
        "-movflags", "+faststart", str(fixed),
    ]
    logger.info("go: encoding %s -> target %dp %.0fkbps video (budget was %.0fkbps)", path.name, height, video_kbps, budget_kbps)
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await proc.communicate()
    if proc.returncode != 0:
        logger.warning("go: ffmpeg encode failed (rc=%s) for %s\n%s", proc.returncode, path.name, out.decode(errors="replace")[-2000:])
        raise RuntimeError(f"encode failed: {out.decode(errors='replace')[-1000:]}")

    size_mb = fixed.stat().st_size / (1024 * 1024)
    logger.info("go: encoded %s -> %dp %.0fkbps, %.1fMB", fixed.name, height, video_kbps, size_mb)
    # Bitrate targeting isn't exact - allow modest headroom before refusing.
    if size_mb > MAX_VIDEO_MB * 1.15:
        fixed.unlink(missing_ok=True)
        raise RuntimeError(f"encoded file is still {size_mb:.0f}MB (target {MAX_VIDEO_MB}MB) - try a shorter video")
    # Free the (usually much larger, up to FRAGMENT_SAFETY_MB) source now
    # rather than leaving it sitting in the temp dir through the upload.
    path.unlink(missing_ok=True)
    return fixed


async def _download_youtube(url: str, out_dir: str, audio: bool) -> Path:
    if audio:
        out_tmpl = os.path.join(out_dir, "%(id)s.%(ext)s")
        cmd = [
            YTDLP_PATH, url,
            "-x", "--audio-format", "mp3",
            "--ffmpeg-location", FFMPEG_PATH,
            "--max-filesize", f"{FRAGMENT_SAFETY_MB}m",
            "--no-playlist", "-o", out_tmpl,
        ]
        logger.info("go: downloading audio for %s", url)
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await proc.communicate()
        if proc.returncode != 0:
            logger.warning("go: yt-dlp audio download failed (rc=%s) for %s\n%s", proc.returncode, url, out.decode(errors="replace")[-2000:])
            raise RuntimeError(out.decode(errors="replace")[-1500:])
        files = list(Path(out_dir).glob("*"))
        if not files:
            raise RuntimeError("yt-dlp reported success but produced no file")
        path = max(files, key=lambda f: f.stat().st_size)
        size_mb = path.stat().st_size / (1024 * 1024)
        logger.info("go: audio for %s -> %s (%.1fMB)", url, path.name, size_mb)
        if size_mb > MAX_AUDIO_MB:
            raise RuntimeError(f"audio is {size_mb:.0f}MB, over the {MAX_AUDIO_MB}MB cap")
        return path

    source = await _download_source(url, out_dir)
    if not await _has_audio(source):
        raise RuntimeError("download has no audio track (likely dropped mid-download) - try again")
    duration = await _probe_duration(source)
    return await _fit_to_size(source, duration)


async def _run_youtube_download(url: str, message, audio: bool) -> str:
    label = "audio" if audio else "video"
    status = await message.reply_text(f"⬇️ Downloading {label}…")
    with tempfile.TemporaryDirectory() as tmp:
        try:
            path = await _download_youtube(url, tmp, audio)
        except RuntimeError as e:
            logger.warning("go: %s download failed for %s: %s", label, url, e)
            await status.edit_text(f"Download failed: {e}")
            return f"youtube download failed: {e}"
        await status.edit_text("⬆️ Uploading…")
        with open(path, "rb") as f:
            if audio:
                await message.reply_audio(f)
            else:
                await message.reply_video(f, supports_streaming=True)
        await status.delete()
    return f"{label} was downloaded and sent to the chat."


async def _propose_youtube(sid: str, query: str, message) -> None:
    session = _SESSIONS.get(sid)
    if session is None:
        return
    try:
        info = await _yt_lookup(query)
    except (RuntimeError, json.JSONDecodeError, KeyError, IndexError) as e:
        session.messages.append({"role": "user", "content": f"Observation: youtube lookup failed: {e}"})
        await _advance(sid, message)
        return

    session.yt_url = info["url"]
    logger.info("go: proposing %s (%s, %ss)", info["url"], info["title"], info.get("duration_seconds"))
    # No duration can fit MAX_VIDEO_MB below the ladder's bottom tier, so a
    # video already known (for free, from the lookup) to be longer than
    # that can never succeed - skip offering Video at all rather than
    # downloading a source that was always going to be rejected.
    too_long = info.get("duration_seconds", 0) > MAX_VIDEO_DURATION_SECONDS
    buttons = []
    if not too_long:
        buttons.append(InlineKeyboardButton("🎬 Video", callback_data=f"go|yt|{sid}|video"))
    buttons.append(InlineKeyboardButton("🎧 Audio only", callback_data=f"go|yt|{sid}|audio"))
    buttons.append(InlineKeyboardButton("❌ Skip", callback_data=f"go|yt|{sid}|cancel"))
    keyboard = InlineKeyboardMarkup([buttons])

    if too_long:
        max_min = MAX_VIDEO_DURATION_SECONDS // 60
        caption = (
            f"{info['title']} ({info['duration']})\n"
            f"Too long to fit video in {MAX_VIDEO_MB}MB (max ~{max_min} min) - audio only, or skip:"
        )
    else:
        caption = f"{info['title']} ({info['duration']})\nSend as:"
    if info.get("thumbnail"):
        try:
            await message.reply_photo(info["thumbnail"], caption=caption[:1024], reply_markup=keyboard)
            return
        except TelegramError:
            logger.warning("go: failed to send video thumbnail", exc_info=True)
    await message.reply_text(f"🎬 {caption}", reply_markup=keyboard)


async def _send_finish(sid: str, session: GoSession, message, answer: str) -> None:
    buttons = []
    if len(session.image_urls) > 1:
        buttons.append(InlineKeyboardButton("🖼 Next image", callback_data=f"go|img|{sid}"))
    if session.sources:
        buttons.append(InlineKeyboardButton("📄 Sources", callback_data=f"go|src|{sid}"))
    buttons.append(InlineKeyboardButton("▶️ Continue", callback_data=f"go|cont|{sid}"))

    if session.image_urls:
        try:
            await message.reply_photo(session.image_urls[0])
        except TelegramError:
            logger.warning("go: failed to send preview image", exc_info=True)

    markup = InlineKeyboardMarkup([buttons]) if buttons else None
    await _reply_markdown(message, answer, reply_markup=markup)


async def _advance(sid: str, message) -> None:
    session = _SESSIONS.get(sid)
    if session is None:
        return

    while session.steps_left > 0:
        session.steps_left -= 1
        try:
            step = await _call_model(session.messages, nsfw=session.nsfw)
        except (OllamaError, _UnsupportedMultimodal) as e:
            logger.exception("go: model call failed")
            await message.reply_text(f"Planning failed: {e}")
            return

        action = step.get("action")
        action_input = str(step.get("action_input") or "").strip()

        if action == "finish" or not action:
            await _send_finish(sid, session, message, action_input or "I couldn't figure out an answer.")
            return

        if action == "ask":
            # Record the model's own question BEFORE returning - otherwise
            # (this branch returns before the shared append below) its
            # question/thought never enters session.messages, and after the
            # user taps an option the model sees two consecutive user turns
            # with its own question erased from its context.
            session.messages.append({"role": "assistant", "content": json.dumps(step)})
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
                InlineKeyboardButton(opt[:30], callback_data=f"go|ask|{sid}|{i}")
                for i, opt in enumerate(options)
            ]])
            await _reply_markdown(message, action_input or "Which do you mean?", reply_markup=keyboard)
            return

        session.messages.append({"role": "assistant", "content": json.dumps(step)})

        if action == "search":
            observation, extra = await _run_search(action_input, nsfw=session.nsfw)
            # Only replace images/sources when this search actually produced
            # some. _run_search returns empty lists on an HTTP failure too, so
            # an unconditional overwrite let a transient error on a SECOND
            # search silently discard a first search's still-valid results
            # (dropping the "Next image"/"Sources" buttons from _send_finish).
            if extra["images"]:
                session.image_urls = extra["images"]
                session.image_idx = 0
            if extra["sources"]:
                session.sources = extra["sources"]
        elif action == "youtube":
            await _propose_youtube(sid, action_input, message)
            return
        elif action == "open":
            observation, image = await _run_open(action_input)
            if image:
                session.image_urls.append(image)
        elif action == "calculate":
            observation = _calculate(action_input)
        else:
            observation = f"unknown action {action!r}; valid actions are search, youtube, open, calculate, ask, finish."

        session.messages.append({"role": "user", "content": f"Observation: {observation}"})

    await message.reply_text("Gave up after too many steps without a final answer.")


async def _mark_done(query, note: str) -> None:
    """Append `note` to the tapped message and drop its buttons.

    Handles both plain-text proposals (ask) and photo proposals with a
    caption (youtube, since _propose_youtube attaches a thumbnail) -
    edit_message_text raises on a photo message, so route by message type.
    """
    msg = query.message
    if msg.photo:
        await query.edit_message_caption(caption=f"{msg.caption or ''}\n\n{note}"[:1024], reply_markup=None)
    else:
        await query.edit_message_text(f"{msg.text or ''}\n\n{note}", reply_markup=None)


async def go_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    parts = (query.data or "").split("|")
    if len(parts) < 3 or parts[0] != "go":
        return
    kind, sid = parts[1], parts[2]
    session = _SESSIONS.get(sid)
    if session is None:
        await _mark_done(query, "(this /go session expired - run /go again)")
        return

    if kind == "ask":
        idx = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else -1
        if not (0 <= idx < len(session.ask_options)):
            return
        choice = session.ask_options[idx]
        await _mark_done(query, f"→ {choice}")
        session.messages.append({"role": "user", "content": f'Observation: user chose "{choice}".'})
        await _advance(sid, query.message)
        return

    if kind == "yt":
        fmt = parts[3] if len(parts) > 3 else "cancel"
        if fmt == "cancel":
            await _mark_done(query, "(skipped)")
            session.messages.append({"role": "user", "content": "Observation: user skipped the youtube download."})
            await _advance(sid, query.message)
            return
        await query.edit_message_reply_markup(reply_markup=None)
        observation = await _run_youtube_download(session.yt_url, query.message, audio=(fmt == "audio"))
        session.messages.append({"role": "user", "content": f"Observation: {observation}"})
        await _advance(sid, query.message)
        return

    if kind == "img":
        if not session.image_urls:
            return
        session.image_idx = (session.image_idx + 1) % len(session.image_urls)
        try:
            await query.message.reply_photo(session.image_urls[session.image_idx])
        except TelegramError:
            logger.warning("go: failed to send preview image", exc_info=True)
        return

    if kind == "src":
        if not session.sources:
            return
        text = "\n\n".join(f"{s['title']}\n{s['url']}" for s in session.sources)
        await query.message.reply_text(text, disable_web_page_preview=True)
        return

    if kind == "cont":
        _PENDING_CONTINUE[query.message.chat_id] = (sid, time.monotonic() + CONTINUE_TTL_SECONDS)
        await query.message.reply_text("💬 Send your follow-up (text and/or a photo) to continue this conversation.")
        return


async def handle_go(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    if not message:
        return

    user = update.effective_user
    if GO_ALLOWED_USER_IDS and (not user or user.id not in GO_ALLOWED_USER_IDS):
        logger.warning("go: rejected user_id=%s", user.id if user else None)
        return
    if not GO_ALLOWED_USER_IDS:
        logger.warning("go: GO_ALLOWED_USER_IDS is not set - anyone can use /go")

    args = list(context.args)
    nsfw = bool(args) and args[0].lower() == "nsfw"
    if nsfw:
        args = args[1:]
    request = " ".join(args).strip()
    usage_stats.record_command_for(update, "go", request)
    if not request:
        await message.reply_text("Usage: /go [nsfw] <what you want>")
        return

    # nsfw mode stays fully local (NSFW_MODEL + DuckDuckGo), no cloud key needed.
    if not nsfw and not OLLAMA_API_KEY:
        await message.reply_text("OLLAMA_API_KEY is not set.")
        return

    # A fresh /go always starts a clean conversation - drop any dangling
    # "waiting for a Continue reply" state for this chat rather than
    # letting it swallow whatever this new run eventually says.
    _PENDING_CONTINUE.pop(message.chat_id, None)

    messages = [
        {"role": "system", "content": _system_prompt(nsfw)},
        {"role": "user", "content": request},
    ]
    sid = _new_session(messages, MAX_STEPS, nsfw=nsfw)
    await _advance(sid, message)


async def _encode_photo(message) -> str | None:
    try:
        photo = message.photo[-1]
        file = await photo.get_file()
        data = await file.download_as_bytearray()
        return base64.b64encode(bytes(data)).decode("ascii")
    except TelegramError:
        logger.warning("go: failed to download continuation photo", exc_info=True)
        return None


async def handle_go_continue(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None:
        return

    pending = _PENDING_CONTINUE.pop(message.chat_id, None)
    if pending is None:
        return  # filter already checked this, but stay defensive
    sid, expires = pending
    if time.monotonic() > expires:
        await message.reply_text("That Continue prompt expired - use /go to start fresh.")
        return

    session = _SESSIONS.get(sid)
    if session is None:
        await message.reply_text("This /go conversation expired - use /go to start fresh.")
        return

    user = update.effective_user
    if GO_ALLOWED_USER_IDS and (not user or user.id not in GO_ALLOWED_USER_IDS):
        logger.warning("go: rejected continue from user_id=%s", user.id if user else None)
        return

    user_msg = {"role": "user", "content": message.text or message.caption or "(photo attached, no caption)"}
    if message.photo:
        image = await _encode_photo(message)
        if image:
            user_msg["images"] = [image]

    session.messages = _trim_context(session.messages)
    session.messages.append(user_msg)
    session.steps_left = MAX_STEPS
    await _advance(sid, message)


def build_go_handler() -> CommandHandler:
    return CommandHandler("go", handle_go)


def build_go_callback_handler() -> CallbackQueryHandler:
    return CallbackQueryHandler(go_callback, pattern=r"^go\|")


def build_go_continue_handler() -> MessageHandler:
    return MessageHandler(_pending_continue_filter & (filters.TEXT | filters.PHOTO) & ~filters.COMMAND, handle_go_continue)


def _demo() -> None:
    """Pinned regression checks for _markdown_to_html - run via
    `python3 telegram_go.py`. Covers both the original conversions
    (verified manually per this repo's history, now pinned for real) and
    the table support added alongside telegram_orna.py's class_guide
    tool, whose long-form answers turned out to use Markdown tables the
    original converter had no path for at all."""
    assert _markdown_to_html("# Heading\ntext") == "<b>Heading</b>\ntext"
    assert _markdown_to_html("**bold** and *italic*") == "<b>bold</b> and <i>italic</i>"
    assert _markdown_to_html("- one\n- two") == "• one\n• two"
    # "*"-marked bullets must become bullets, not be paired into italic spans.
    assert _markdown_to_html("* one\n* two\n* three") == "• one\n• two\n• three"
    # intraword underscores (identifiers/filenames) are NOT italic delimiters.
    assert _markdown_to_html("orna_knowledge.txt and orna_guides.py") == "orna_knowledge.txt and orna_guides.py"
    assert _markdown_to_html("my_variable_name") == "my_variable_name"
    # genuine _emphasis_ at word boundaries still italicizes.
    assert _markdown_to_html("this is _emph_ here") == "this is <i>emph</i> here"
    # a heading containing bold isn't double-wrapped (<b><b>..</b></b>).
    assert _markdown_to_html("## **Important**") == "<b>Important</b>"
    assert _markdown_to_html("`code`") == "<code>code</code>"
    assert _markdown_to_html("```\nx = 1\n```") == "<pre>x = 1</pre>"
    assert _markdown_to_html("[Orna](https://playorna.com)") == '<a href="https://playorna.com">Orna</a>'
    assert _markdown_to_html("<3 & you") == "&lt;3 &amp; you"  # literal HTML-special chars survive as text, not tags

    table = "| Longname | B |\n|---|----|\n| 1 | 22 |"
    got = _markdown_to_html(table)
    assert got.startswith("<pre>") and got.endswith("</pre>"), got
    # Every row's first column is padded to the widest cell in it ("Longname"),
    # not just its own header width - real column alignment, not per-cell.
    assert "Longname | B" in got and "1        | 22" in got, got

    print("telegram_go._demo: all checks passed")


if __name__ == "__main__":
    _demo()
