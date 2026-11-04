"""Knowledge from the official Orna Discord's #orn-feedback forum: one thread per event/topic in which the ORN
(Orna Representative Network) keeps a summary of the community's feedback and marks what Northern Forge
resolved. ORN members edit their first post rather than adding messages, so every run re-reads every thread
(42 threads, small) and a thread whose text changed needs its facts redone.

Facts are written by hand (or by any summarizer) into FACTS as {thread_id: {"hash": <text hash>, "facts": [...]}};
discord_threads.write_corpus() adds them to the same "discord_threads" corpus and Pinecone namespace.

    python3 -m scrapers.orn_feedback fetch     # re-read the forum; lists threads whose facts are missing/stale
    python3 -m scrapers.orn_feedback stale     # the same list, without fetching
    python3 -m scrapers.orn_feedback --demo
"""

from __future__ import annotations

import hashlib
import json
import sys

import paths

FORUM = "1122705721281220628"    # #orn-feedback
GUILD = "448527960056791051"
RAW_DIR = paths.CACHE / "orn_feedback"
FACTS = paths.CACHE / "discord" / "orn_feedback_facts.json"
SINCE = ""                       # all threads: the ORN summaries are short and dated (owner, 2026-11-04)


def _load(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def text_hash(rec: dict) -> str:
    return hashlib.sha1("\n".join(m["text"] for m in rec["messages"]).encode("utf-8")).hexdigest()[:12]


def fetch() -> int:
    from knowledge.orna_discord_search import _browser
    from scrapers.discord_threads import _message
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    n, offset = 0, 0
    try:
        threads = []
        while True:
            d = _browser.get(f"https://discord.com/api/v9/channels/{FORUM}/threads/search?archived=true"
                             f"&sort_by=last_message_time&sort_order=desc&limit=25&offset={offset}")
            if not isinstance(d, dict):
                raise RuntimeError("#orn-feedback: the thread listing could not be read")
            threads += d.get("threads") or []
            if not d.get("has_more"):
                break
            offset += 25
        for t in threads:
            msgs, after = [], "0"
            while True:
                page = _browser.get(f"https://discord.com/api/v9/channels/{t['id']}/messages?limit=100&after={after}")
                if not isinstance(page, list):
                    raise RuntimeError(f"thread {t['id']}: messages could not be read")
                if not page:
                    break
                after = str(max(int(m["id"]) for m in page))
                msgs += [x for x in map(_message, page) if x]
                if len(page) < 100:
                    break
            msgs.sort(key=lambda x: int(x["id"]))
            rec = {"id": t["id"], "name": t.get("name", ""), "messages": msgs,
                   "created": (t.get("thread_metadata") or {}).get("create_timestamp", "")[:10]}
            (RAW_DIR / f"{t['id']}.json").write_text(json.dumps(rec, ensure_ascii=False), encoding="utf-8")
            n += 1
    finally:
        _browser.close()
    return n


def records() -> list:
    """Saved threads in scope (last message since SINCE), newest first."""
    recs = [r for p in RAW_DIR.glob("[0-9]*.json") for r in [_load(p, {})] if r.get("messages")]
    return sorted((r for r in recs if r["messages"][-1]["ts"] >= SINCE), key=lambda r: -int(r["id"]))


def stale() -> list:
    """Threads in scope whose facts are missing or were written for different text."""
    facts = _load(FACTS, {})
    return [r for r in records() if facts.get(r["id"], {}).get("hash") != text_hash(r)]


def entries() -> list:
    """[(title, body, url)] for the corpus - only threads whose facts match their current text."""
    facts, out = _load(FACTS, {}), []
    for r in records():
        f = facts.get(r["id"])
        if f and f.get("facts") and f.get("hash") == text_hash(r):
            out.append((f"ORN feedback: {r['name']} (Discord #orn-feedback, {r['created']}..{r['messages'][-1]['ts']})",
                        "\n".join(f"- {x}" for x in f["facts"]), f"https://discord.com/channels/{GUILD}/{r['id']}"))
    return out


def _demo() -> None:
    r = {"id": "1", "name": "t", "created": "2026-01-01", "messages": [{"id": "1", "ts": "2026-01-01", "text": "a"}]}
    assert text_hash(r) == text_hash(dict(r)) and text_hash(r) != text_hash({**r, "messages": [{**r["messages"][0], "text": "b"}]})
    print("ok")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "--demo":
        _demo()
    elif cmd in ("fetch", "stale"):
        if cmd == "fetch":
            print(f"{fetch()} threads read", file=sys.stderr)
        for r in stale():
            print(r["id"], text_hash(r), r["created"], r["name"])
    else:
        print(__doc__)
