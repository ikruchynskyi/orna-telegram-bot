"""Harvest curated Orna Discord knowledge, through a real Chrome the user logged into.

WHY A BROWSER. Reading servers you are only a MEMBER of needs a user account -
a bot can only read servers it was invited to (that is what orna_discord.py
does, for the followed announcement channel). Automating a user account is
against Discord's terms and can get the account banned; the user chose that
risk knowingly (2026-10-06). This module keeps it as small as it can:
read-only, ~30 requests a run, paced, and the account token never leaves the
browser - Chrome is driven through Discord's own search box and the app's own
request is read back, so Discord sees ordinary web-app traffic.

HOW. A dedicated Chrome profile (.discord_chrome/, gitignored) is logged in
once by hand: `python3 orna_discord_search.py login`. A hook injected into the
page wraps XMLHttpRequest: when the app sends its /messages/search request, the
hook swaps in the URL we want (a channel's history page, its pins) and stashes
the response body for Selenium to read back.

WHAT. Not chat: measured 2026-10-06, keyword search over chat returns people
ASKING a question, and all of one forum is 224k messages. Only what players
curated: the full history of the FAQ/guide channels (~180 messages) and the
PINNED messages of every class/strategy channel (~100). Images in them (charts,
tier lists) are transcribed to text once by a vision model, cached by
attachment id, and indexed with their message. orna_pinecone indexes the
result as the "discord" namespace.

Run: python3 orna_discord_search.py login      open Chrome to log in (once)
     python3 orna_discord_search.py harvest    fetch (~2 min) + transcribe new images
     python3 orna_discord_search.py transcribe only the images a previous run missed
     python3 orna_discord_search.py search <q> one live search (the bot's discord_search)
     python3 orna_discord_search.py            self-check (no browser)
Then: python3 orna_pinecone.py discord
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import sys
import threading
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode

import httpx

logger = logging.getLogger(__name__)

PROFILE_DIR = Path(__file__).with_name(".discord_chrome")
CACHE_DIR = Path(__file__).with_name(".discord_cache")
API = "https://discord.com/api/v9"
MIN_CONTENT = 25            # "lol same" carries no knowledge
MIN_INTERVAL = 2.5          # seconds between requests - human pace, not a crawler
RESULT_WAIT = 15            # seconds to wait for one response
RATE_LIMIT_WAIT = 60
# Measured 2026-10-06 on a 59-row number table and a 72-cell X-grid, against
# the images: kimi-k3 exact on numbers, 0-1 grid cells wrong, ~12s/image;
# gemma4:31b missed the same grid cell every run and took ~60s; deepseek-v4.1-
# flash got 4-5 rows wrong. Changing it re-transcribes every image (cache key).
VISION_MODEL = "kimi-k3"

GUILDS = {"448527960056791051": "Orna: The GPS RPG", "748188991852904621": "Orna Legends"}
# channel id -> (guild id, name). FULL: the whole history (small, curated).
FULL = {
    "1087092819740606484": ("448527960056791051", "faq"),
    "966231961091854387": ("448527960056791051", "guides-and-tools"),
    "927847673543938078": ("748188991852904621", "faq"),
    "811290781896802305": ("748188991852904621", "useful-tips-and-charts"),
    "1041428820454031521": ("748188991852904621", "aethric-resources-and-faq"),
}
# PINNED only: busy class/strategy channels, where pins are the curated answers.
PINNED = {
    "905399001564708925": ("448527960056791051", "early-game"),
    "905399155055284264": ("448527960056791051", "mid-game"),
    "905399265784913960": ("448527960056791051", "late-game"),
    "1461698457415979113": ("448527960056791051", "warrior"),
    "1461698527943200957": ("448527960056791051", "thief"),
    "1461698570079047692": ("448527960056791051", "mage"),
    "1461698614207451278": ("448527960056791051", "valhallan"),
    "1461699097500057682": ("448527960056791051", "summoner"),
    "1461699153246552166": ("448527960056791051", "gods"),
    "930484646918103060": ("748188991852904621", "tools"),
    "811270018661613627": ("748188991852904621", "questions"),
    "811254537230745620": ("748188991852904621", "gilgamesh-gallia"),
    "811254572042420245": ("748188991852904621", "heretic-hera"),
    "811254753178550333": ("748188991852904621", "realmshifter"),
    "811254803199164436": ("748188991852904621", "beowulf-bestla"),
    "811254836208074752": ("748188991852904621", "deity"),
    "1009229920087593000": ("748188991852904621", "grand-summoner"),
    "814976465421860924": ("748188991852904621", "swash-blade"),
    "1077164761554370560": ("748188991852904621", "towers"),
    "979507211954946118": ("748188991852904621", "seers-guild-and-amities"),
    "864030194779291658": ("748188991852904621", "endless-dungeon"),
    "811271750276743268": ("748188991852904621", "codexing"),
}

_HOOK = r"""
(() => {
  if (window.__ornaHooked) return;
  window.__ornaHooked = true; window.__ornaHits = []; window.__ornaUrl = null;
  const open = XMLHttpRequest.prototype.open, send = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open = function(m, u, ...rest) {
    u = String(u);
    this.__ornaMine = u.includes('/messages/search');
    if (window.__ornaUrl && this.__ornaMine) { u = window.__ornaUrl; window.__ornaUrl = null; }
    return open.call(this, m, u, ...rest);
  };
  XMLHttpRequest.prototype.send = function() {
    if (this.__ornaMine)
      this.addEventListener('loadend', () => window.__ornaHits.push({status: this.status, body: this.responseText}));
    return send.apply(this, arguments);
  };
})();
"""


def _driver(headless: bool):
    from selenium import webdriver
    opts = webdriver.ChromeOptions()
    opts.add_argument(f"--user-data-dir={PROFILE_DIR}")
    opts.add_argument("--window-size=1400,900")
    if headless:
        opts.add_argument("--headless=new")
    drv = webdriver.Chrome(options=opts)
    # Runs before Discord's own scripts on every load, so the hook survives reloads.
    drv.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {"source": _HOOK})
    return drv


def login() -> None:
    """Open a visible Chrome on Discord's login page and wait for the user."""
    drv = _driver(headless=False)
    drv.get("https://discord.com/login")
    print("Log in to Discord in the Chrome window that just opened (waiting up to 15 min)...", flush=True)
    deadline = time.time() + 900
    while time.time() < deadline:
        if "/channels/" in drv.current_url:
            print("Logged in - the profile is saved in", PROFILE_DIR)
            time.sleep(5)       # let Discord finish writing its session to the profile
            drv.quit()
            return
        time.sleep(2)
    drv.quit()
    raise SystemExit("timed out waiting for the login")


