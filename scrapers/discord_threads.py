"""Knowledge from the official Orna Discord's discussion threads (forum channels), for knowledge_search.

    python3 -m scrapers.discord_threads            # fetch new messages, summarize new windows, index - all three
    python3 -m scrapers.discord_threads fetch      # only pull new messages to data/cache/discord_threads/<id>.json
    python3 -m scrapers.discord_threads summarize  # only extract facts from windows not done before (cloud model)
    python3 -m scrapers.discord_threads pending N [SINCE]  # the next N unsummarized windows (ending >= SINCE) to
                                                   # data/cache/discord/pending/
    python3 -m scrapers.discord_threads accept F   # merge facts written by hand/another model: F = {"<key>": [facts]}
    python3 -m scrapers.discord_threads index      # only write the RAG file and push new chunks to Pinecone
    python3 -m scrapers.discord_threads --demo     # self-check, no network

1. FETCH (the logged-in Discord profile of knowledge/orna_discord_search - a bot can't read a server it wasn't
   invited to). Each forum's threads are listed by last activity; a thread whose last message id is the one
   already saved is skipped, and the listing stops at the first such thread (everything after it is older).
   The early stop needs a COMPLETE earlier run (_state.json): after an interrupted first run, the newest
   threads are saved and the rest are not, so every thread is checked instead. A changed thread is read from its saved cursor on (`/messages?after=`, 100 a request, paced), saved after
   every page, so an interrupted run resumes. Measured 2026-11-03: #discussion-threads has 771 threads and
   ~207k messages - a first run is ~2,100 requests (~3 h); later runs read only the delta. Author names are
   never stored (same rule as orna_discord_search), images aren't transcribed.
2. SUMMARIZE: each thread is cut into chronological windows of ~WINDOW_CHARS; a cloud model (SUMMARY_MODEL,
   deepseek-v4.1-flash - the owner's choice 2026-11-03; the local qwen took ~25 s a window, ~12 h for the first
   run) turns each into self-contained facts as JSON. The first run's windows were summarized by Claude through
   `pending`/`accept`, with the same prompt. Cached by window (thread id + first/last message id), so only
   a thread's new tail is summarized again. Opinions, suggestions and complaints are dropped by the prompt.
3. INDEX: data/cache/discord/threads_knowledge.txt ("=== <thread>, <from>..<to> ===" sections, each dated so
   newer statements can win - see _SOURCE_NOTES in telegram_orna) is the corpus of the "discord_threads"
   namespace. Only chunks not uploaded before go to Pinecone; chunks of a window that grew are deleted.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import sys
import time

import httpx

import paths
from knowledge import orna_curated, orna_pinecone
from orna.orna_discord import clean_markup, snowflake_ms

logger = logging.getLogger(__name__)

GUILD = "448527960056791051"     # Orna: The GPS RPG (official)
# forum channel id -> name. Not read: #devlogs (2 posts, under MIN_MESSAGES), #beta-feedback, #orn-feedback, #bug-reports (reports, not knowledge),
# #kingdoms, #party-play, #build-showcase, #classic-* - add one here to read it.
FORUMS = {"1084643200725164062": "discussion-threads"}
RAW_DIR = paths.CACHE / "discord_threads"
FACTS = paths.CACHE / "discord" / "thread_facts.json"
UPLOADED = paths.CACHE / "discord" / "thread_uploaded.json"
NS = "discord_threads"
MIN_CHARS = 30          # "lol", "+1", "same" carry nothing
# Scope (owner, 2026-11-03): threads with more than 50 messages, and only what was said since 2025 - older
# threads are neither fetched nor summarized, a window that ends before SINCE is skipped.
MIN_MESSAGES = 51
SINCE = "2025-01-01"
WINDOW_CHARS = 9000     # ~2.5k tokens of chat per local-model call
LINE_CHARS = 1500
MODEL = os.environ.get("SUMMARY_MODEL", "deepseek-v4.1-flash")
TIMEOUT = httpx.Timeout(300.0, connect=10.0)
STATE = RAW_DIR / "_state.json"
PENDING = paths.CACHE / "discord" / "pending"


def _load(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _save(path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def _message(m: dict):
    """An API message -> {"id","ts","text"}, or None (joins, pins, one-word replies, bare links)."""
    if m.get("type") not in (0, 19):
        return None
    text = clean_markup(m.get("content") or "")
    if len(re.sub(r"https?://\S+", "", text).strip()) < MIN_CHARS:
        return None
    return {"id": m["id"], "ts": (m.get("timestamp") or "")[:10], "text": text}


# --- 1. fetch ---

def _threads(browser, forum: str) -> list:
    """The forum's threads, most recently active first, up to the first one already saved unchanged."""
    out, offset = [], 0
    complete = _load(STATE, {}).get("complete")
    while True:
        d = browser.get(f"https://discord.com/api/v9/channels/{forum}/threads/search?archived=true"
                        f"&sort_by=last_message_time&sort_order=desc&limit=25&offset={offset}")
        if not isinstance(d, dict):
            raise RuntimeError(f"#{FORUMS[forum]}: the thread listing could not be read")
        for t in d.get("threads") or []:
            if not in_scope(t):
                continue
            if _load(RAW_DIR / f"{t['id']}.json", {}).get("last") == t.get("last_message_id"):
                if complete:
                    return out           # sorted by activity: every thread after this one is unchanged too
                continue
            out.append(t)
        if not d.get("has_more"):
            return out
        offset += 25


