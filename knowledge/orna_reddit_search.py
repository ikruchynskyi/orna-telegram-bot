"""Live search of r/OrnaRPG, through a real Chrome on a dedicated profile.

WHY A BROWSER. Anonymous API-style access 403s (see orna_scrape_reddit.py).
Measured 2026-10-06: a HEADLESS Chrome also gets 403 - but a normal Chrome
window gets 200 from /r/OrnaRPG/search.json even logged out. So: a real,
non-headless Chrome, moved off-screen so nothing pops up, fetching Reddit's own
JSON from inside a reddit.com page. No login is needed; `login` exists in case
Reddit starts requiring one (Google sign-in refuses an automated browser - log
in once in a NORMAL Chrome on this profile instead, see README).

WHAT. Reddit search is keyword search: all words must match ("prometheus
sigil" -> 0 posts), so a miss is retried with the words OR'ed, ranked by
relevance ("prometheus OR sigil" finds "Replica of Prometheus"; ranked by top,
an OR query returns popular threads that merely mention a word). A thread is
rendered exactly like the orna_qa corpus (orna_scrape_qa._block: the question,
its top answers with upvotes, DEV for Orna's developers, no other names). A
thread already in that corpus is rendered from its cached comments - no
request - and a new one is KEPT (.reddit_cache/live.json), then upserted into
the same "qa" namespace, so the next similar question finds it through
knowledge_search.

Run: python3 -m knowledge.orna_reddit_search search <query>   one live search, printed
     python3 -m knowledge.orna_reddit_search                  self-check (no browser)
"""
from __future__ import annotations

import paths
import json
import logging
import os
import sys
import threading
import time
from pathlib import Path
from typing import Optional
from urllib.parse import quote

logger = logging.getLogger(__name__)

PROFILE_DIR = paths.STATE / "reddit_chrome"
CACHE_DIR = paths.CACHE / "reddit"
QA_CACHE = paths.QA_CRAWL                # orna_scrape_qa's crawl: one <post id>.json per thread
LIVE_FILE = CACHE_DIR / "live.json"
SITE = "https://www.reddit.com"
MIN_INTERVAL = 2.0                     # seconds between requests
THREADS = 3                            # threads rendered per search
RATE_LIMIT_WAIT = 30

_FETCH = ("return fetch(arguments[0]).then(r => r.text().then(t => ({status: r.status, body: t})))"
          ".catch(e => ({status: 0, body: String(e)}));")


class _Browser:
    """One off-screen Chrome, one request at a time, closed after each search."""

    def __init__(self):
        self.lock = threading.Lock()
        self.drv = None
        self.last = 0.0

    def _page(self):
        if self.drv is None:
            from selenium import webdriver
            opts = webdriver.ChromeOptions()
            opts.add_argument(f"--user-data-dir={PROFILE_DIR}")
            opts.add_argument("--window-position=-3000,-3000")     # real window, off-screen
            opts.add_argument("--window-size=1200,900")
            self.drv = webdriver.Chrome(options=opts)
            self.drv.get(f"{SITE}/r/OrnaRPG/")
        return self.drv

    def close(self):
        if self.drv is not None:
            try:
                self.drv.quit()
            finally:
                self.drv = None

    def get(self, path: str):
        """GET a reddit.com JSON path from inside the page. Waits out a 429."""
        with self.lock:
            for _attempt in range(3):
                drv = self._page()
                wait = MIN_INTERVAL - (time.time() - self.last)
                if wait > 0:
                    time.sleep(wait)
                res = drv.execute_script(_FETCH, path)
                self.last = time.time()
                if res["status"] == 200:
                    return json.loads(res["body"])
                if res["status"] == 429:
                    logger.info("reddit: rate limited, waiting %ss", RATE_LIMIT_WAIT)
                    time.sleep(RATE_LIMIT_WAIT)
                    continue
                raise RuntimeError(f"reddit refused {path.split('?')[0]} (HTTP {res['status']})")
        raise RuntimeError("reddit: still rate limited after 3 tries")


_browser = _Browser()


def _variants(query: str) -> list:
    """All the query's distinctive words, then the same words OR'ed."""
    from knowledge.orna_discord_search import _keywords
    kw = _keywords(query) or query.split()[:1]
    out = [" ".join(kw)] + ([" OR ".join(kw)] if len(kw) > 1 else [])
    return [q for q in out if q]