class _Browser:
    """One headless Chrome, one request at a time."""

    def __init__(self):
        self.lock = threading.Lock()
        self.drv = None
        self.last = 0.0
        self.n = 0
        self.stale = False      # the app's search panel was fed a non-search response

    def _box(self):
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support import expected_conditions as EC
        from selenium.webdriver.support.ui import WebDriverWait
        if self.drv is None:
            self.drv = _driver(headless=True)
            self.drv.get(f"https://discord.com/channels/{next(iter(GUILDS))}")
        elif self.stale:
            self.drv.refresh()
        self.stale = False
        try:
            return WebDriverWait(self.drv, 30).until(EC.element_to_be_clickable(
                (By.CSS_SELECTOR, '[role="combobox"][aria-label^="Search"]')))
        except Exception:
            url = self.drv.current_url
            self.close()
            if "/login" in url:
                raise RuntimeError("the Discord session expired - run `python3 orna_discord_search.py login`")
            raise

    def close(self):
        if self.drv is not None:
            try:
                self.drv.quit()
            finally:
                self.drv = None

    def search(self, guild: str, params: dict) -> Optional[dict]:
        """One /guilds/<guild>/messages/search request."""
        return self.get(f"{API}/guilds/{guild}/messages/search?{urlencode(params)}")

    def get(self, url: str) -> Optional[dict]:
        """One read-only GET, sent by the Discord app itself in place of its
        own search request. None on a non-200 (202 = still indexing) or a
        timeout; waits out a 429 and retries."""
        from selenium.webdriver.common.keys import Keys
        with self.lock:
            for _attempt in range(5):
                box = self._box()
                wait = MIN_INTERVAL - (time.time() - self.last)
                if wait > 0:
                    time.sleep(wait)
                self.drv.execute_script("window.__ornaHits = []; window.__ornaUrl = arguments[0];", url)
                # The typed text only triggers the request (the hook replaces
                # it); never the same twice, or the app skips a repeated search
                # - including one it restored after a reload.
                self.n += 1
                box.click()
                box.send_keys(Keys.COMMAND, "a")
                box.send_keys(Keys.BACKSPACE)
                box.send_keys(f"orna {int(time.time()) % 100000}{self.n}", Keys.ENTER)
                self.last = time.time()
                deadline = time.time() + RESULT_WAIT
                hits = None
                while time.time() < deadline:
                    hits = self.drv.execute_script("return window.__ornaHits;")
                    if hits:
                        break
                    time.sleep(0.3)
                # Anything but search results leaves the app's search panel
                # stuck, and it then sends nothing more until a reload.
                self.stale = "/messages/search" not in url
                if not hits:
                    logger.info("discord: request timed out, reloading the page")
                    self.stale = True
                    continue
                status = hits[-1]["status"]
                if status == 200:
                    return json.loads(hits[-1]["body"])
                if status == 429:
                    logger.info("discord: rate limited, waiting %ss", RATE_LIMIT_WAIT)
                    time.sleep(RATE_LIMIT_WAIT)
                    continue
                logger.info("discord: search got HTTP %s", status)
                return None
        raise RuntimeError("discord: search failed 5 times in a row")