def in_scope(t: dict) -> bool:
    """A listed thread worth reading: big enough, and active since SINCE."""
    last = t.get("last_message_id") or t["id"]
    since_ms = time.mktime(time.strptime(SINCE, "%Y-%m-%d")) * 1000
    return (t.get("message_count") or 0) >= MIN_MESSAGES and snowflake_ms(last) >= since_ms


def fetch() -> int:
    from knowledge.orna_discord_search import _browser
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    n = 0
    try:
        for forum, fname in FORUMS.items():
            changed = _threads(_browser, forum)
            print(f"#{fname}: {len(changed)} threads with new messages", file=sys.stderr, flush=True)
            for i, t in enumerate(changed, 1):
                path = RAW_DIR / f"{t['id']}.json"
                rec = _load(path, None) or {"id": t["id"], "forum": fname, "messages": [], "last": "0"}
                rec.update(name=t.get("name", ""), message_count=t.get("message_count") or 0, created=(t.get("thread_metadata") or {}).get("create_timestamp", "")[:10])
                before = len(rec["messages"])
                while True:
                    page = _browser.get(f"https://discord.com/api/v9/channels/{t['id']}/messages?limit=100&after={rec['last']}")
                    if not isinstance(page, list):
                        raise RuntimeError(f"thread {t['id']}: messages could not be read")
                    if not page:
                        break
                    rec["last"] = str(max(int(m["id"]) for m in page))
                    rec["messages"] += sorted((x for x in map(_message, page) if x), key=lambda x: int(x["id"]))
                    _save(path, rec)            # every page: an interrupted run resumes here
                    if len(page) < 100:
                        break
                if t.get("last_message_id") and int(rec["last"]) < int(t["last_message_id"]):
                    rec["last"] = t["last_message_id"]   # the newest was deleted: don't re-read forever
                _save(path, rec)
                n += len(rec["messages"]) - before
                print(f"  {i}/{len(changed)} {rec['name'][:60]}: +{len(rec['messages']) - before}",
                      file=sys.stderr, flush=True)
        _save(STATE, {"complete": True, "at": time.strftime("%Y-%m-%d %H:%M")})
    finally:
        _browser.close()
    return n


# --- 2. summarize ---

def windows(rec: dict) -> list:
    """Chronological windows of a thread's messages: [(key, from_date, to_date, first_id, lines)]. Greedy from
    the start, so earlier windows - and their cached facts - stay the same as the thread grows."""
    out, cur, size = [], [], 0

    def close():
        if cur:
            k = hashlib.sha1(f"{rec['id']}|{cur[0]['id']}|{cur[-1]['id']}".encode()).hexdigest()[:12]
            out.append((k, cur[0]["ts"], cur[-1]["ts"], cur[0]["id"],
                        [f"[{m['ts']}] {m['text'][:LINE_CHARS].replace(chr(10), ' / ')}" for m in cur]))

    for m in rec["messages"]:
        n = min(len(m["text"]), LINE_CHARS) + 14
        if cur and size + n > WINDOW_CHARS:
            close()
            cur, size = [], 0
        cur.append(m)
        size += n
    close()
    return out


