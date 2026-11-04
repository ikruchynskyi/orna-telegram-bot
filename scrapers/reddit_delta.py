"""Add only what's NEW on Reddit to the two Reddit corpora, and only what the knowledge base doesn't have yet.

    python3 -m scrapers.reddit_delta            # fetch, judge, append, upsert the new records
    python3 -m scrapers.reddit_delta --dry-run  # fetch and judge, print what would be added, write nothing

1. Delta: the developers' listings (orna_scrape_reddit._AUTHORS) are read newest first and stop at the first
   item already in orna_reddit.txt (matched by URL); r/OrnaRPG's newest posts that aren't in orna_qa.txt or
   the crawl cache are fetched. Without API keys or REDDIT_COOKIE, Reddit refuses the bot's Chrome every .json
   ("blocked by network security", 2026-11), so the developer listings come from their RSS feeds through that
   Chrome (orna_reddit_search._browser) and the r/OrnaRPG threads are skipped (RSS has no scores).
2. Same filters as the full scrapers: a dev entry needs _MIN_BODY_CHARS and an Orna subreddit; a thread needs
   an answer that passes orna_scrape_qa._block (score, length, no bots); held-out eval threads stay out.
3. Novelty: each candidate is shown to a free cloud model (gemma4) with the 3 closest existing chunks from the
   keyword index; it is kept only if it adds a fact, number, mechanic or correction they don't have (a dev
   statement that UPDATES an old one counts). Verdicts are cached in data/cache/reddit_delta.json.
4. Kept items go in a dated section at the END of each file, so every existing record keeps its id: only the
   new records are upserted to Pinecone (no re-index), and the keyword index rebuilds itself from the files.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time

import paths
from knowledge import orna_pinecone, orna_qa, orna_reddit, orna_textindex
from knowledge.orna_reddit_search import _browser, _comments
from llm.ollama_client import OLLAMA_CLOUD_HOST, OllamaError, chat_json
from scrapers import orna_scrape_qa as QA
from scrapers import orna_scrape_reddit as R
from scrapers.removed_content import scrub

JUDGE_MODEL = os.environ.get("REDDIT_DELTA_MODEL", "gemma4:31b")
VERDICTS = paths.CACHE / "reddit_delta.json"
MAX_PAGES = 10          # 100 items a page; a delta stops far earlier, at the first known item
NEW_POSTS_PAGES = 3     # r/OrnaRPG/new: the newest 300 posts
QA_MIN_AGE_DAYS = 2     # a post younger than this hasn't collected its answers yet


_oauth = {}


def _get(path: str) -> dict:
    """A reddit JSON path: through the API when REDDIT_CLIENT_ID/SECRET are set (a "script" app), with a logged-in
    browser's REDDIT_COOKIE when that is set, else through
    the bot's Chrome - which needs a logged-in Reddit session in data/state/reddit_chrome: since 2026-10 Reddit
    answers anonymous JSON with 403."""
    cid, secret = os.environ.get("REDDIT_CLIENT_ID"), os.environ.get("REDDIT_CLIENT_SECRET")
    if os.environ.get("REDDIT_COOKIE", "").strip() and not (cid and secret):
        # a logged-in browser's cookie string (orna_scrape_qa's convention; .env only, never committed)
        if "sess" not in _oauth:
            _oauth["sess"] = QA._session()
        time.sleep(2)
        for _ in range(3):
            r = _oauth["sess"].get(QA.BASE + path, timeout=30)
            if r.status_code == 429:
                time.sleep(30)
                continue
            r.raise_for_status()
            return r.json()
        raise RuntimeError("reddit: still rate limited after 3 tries")
    if not (cid and secret):
        return _browser.get(path)
    import httpx
    if "token" not in _oauth:
        _oauth["token"] = R._token(cid, secret)
    time.sleep(R.SLEEP_BETWEEN)
    r = httpx.get(R.API_BASE + path, headers={"Authorization": f"bearer {_oauth['token']}", "User-Agent": R.USER_AGENT},
                  timeout=30)
    r.raise_for_status()
    return r.json()


def _api() -> bool:
    """True when _get has a route Reddit still answers: API keys or REDDIT_COOKIE."""
    return bool(os.environ.get("REDDIT_COOKIE", "").strip()
                or (os.environ.get("REDDIT_CLIENT_ID") and os.environ.get("REDDIT_CLIENT_SECRET")))


_ATOM = "{http://www.w3.org/2005/Atom}"


def _rss_listing(feed: str) -> dict:
    """An Atom feed of a /user/<name>/<kind> listing in the .json listing's shape (no scores: RSS has none)."""
    import re
    import xml.etree.ElementTree as ET
    from datetime import datetime
    children = []
    for e in ET.fromstring(feed).iter(f"{_ATOM}entry"):
        md = re.search(r'<div class="md">(.*)</div>', e.findtext(f"{_ATOM}content") or "", re.S)
        text = re.sub(r"<[^>]+>", "", re.sub(r"</p>|<br\s*/?>", "\n", md.group(1) if md else "")).strip()
        title, cat = e.findtext(f"{_ATOM}title") or "", e.find(f"{_ATOM}category")
        when = e.findtext(f"{_ATOM}published") or e.findtext(f"{_ATOM}updated")
        item = {"name": e.findtext(f"{_ATOM}id") or "",
                "permalink": e.find(f"{_ATOM}link").get("href").replace("https://www.reddit.com", ""),
                "subreddit": cat.get("term") if cat is not None else "",
                "created_utc": datetime.fromisoformat(when).timestamp() if when else 0}
        if item["name"].startswith("t1_"):          # a comment's title is "/u/<name> on <post title>"
            item.update(body=text, link_title=title.split(" on ", 1)[-1])
        else:
            item.update(title=title, selftext=text)
        children.append({"data": item})
    return {"data": {"children": children, "after": children[-1]["data"]["name"] if len(children) >= 100 else None}}