_browser = _Browser()


def _record(m: dict, channel: str, pinned: bool) -> Optional[dict]:
    """One API message -> a stored record, or None for joins/pins/boosts.
    Author names are left out on purpose: the knowledge is in the text, and
    the bot quotes into a ~180-person chat people never agreed to be quoted in."""
    if m.get("type") not in (0, 19):
        return None
    images = [{"id": a["id"], "url": a["url"], "name": a.get("filename", "")}
              for a in m.get("attachments") or []
              if str(a.get("content_type", "")).startswith("image/")]
    text = (m.get("content") or "").strip()
    # A chart posted under an embed (a link preview) carries its text there.
    for e in m.get("embeds") or []:
        text += "".join(f"\n{e[k]}" for k in ("title", "description") if e.get(k))
    if len(text) < MIN_CONTENT and not images:
        return None
    guild, name = (FULL.get(channel) or PINNED[channel])
    return {"id": m["id"], "guild": guild, "channel": channel, "channel_name": name,
            "ts": m.get("timestamp", ""), "text": text.strip(), "images": images, "pinned": pinned}


def _channel_history(channel: str) -> list:
    guild = FULL[channel][0]
    out, cursor = [], None
    while True:
        params = {"channel_id": channel, "sort_by": "timestamp", "sort_order": "desc"}
        if cursor:
            params["max_id"] = cursor
        data = _browser.search(guild, params)
        msgs = [m for group in (data or {}).get("messages") or [] for m in group
                if m.get("hit") and m["id"] != cursor]
        if not msgs:
            return out
        out.extend(r for r in (_record(m, channel, False) for m in msgs) if r)
        cursor = min(msgs, key=lambda m: int(m["id"]))["id"]


def _pins(channel: str) -> list:
    data = _browser.get(f"{API}/channels/{channel}/pins")
    if not isinstance(data, list):
        raise RuntimeError(f"pins of #{PINNED[channel][1]} could not be read")
    return [r for r in (_record(m, channel, True) for m in data) if r]


_TRANSCRIBE = (
    "This image was posted in an Orna RPG community Discord as a guide, chart or reference. "
    "Make its information searchable as text. Return JSON: {\"summary\": one sentence on what it shows, "
    "\"text\": every piece of text in it, transcribed exactly}. "
    # Named cells, not "| | X |": a bare positional cell is how a mark lands in
    # the wrong column, and a row that names its columns also embeds on its own.
    "Tables: one row per line. A grid of marks (X, checkmarks, colored cells): for each row, list by name ONLY the "
    "columns that are marked, e.g. 'B Hydrus: Warrior, Thief'. Other tables: 'row name - Column: value, Column: value'. "
    "Keep item, class, monster and stat names exactly as written. Transcribe only what is visible - never add facts. "
    "If it has no readable text, describe it in \"summary\" and leave \"text\" empty.")