_PROMPT = ("Below is part of a player discussion thread from the official Discord of the mobile game Orna "
           "(thread: \"{name}\", {start} to {end}). Each line is one message, with its date.\n\n{chat}\n\n"
           "Extract the GAME KNOWLEDGE in it: facts, numbers, formulas, drop sources, mechanics, how a feature works, "
           "what a patch changed, and strategies players confirm work. Each fact must stand alone: name the item, "
           "class, skill or system it is about, and give the date (YYYY-MM-DD) of the message it comes from. Mark a "
           "claim others disputed with \"(disputed)\", and one said by an Orna developer with \"(dev)\" if the "
           "chat makes that clear. Leave out opinions, suggestions, wishes, complaints, jokes and questions nobody "
           "answered. Do not add anything the chat doesn't say.\n"
           "Answer as JSON: {{\"facts\": [\"<fact>\", ...]}} - an empty list when there is no game knowledge.")


async def _summarize(name: str, start: str, end: str, lines: list) -> list:
    from llm.ollama_client import OLLAMA_CLOUD_HOST, chat_json
    out = await chat_json(OLLAMA_CLOUD_HOST, MODEL, [{"role": "user", "content": _PROMPT.format(
        name=name, start=start, end=end, chat="\n".join(lines))}],
        {"Authorization": f"Bearer {os.environ.get('OLLAMA_API_KEY', '')}"}, timeout=TIMEOUT, think=False)
    return _clean_facts(out.get("facts"))


def _clean_facts(facts) -> list:
    return [str(f).strip()[:600] for f in facts or [] if str(f).strip()]


def _scoped() -> list:
    """(rec, window) for every in-scope window of every saved thread, newest thread first."""
    return [(rec, w) for p in sorted(RAW_DIR.glob("[0-9]*.json"), key=lambda p: -int(p.stem))
            for rec in [_load(p, {})] if rec.get("forum") in FORUMS.values()
            and rec.get("message_count", MIN_MESSAGES) >= MIN_MESSAGES
            for w in windows(rec) if w[2] >= SINCE]


def _todo() -> list:
    facts = _load(FACTS, {})
    return [(rec, w) for rec, w in _scoped() if w[0] not in facts]


def pending(n: int, since: str = SINCE) -> int:
    """The next n unsummarized windows (ending on or after `since`) as text files, prompt included, for a
    summarizer outside this script."""
    PENDING.mkdir(parents=True, exist_ok=True)
    todo = [t for t in _todo() if t[1][2] >= since][:n]
    for rec, (k, start, end, _first, lines) in todo:
        (PENDING / f"{k}.txt").write_text(_PROMPT.format(name=rec["name"], start=start, end=end,
                                                         chat="\n".join(lines)), encoding="utf-8")
    print(f"{len(todo)} written to {PENDING} ({len(_todo())} unsummarized in all)", file=sys.stderr)
    return len(todo)


def accept(path) -> int:
    """Merge {"<window key>": [facts]} into the facts cache; drops the matching pending files."""
    facts, got = _load(FACTS, {}), json.loads(open(path, encoding="utf-8").read())
    for k, v in got.items():
        facts[k] = _clean_facts(v)
        (PENDING / f"{k}.txt").unlink(missing_ok=True)
    _save(FACTS, facts)
    return len(got)


def summarize() -> int:
    from llm.ollama_client import OllamaError
    facts = _load(FACTS, {})
    todo = _todo()
    print(f"{len(todo)} windows to summarize with {MODEL}", file=sys.stderr, flush=True)
    for i, (rec, (k, start, end, _first, lines)) in enumerate(todo, 1):
        t0 = time.time()
        try:
            got = asyncio.run(_summarize(rec["name"], start, end, lines))
        except OllamaError as e:                 # not cached: tried again next run
            logger.warning("window %s of %r: %s", k, rec["name"], e)
            continue
        facts[k] = got
        _save(FACTS, facts)
        print(f"  {i}/{len(todo)} {rec['name'][:50]} {start}..{end}: {len(got)} facts ({time.time() - t0:.0f}s)",
              file=sys.stderr, flush=True)
    return len(todo)


# --- 3. index ---