def _known_urls(path) -> set:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return set()
    import re
    return {u.rstrip("/").split("?")[0] for u in re.findall(r"https://www\.reddit\.com/r/[^\s)]+", text)}


def dev_candidates(known: set) -> list:
    """New dev entries (formatted like orna_reddit.txt), newest first: (url, entry_text)."""
    out = []
    for user, kind in R._AUTHORS:
        after, stop = None, False
        for _ in range(MAX_PAGES):
            q = f"/user/{user}/{kind}.json?limit=100&raw_json=1" + (f"&after={after}" if after else "")
            data = (_get(q) if _api() else _rss_listing(_browser.get(q.replace(".json", ".rss", 1), raw=True))).get("data") or {}
            items = [c.get("data") or {} for c in data.get("children") or []]
            for item in items:
                url = f"https://www.reddit.com{item.get('permalink', '')}".rstrip("/")
                if url in known:
                    stop = True
                    break
                for entry in R.format_entries(user, kind, [item]):
                    out.append((url, entry))
            after = data.get("after")
            if stop or not after or not items:
                break
        print(f"  u/{user} {kind}: {sum(1 for u, _e in out)} new so far", file=sys.stderr)
    return out


def qa_candidates(known: set) -> list:
    """New r/OrnaRPG threads with answers that pass the corpus filters: (url, block)."""
    heldout = QA._load_eval_ids()
    cutoff = time.time() - QA_MIN_AGE_DAYS * 86400
    out, after = [], None
    for _ in range(NEW_POSTS_PAGES):
        data = _get("/r/OrnaRPG/new.json?limit=100&raw_json=1" + (f"&after={after}" if after else "")).get("data") or {}
        for c in data.get("children") or []:
            p = c.get("data") or {}
            url = f"https://www.reddit.com{p.get('permalink', '')}".rstrip("/")
            if (url in known or p.get("id") in heldout or not p.get("num_comments")
                    or (p.get("created_utc") or 0) > cutoff):
                continue
            cached = QA.CACHE_DIR and os.path.join(QA.CACHE_DIR, f"{p['id']}.json")
            if cached and os.path.exists(cached):
                with open(cached, encoding="utf-8") as fh:
                    comments = json.load(fh)
            else:
                comments = _comments(_get(f"{p['permalink']}.json?limit=50&sort=top&raw_json=1"))
                if cached:
                    os.makedirs(QA.CACHE_DIR, exist_ok=True)
                    with open(cached, "w", encoding="utf-8") as fh:
                        json.dump(comments, fh)
            block = QA._block(p, comments)
            if block:
                out.append((url, block))
        after = data.get("after")
        if not after:
            break
    return out


_JUDGE = ("You maintain a knowledge base for the mobile game Orna. Decide whether a NEW Reddit text adds knowledge "
          "the base does not have yet.\n\nEXISTING KNOWLEDGE (the closest entries):\n{existing}\n\nNEW TEXT:\n{new}\n\n"
          "Answer as JSON: {{\"novel\": true|false, \"why\": \"<one sentence>\"}}. novel=true only if the new text "
          "states a game fact, number, formula, mechanic, strategy, or a correction/update of an older statement "
          "that the existing knowledge does not already contain. Chit-chat, questions without a useful answer, "
          "bug reports, memes and repeats of known facts are novel=false.")


async def _judge(new: str) -> dict:
    hits = await asyncio.to_thread(orna_textindex.search, new[:600], 3)
    existing = "\n---\n".join(h["text"][:1200] for h in hits) or "(nothing related)"
    key = os.environ.get("OLLAMA_API_KEY")
    try:
        return await chat_json(OLLAMA_CLOUD_HOST, JUDGE_MODEL, [{"role": "user", "content": _JUDGE.format(
            existing=existing, new=new[:3000])}], {"Authorization": f"Bearer {key}"} if key else {})
    except OllamaError as e:
        return {"novel": None, "why": f"judge failed: {e}"}