def _transcribe(url: str) -> dict:
    """One image -> {"summary", "text"} via the vision model on Ollama Cloud."""
    from ollama_client import OLLAMA_CLOUD_HOST, OllamaBusy, chat_json
    img = httpx.get(url, timeout=60, follow_redirects=True)
    img.raise_for_status()
    for attempt in range(6):
        try:
            out = asyncio.run(chat_json(
                OLLAMA_CLOUD_HOST, VISION_MODEL,
                [{"role": "user", "content": _TRANSCRIBE, "images": [base64.b64encode(img.content).decode()]}],
                headers={"Authorization": f"Bearer {os.environ['OLLAMA_API_KEY']}"},
                timeout=httpx.Timeout(300.0)))
            break
        except OllamaBusy:
            # Ollama Cloud caps concurrent requests per account - shared with the live bot.
            if attempt == 5:
                raise
            time.sleep(20)
    return {"summary": str(out.get("summary", "")).strip(), "text": str(out.get("text", "")).strip(),
            "model": VISION_MODEL}


def _write_json(path: Path, data) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def _load_json(name: str, default):
    try:
        return json.loads((CACHE_DIR / name).read_text(encoding="utf-8"))
    except Exception:
        return default


def harvest() -> None:
    """Fetch everything (~30 requests), then transcribe images not seen before.
    The message file is replaced only by a complete fetch - never by a partial one."""
    CACHE_DIR.mkdir(exist_ok=True)
    records = []
    try:
        for channel, (_g, name) in FULL.items():
            got = _channel_history(channel)
            print(f"#{name}: {len(got)} messages", flush=True)
            records.extend(got)
        for channel, (_g, name) in PINNED.items():
            got = _pins(channel)
            print(f"#{name}: {len(got)} pinned", flush=True)
            records.extend(got)
    finally:
        _browser.close()
    if not records:
        raise RuntimeError("harvest returned nothing - keeping the previous cache")
    _write_json(CACHE_DIR / "messages.json", records)

    transcribe(records)


def transcribe(records: Optional[list] = None) -> None:
    """Transcribe images not yet done by VISION_MODEL, 2 at a time (Ollama
    Cloud's concurrency cap is shared with the live bot). Cached by attachment
    id: an image costs one vision call per model. Attachment URLs are signed and
    expire within a day, so this runs right after a fetch."""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    records = records if records is not None else _load_json("messages.json", [])
    images = _load_json("images.json", {})
    todo = [a for r in records for a in r["images"] if images.get(a["id"], {}).get("model") != VISION_MODEL]
    with ThreadPoolExecutor(2) as pool:
        futures = {pool.submit(_transcribe, a["url"]): a for a in todo}
        for i, f in enumerate(as_completed(futures), 1):
            a = futures[f]
            try:
                images[a["id"]] = f.result()
                _write_json(CACHE_DIR / "images.json", images)
                print(f"image {i}/{len(todo)} {a['name']}: {images[a['id']]['summary'][:80]}", flush=True)
            except Exception as e:      # one bad image must not lose the rest - rerun picks it up
                logger.warning("image %s failed: %s", a["name"], e)
    print(f"{len(records)} messages, {sum(len(r['images']) for r in records)} images "
          f"({len(todo)} to transcribe this run)")


def units() -> list:
    """(head, lines, title, url) per message - the shape orna_pinecone._units
    returns for every corpus. Each message is its own unit: these channels hold
    standalone posts (a FAQ answer, a guide, a pinned explanation), not threads."""
    images = _load_json("images.json", {})
    out = []
    for r in _load_json("messages.json", []):
        title = f"{GUILDS[r['guild']]} #{r['channel_name']}" + (" (pinned)" if r["pinned"] else "")
        lines = r["text"].splitlines() if r["text"] else []
        for a in r["images"]:
            t = images.get(a["id"])
            if t:
                lines.append(f"[image {a['name']}: {t['summary']}]")
                lines.extend(t["text"].splitlines())
        if sum(len(l) for l in lines) < MIN_CONTENT:
            continue
        out.append((f"[{title}, {r['ts'][:10]}]", lines, title,
                    f"https://discord.com/channels/{r['guild']}/{r['channel']}/{r['id']}"))
    return out


# --- live search: the /orna loop's last resort (discord_search) ---