def write_corpus() -> int:
    """Rewrite threads_knowledge.txt from the current windows (a grown window's old facts drop out)."""
    facts = _load(FACTS, {})
    entries = []
    for rec, (k, start, end, first, _lines) in _scoped():
        if facts.get(k):
            entries.append((f"{rec['name']} (Discord #{rec['forum']}, {start}..{end}) #{k}",
                            "\n".join(f"- {f}" for f in facts[k]),
                            f"https://discord.com/channels/{GUILD}/{rec['id']}/{first}"))
    from scrapers import orn_feedback          # the ORN's own summaries, same namespace
    entries += orn_feedback.entries()
    orna_curated.save(NS, entries)
    return len(entries)


def _delete(ids: list) -> None:
    host = orna_pinecone._index_host()
    for i in range(0, len(ids), 1000):
        httpx.post(f"https://{host}/vectors/delete", headers=orna_pinecone._headers(), timeout=60,
                   json={"ids": ids[i:i + 1000], "namespace": NS}).raise_for_status()


def index() -> tuple:
    """-> (sections, records upserted, records deleted). Only the difference goes to Pinecone."""
    sections = write_corpus()
    recs = orna_pinecone.records(NS)
    uploaded = set(_load(UPLOADED, []))
    new = [r for r in recs if r["_id"] not in uploaded]
    stale = sorted(uploaded - {r["_id"] for r in recs})
    if orna_pinecone.enabled():
        if new:
            orna_pinecone.upsert(NS, new)
        if stale:
            _delete(stale)
        _save(UPLOADED, sorted(r["_id"] for r in recs))
    return sections, len(new), len(stale)


def _demo() -> None:
    new_id = str((int(time.mktime((2026, 10, 1, 0, 0, 0, 0, 0, 0)) * 1000) - 1420070400000) << 22)
    old_id = str((int(time.mktime((2024, 6, 1, 0, 0, 0, 0, 0, 0)) * 1000) - 1420070400000) << 22)
    assert in_scope({"id": old_id, "last_message_id": new_id, "message_count": 51})
    assert not in_scope({"id": old_id, "last_message_id": new_id, "message_count": 50})
    assert not in_scope({"id": old_id, "last_message_id": old_id, "message_count": 900})
    assert _message({"type": 0, "id": "1", "timestamp": "2026-10-01T00:00", "content": "lol same"}) is None
    assert _message({"type": 7, "id": "1", "content": "x" * 50}) is None
    assert _message({"type": 0, "id": "1", "content": "https://tenor.com/view/some-very-long-gif-link-here"}) is None
    m = _message({"type": 0, "id": "9", "timestamp": "2026-10-01T05:00:00", "author": {"username": "someone"},
                  "content": "<@123> Titan Crucible drops from Titan Mnemosyne again <:orns:55>"})
    assert m == {"id": "9", "ts": "2026-10-01", "text": "Titan Crucible drops from Titan Mnemosyne again"}, m
    rec = {"id": "100", "name": "T", "messages": [{"id": str(i), "ts": f"2026-10-{i:02d}", "text": "y" * 3000}
                                                 for i in range(1, 8)]}
    w = windows(rec)
    assert [len(x[4]) for x in w] == [5, 2] and w[0][1:4] == ("2026-10-01", "2026-10-05", "1"), [len(x[4]) for x in w]
    assert len(w[0][4][0]) == len("[2026-10-01] ") + LINE_CHARS
    rec["messages"].append({"id": "8", "ts": "2026-10-08", "text": "z" * 40})
    w2 = windows(rec)
    assert w2[0][0] == w[0][0] and w2[1][0] != w[1][0]    # only the tail window changes
    print("discord_threads: all checks passed")


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    for noisy in ("selenium", "urllib3", "httpx"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    step = sys.argv[1] if len(sys.argv) > 1 else "all"
    if step == "--demo":
        _demo()
        sys.exit()
    if step == "pending":
        pending(int(sys.argv[2]) if len(sys.argv) > 2 else 20, *sys.argv[3:4])
        sys.exit()
    if step == "accept":
        print("accepted", accept(sys.argv[2]), file=sys.stderr)
        sys.exit()
    if step in ("fetch", "all"):
        print("new messages:", fetch(), file=sys.stderr)
    if step in ("summarize", "all"):
        summarize()
    if step in ("index", "all"):
        print("sections %d, upserted %d, deleted %d" % index(), file=sys.stderr)