def _append(path, section: str, blocks: list) -> None:
    text = path.read_text(encoding="utf-8").rstrip("\n")
    path.write_text(text + f"\n\n=== {section} ===\n" + "\n\n".join(b.strip("\n") for b in blocks) + "\n",
                    encoding="utf-8")


def _upsert_tail(ns: str, before: int) -> int:
    """Upsert the records of the units added after the first `before` ones (their ids are new)."""
    positional = sum(1 for u in orna_pinecone._units(ns) if len(u) == 4)   # keyed units (live threads) come after
    added = set(range(before, positional))
    recs = [r for r in orna_pinecone.records(ns) if r["_id"].split("-")[1].isdigit() and int(r["_id"].split("-")[1]) in added]
    if recs and orna_pinecone.enabled():
        orna_pinecone.upsert(ns, recs)
    return len(recs)


async def main(dry: bool) -> None:
    verdicts = json.loads(VERDICTS.read_text()) if VERDICTS.exists() else {}
    today = time.strftime("%Y-%m-%d")
    try:
        print("reading new developer posts and comments...", file=sys.stderr)
        devs = dev_candidates(_known_urls(R.OUTPUT_PATH if hasattr(R, "OUTPUT_PATH") else orna_reddit.DATA_PATH))
        if _api():
            print(f"reading r/OrnaRPG's newest threads...", file=sys.stderr)
            qas = qa_candidates(_known_urls(paths.DATA / "orna_qa.txt"))
        else:   # the answer filter ranks by score, which RSS doesn't carry
            print("r/OrnaRPG threads skipped: no API key or REDDIT_COOKIE, and RSS has no scores", file=sys.stderr)
            qas = []
    finally:
        _browser.close()
    print(f"candidates: {len(devs)} developer entries, {len(qas)} threads", file=sys.stderr)

    kept = {"reddit": [], "qa": []}
    for ns, items in (("reddit", devs), ("qa", qas)):
        for url, text in items:
            v = verdicts.get(url) or await _judge(text)
            if v.get("novel") is not None:
                verdicts[url] = v
            print(f"  [{ 'NEW ' if v.get('novel') else 'skip'}] {ns:6} {text.splitlines()[0][:90]} - {v.get('why', '')[:100]}")
            if v.get("novel"):
                kept[ns].append(scrub(text))
    VERDICTS.write_text(json.dumps(verdicts, ensure_ascii=False, indent=1))
    print(f"novel: {len(kept['reddit'])} developer entries, {len(kept['qa'])} threads", file=sys.stderr)
    if dry or not (kept["reddit"] or kept["qa"]):
        return

    before = {ns: sum(1 for u in orna_pinecone._units(ns) if len(u) == 4) for ns in ("reddit", "qa")}
    if kept["reddit"]:
        _append(orna_reddit.DATA_PATH, f"Developer posts added {today} (reddit_delta, developer commentary)", kept["reddit"])
        orna_reddit._entries = None
    if kept["qa"]:
        # orna_qa.txt is a run of "=== thread ===" blocks; new ones simply go at the end
        path = paths.DATA / "orna_qa.txt"
        path.write_text(path.read_text(encoding="utf-8").rstrip("\n") + "\n\n" + "\n".join(kept["qa"]), encoding="utf-8")
        orna_qa._THREADS = None
    for ns in ("reddit", "qa"):
        if kept[ns]:
            print(f"{ns}: {_upsert_tail(ns, before[ns])} new records upserted to Pinecone", file=sys.stderr)


def _demo() -> None:
    feed = ('<feed xmlns="http://www.w3.org/2005/Atom"><entry><category term="OrnaRPG"/><content type="html">'
            '&lt;div class="md"&gt;&lt;p&gt;Sites rotate daily.&lt;/p&gt;&lt;/div&gt;</content><id>t1_abc</id>'
            '<link href="https://www.reddit.com/r/OrnaRPG/comments/1x/t/abc/"/><updated>2026-10-29T16:58:48+00:00</updated>'
            '<title>/u/OrnaOdie on Gleaming mobs</title></entry></feed>')
    d = _rss_listing(feed)["data"]
    it = d["children"][0]["data"]
    assert it["body"] == "Sites rotate daily." and it["link_title"] == "Gleaming mobs" and d["after"] is None
    assert it["permalink"] == "/r/OrnaRPG/comments/1x/t/abc/" and it["subreddit"] == "OrnaRPG" and it["created_utc"] > 0
    print("ok")


if __name__ == "__main__":
    if "--demo" in sys.argv:
        _demo()
    else:
        asyncio.run(main("--dry-run" in sys.argv))