def format_conversation(hit: dict, max_line: int = 400) -> str:
    """A hit with its surrounding messages, the matched one marked with ►."""
    lines = [f"[{hit['guild']}, {hit['date']}]"]
    for m in hit.get("context") or [hit]:
        mark = "►" if m["id"] == hit["id"] else " "
        lines.append(f"{mark} {m['text'][:max_line].replace(chr(10), ' ')}")
    return "\n".join(lines)

def enabled() -> bool:
    """Only once the profile has been logged in - the bot never opens a login window."""
    return (PROFILE_DIR / "Default").is_dir()


def _hits(data: Optional[dict], guild: str, min_len: int = 40) -> list:
    out = []
    for group in (data or {}).get("messages") or []:
        for m in group:
            text = (m.get("content") or "").strip()
            if m.get("hit") and m.get("type") in (0, 19) and len(text) >= min_len:
                out.append({"id": m["id"], "channel": m["channel_id"], "guild": GUILDS[guild],
                            "date": m.get("timestamp", "")[:10], "text": text,
                            "url": f"https://discord.com/channels/{guild}/{m['channel_id']}/{m['id']}"})
    return out


CONTEXT = 10        # messages kept on each side of a hit


def _context(guild: str, hit: dict) -> list:
    """The CONTEXT messages before and after a hit in its channel, oldest first,
    the hit included. A keyword hit is often the QUESTION; the answer is in the
    replies around it. Two channel-filtered searches (newest-first below the
    hit, oldest-first above it) rather than the "messages around" endpoint: the
    app's search panel survives a search response, any other one costs a reload."""
    side = {}
    for key, order in (("max_id", "desc"), ("min_id", "asc")):
        data = _browser.search(guild, {"channel_id": hit["channel"], key: hit["id"],
                                       "sort_by": "timestamp", "sort_order": order})
        side[key] = [m for m in _hits(data, guild, min_len=1) if m["id"] != hit["id"]][:CONTEXT]
    return list(reversed(side["max_id"])) + [hit] + side["min_id"]


# Discord search is FULL-TEXT with AND over every word, not semantic: a
# sentence matches nothing ("ward capacity" -> 1 hit, "ward" -> thousands). So
# the query is cut to its distinctive words, and widened on a miss.
_STOP = set("""a an the and or of to in on for with without at by from is are was were be been do does did
how what which who whom why when where can could should would will i my me you your we our it its this that these
those there their them they get got use using work works working best good better any some all about vs versus
into out up down more most much many does dont don't is it's im i'm orna rpg game player players please help
question anyone know tell explain mean means""".split())


def _keywords(query: str) -> list:
    """The query's distinctive words, in order, at most 3."""
    seen, out = set(), []
    for w in re.findall(r"[^\W_]+(?:'[^\W_]+)?", query.lower()):
        if len(w) > 2 and w not in _STOP and w not in seen:
            seen.add(w)
            out.append(w)
    return out[:3]


def _variants(query: str) -> list:
    """Most specific first, each one more general: all keywords, the first two,
    then the single longest word (length as a cheap proxy for rarity)."""
    kw = _keywords(query) or query.split()[:1]
    out = [" ".join(kw), " ".join(kw[:2]), max(kw, key=len)]
    return list(dict.fromkeys(q for q in out if q))


def search(query: str, per_guild: int = 2) -> list:
    """Keyword search of both servers' whole chat, most relevant first, from the
    most specific of _variants to the most general until one matches; each hit
    comes back with its surrounding conversation under "context". Blocking
    (Selenium) - call through asyncio.to_thread. Chrome is started for the
    search and closed after it: this runs rarely, and an idle Chrome would hold
    the profile the harvest needs."""
    variants = _variants(query)
    out = []
    try:
        for guild in GUILDS:
            for q in variants:
                found = _hits(_browser.search(guild, {"content": q, "sort_by": "relevance"}), guild)[:per_guild]
                if found:
                    for h in found:
                        h["context"] = _context(guild, h)
                    out.extend(found)
                    break
    finally:
        _browser.close()
    return out


# --- what live searches found, kept (the "discord_live" namespace) ---
# A discord_search costs 10-30s and a real browser session on the user's
# account; its conversations are kept so the next similar question is answered
# by knowledge_search instead. Raw chat, so its own namespace and trust label -
# never mixed into the curated harvest, and safe from that one's re-index.
LIVE_FILE = "live.json"