def _comments(blob) -> list:
    """Top-level comments of a /comments/<id>.json response, in orna_scrape_qa's shape."""
    out = []
    if isinstance(blob, list) and len(blob) > 1:
        for k in (blob[1].get("data") or {}).get("children") or []:
            c = k.get("data") or {}
            if k.get("kind") == "t1" and c.get("body"):
                out.append({"score": c.get("score", 0), "author": c.get("author", ""), "body": c["body"]})
    return out


def _load_live() -> dict:
    try:
        return json.loads(LIVE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def search(query: str, threads: int = THREADS) -> list:
    """Threads answering `query`, most relevant first: [{"id", "title", "url",
    "block", "new"}]. "new" = not yet in the corpus or the kept store - those
    are what save_live keeps. Blocking (Selenium) - call through to_thread."""
    from scrapers.orna_scrape_qa import _block
    live = _load_live()
    out = []
    try:
        posts = []
        for q in _variants(query):
            data = _browser.get(f"/r/OrnaRPG/search.json?q={quote(q)}&restrict_sr=1&sort=relevance"
                                f"&t=all&limit=15&raw_json=1")
            posts = [c["data"] for c in (data.get("data") or {}).get("children") or []
                     if (c.get("data") or {}).get("num_comments", 0) > 0]
            if posts:
                break
        for p in posts:
            if len(out) >= threads:
                break
            cached = QA_CACHE / f"{p['id']}.json"
            if p["id"] in live:
                block, new = live[p["id"]], False
            elif cached.exists():
                block, new = _block(p, json.loads(cached.read_text())), False
            else:
                block = _block(p, _comments(_browser.get(f"{p['permalink']}.json?limit=50&sort=top&raw_json=1")))
                new = True
            if block:       # "" = no answer passed the corpus's own filters
                out.append({"id": p["id"], "title": p.get("title", ""), "url": SITE + p.get("permalink", ""),
                            "block": block, "new": new})
    finally:
        _browser.close()
    return out


def save_live(threads: list) -> list:
    """Keep the new threads; returns their ids."""
    live = _load_live()
    new = [t for t in threads if t["new"] and t["id"] not in live]
    if new:
        CACHE_DIR.mkdir(exist_ok=True)
        live.update({t["id"]: t["block"] for t in new})
        tmp = LIVE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(live, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, LIVE_FILE)
    return [t["id"] for t in new]


def live_threads() -> list:
    """(post id, orna_qa.Thread) per kept thread - parsed by orna_qa's own reader."""
    from knowledge import orna_qa
    out = []
    for pid, block in _load_live().items():
        out.extend((pid, th) for th in orna_qa._parse(block))
    return out


def _demo() -> None:
    global LIVE_FILE, CACHE_DIR
    import tempfile
    assert _variants("How does the Prometheus sigil work?") == ["prometheus sigil", "prometheus OR sigil"]
    assert _variants("ward") == ["ward"]
    blob = [{}, {"data": {"children": [
        {"kind": "t1", "data": {"score": 5, "author": "OrnaOdie", "body": "Summons are not followers, no."}},
        {"kind": "more", "data": {}}]}}]
    assert _comments(blob) == [{"score": 5, "author": "OrnaOdie", "body": "Summons are not followers, no."}]
    from scrapers.orna_scrape_qa import _block
    block = _block({"id": "x1", "title": "Are summons followers?", "created_utc": 1700000000, "score": 3,
                    "permalink": "/r/OrnaRPG/comments/x1/"}, _comments(blob))
    assert "A [5up DEV]: Summons are not followers" in block and "OrnaOdie" not in block, block
    real = (LIVE_FILE, CACHE_DIR)
    with tempfile.TemporaryDirectory() as tmp:
        CACHE_DIR, LIVE_FILE = Path(tmp), Path(tmp) / "live.json"
        try:
            t = {"id": "x1", "title": "", "url": "", "block": block, "new": True}
            assert save_live([t]) == ["x1"] and save_live([t]) == []          # kept once
            assert save_live([{**t, "id": "x2", "new": False}]) == []          # a corpus thread is not re-kept
            ((pid, th),) = live_threads()
            assert pid == "x1" and th.title == "Are summons followers?" and th.answers, th
        finally:
            LIVE_FILE, CACHE_DIR = real
    print("orna_reddit_search: _demo ok")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    for noisy in ("selenium", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    if sys.argv[1:2] == ["search"]:
        for t in search(" ".join(sys.argv[2:])):
            print(("NEW " if t["new"] else "") + t["block"])
    else:
        _demo()