def save_live(hits: list) -> list:
    """Persist hits not seen before (keyed by the hit message's id) with their
    surrounding conversation; returns the new keys. Author names are not in a
    hit to begin with (see _hits)."""
    CACHE_DIR.mkdir(exist_ok=True)
    live = _load_json(LIVE_FILE, {})
    new = []
    for h in hits:
        if h["id"] in live:
            continue
        live[h["id"]] = {"guild": h["guild"], "date": h["date"], "url": h["url"],
                         "lines": [("► " if m["id"] == h["id"] else "") + m["text"].replace("\n", " ")
                                   for m in h.get("context") or [h]]}
        new.append(h["id"])
    if new:
        _write_json(CACHE_DIR / LIVE_FILE, live)
    return new


def live_units() -> list:
    """(head, lines, title, url, key) per kept conversation - keyed, so each one
    can be upserted on its own (see orna_pinecone.records)."""
    return [(f"[{c['guild']} chat, {c['date']}]", c["lines"], f"Discord {c['guild']} chat ({c['date']})",
             c["url"], key) for key, c in _load_json(LIVE_FILE, {}).items()]


def _demo() -> None:
    msg = {"type": 0, "id": "5", "timestamp": "2026-10-05T20:40:41+00:00", "content": "x" * 30,
           "author": {"username": "someone"},
           "attachments": [{"id": "a1", "url": "https://cdn/x.png", "filename": "x.png", "content_type": "image/png"},
                           {"id": "a2", "url": "https://cdn/x.txt", "filename": "x.txt", "content_type": "text/plain"}]}
    r = _record(msg, "966231961091854387", False)
    assert r["channel_name"] == "guides-and-tools" and [a["id"] for a in r["images"]] == ["a1"], r
    assert "someone" not in json.dumps(r)                                   # no author names stored
    assert _record({**msg, "attachments": [], "content": "lol"}, "966231961091854387", False) is None
    assert _record({**msg, "type": 7}, "966231961091854387", False) is None  # a join, not a post
    assert _record({**msg, "content": "", "embeds": [{"title": "Tier list", "description": "y" * 30}],
                    "attachments": []}, "966231961091854387", False)["text"].startswith("Tier list")
    assert set(FULL).isdisjoint(PINNED)
    data = {"messages": [[{"hit": True, "type": 0, "content": "short", "timestamp": "2026-10-05T01:00:00",
                           "id": "1", "channel_id": "9"}],
                         [{"hit": True, "type": 0, "content": "z" * 50, "timestamp": "2026-10-05T01:00:00",
                           "id": "2", "channel_id": "9", "author": {"username": "someone"}}]]}
    assert _variants("How does the Prometheus sigil work?") == ["prometheus sigil", "prometheus"]
    assert _variants("best heretic raid build omniflask ward") == ["heretic raid build", "heretic raid", "heretic"]
    assert _variants("ward") == ["ward"]
    hits = _hits(data, "748188991852904621")
    assert len(hits) == 1 and hits[0]["url"].endswith("/748188991852904621/9/2") and "someone" not in str(hits)
    h = {**hits[0], "context": [{"id": "1", "text": "q?"}, hits[0], {"id": "3", "text": "a!"}]}
    assert format_conversation(h).splitlines()[1:] == ["  q?", "► " + "z" * 50, "  a!"]

    # save_live: new hits kept once, with the matched line marked; a repeat is not re-added
    global CACHE_DIR
    import tempfile
    real_dir = CACHE_DIR
    with tempfile.TemporaryDirectory() as tmp:
        CACHE_DIR = Path(tmp)
        try:
            assert save_live([h]) == ["2"] and save_live([h]) == []
            (unit,) = live_units()
            assert unit[1] == ["q?", "► " + "z" * 50, "a!"] and unit[4] == "2", unit
        finally:
            CACHE_DIR = real_dir
    print("orna_discord_search: _demo ok")


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    for noisy in ("selenium", "urllib3", "httpx"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    args = sys.argv[1:]
    if args == ["login"]:
        login()
    elif args == ["harvest"]:
        harvest()
    elif args == ["transcribe"]:
        transcribe()
    elif args[:1] == ["search"]:
        for h in search(" ".join(args[1:])):
            print(format_conversation(h), "\n")
    else:
        _demo()
