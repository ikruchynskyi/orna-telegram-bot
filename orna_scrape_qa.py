#!/usr/bin/env python3
"""
orna_scrape_qa.py - build `orna_qa.txt`: community QUESTIONS from r/OrnaRPG
paired with their best-voted ANSWERS.

Why this earns a place next to four existing corpora: it is the only one indexed
by the QUESTION as a player phrases it. `orna_knowledge` is tabular (a row IS the
answer), `orna_reddit` holds dev prose, `orna_echo` holds guide sections,
`orna_guides` holds whole class guides - all indexed by the ANSWER's wording. A
player's incoming question resembles a past player's question far more than it
resembles any answer, and the live failure that motivated this is exactly that:
the Vritra Charm answer WAS in `orna_reddit.txt`, and three `knowledge_search`
calls missed it because the model searched the ITEM name while the text is
indexed by the MECHANIC.

It also carries the one kind of knowledge no other source has: a CORRECTED
PREMISE. "How to get multiple followers?" is answered by "you're fighting a
summoner, those are summons, not followers" - a misconception fix that exists
only in community Q&A.

REDDIT NEEDS A LOGGED-IN SESSION. Anonymous access 403s (see
orna_scrape_reddit's docstring and CLAUDE.md). Pass a browser session cookie
via the environment - NEVER hardcode or commit one:

    REDDIT_COOKIE="$(cat /path/to/cookie)" python3 orna_scrape_qa.py

Resumable on purpose: a full crawl is one HTTP request per post and runs for
tens of minutes, so every post's raw comment JSON is cached under
`.qa_cache/` (gitignored) and a re-run skips what it already has. Re-run after a
network drop rather than starting over.

HELD OUT: `_EVAL_POST_IDS` are the six threads used as a blind evaluation set
(CLAUDE.md, "Benchmarking against public answers does not work"). They are
crawled but written to `orna_qa_heldout.txt` instead, so the corpus cannot
answer the questions it is being judged on. Note the hold-out is NOT airtight -
`web_search` reaches the live threads anyway - so it is hygiene, not a
guarantee.
"""
from __future__ import annotations

import html as _html
import json
import os
import re
import sys
import time
from datetime import datetime, timezone

import requests

BASE = "https://www.reddit.com"
OUT_PATH = "orna_qa.txt"
HELDOUT_PATH = "orna_qa_heldout.txt"
CACHE_DIR = ".qa_cache"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36")
# 1.0s was NOT enough: a first full run got 429 Too Many Requests after ~88
# threads and lost the other 700. Reddit rate-limits a browser-cookie session
# fairly tightly, so this backs off politely and the crawl is resumable
# (.qa_cache) rather than fast.
DELAY_SECONDS = 5.0
TIMEOUT = 45
# On a 429: wait Retry-After if given, else escalate. Give up the whole crawl
# after this many CONSECUTIVE 429s rather than hammering a service that has
# just asked us to stop - the cache means a later run resumes.
_RATE_LIMIT_BACKOFFS = (30, 60, 120, 240)
_MAX_CONSECUTIVE_429 = len(_RATE_LIMIT_BACKOFFS)


class RateLimited(RuntimeError):
    """Reddit asked us to stop. The caller stops the crawl and keeps what it has."""
MAX_PAGES = 12                 # runaway guard, not a target - paging stops on a null cursor

# Keep the top few answers per thread. More than this is diminishing: a 1-upvote
# reply is rarely the answer, and the corpus should stay searchable.
MAX_ANSWERS = 3
MIN_ANSWER_SCORE = 2
MIN_ANSWER_CHARS = 25
DEV_AUTHORS = {"ornaodie", "widogeist"}

# The blind-evaluation threads - crawled, but written to the held-out file.
_EVAL_POST_IDS = {"1p6ofmk", "1q59lgw", "1smlan2"}   # replaced at runtime, see _load_eval_ids

# Listing seams. flair:QUESTION is the richest, and several sorts overlap
# heavily but each reaches posts the others miss.
_SEAMS = [
    ("search", {"q": "flair:QUESTION", "restrict_sr": "1", "sort": "top", "t": "all"}),
    ("search", {"q": "flair:QUESTION", "restrict_sr": "1", "sort": "top", "t": "year"}),
    ("search", {"q": "flair:QUESTION", "restrict_sr": "1", "sort": "comments", "t": "all"}),
    ("search", {"q": "flair:QUESTION", "restrict_sr": "1", "sort": "new", "t": "all"}),
]

_WS_RE = re.compile(r"[ \t ]+")
_URL_RE = re.compile(r"https?://\S+")


def _session() -> requests.Session:
    cookie = os.environ.get("REDDIT_COOKIE", "").strip()
    if not cookie:
        sys.exit("REDDIT_COOKIE is not set - export a logged-in browser cookie string "
                 "(never commit it). Anonymous reddit access 403s.")
    s = requests.Session()
    s.headers.update({"cookie": cookie, "user-agent": UA,
                      "accept": "text/html,application/xhtml+xml,*/*;q=0.8",
                      "accept-language": "en-US,en;q=0.9"})
    return s


def _clean(text: str) -> str:
    text = _html.unescape(text or "")
    text = _URL_RE.sub(lambda m: m.group(0)[:60], text)
    return _WS_RE.sub(" ", text.replace("\r", " ").replace("\n", " ")).strip()


_POSTS_CACHE = "_posts.json"


def list_posts(sess: requests.Session, progress=True) -> list:
    """Every QUESTION-flaired post the listing seams reach, deduped by id.

    CACHED, because the listing is the one part a rate-limited resume cannot
    redo: a second run hit 429 on every seam and ended up with zero posts, which
    would have thrown away a part-built corpus. With the listing on disk a
    resume only needs the per-post fetches it is actually missing."""
    os.makedirs(CACHE_DIR, exist_ok=True)
    cache_path = os.path.join(CACHE_DIR, _POSTS_CACHE)
    seen: dict = {}
    if os.path.exists(cache_path):
        try:
            with open(cache_path) as fh:
                seen = {p["id"]: p for p in json.load(fh) if p.get("id")}
            if progress:
                print(f"  {len(seen)} posts from the listing cache", file=sys.stderr)
        except Exception:
            seen = {}
    for path, params in _SEAMS:
        after = None
        for page in range(MAX_PAGES):
            q = dict(params, limit="100", raw_json="1")
            if after:
                q["after"] = after
            r = sess.get(f"{BASE}/r/OrnaRPG/{path}.json", params=q, timeout=TIMEOUT)
            if r.status_code == 429:
                # Being throttled on the LISTING is terminal for this run - but
                # only for this run, since both the listing and the per-post
                # answers are cached.
                if progress:
                    print(f"  {params.get('sort')}/{params.get('t')} page{page}: 429 - "
                          "stopping the listing pass", file=sys.stderr)
                break
            if r.status_code != 200:
                if progress:
                    print(f"  {path} {params.get('sort')} page{page}: http {r.status_code}", file=sys.stderr)
                break
            data = r.json().get("data") or {}
            kids = data.get("children") or []
            for k in kids:
                p = k.get("data") or {}
                if p.get("id"):
                    seen.setdefault(p["id"], p)
            after = data.get("after")
            if progress:
                print(f"  {params.get('sort')}/{params.get('t')} page{page}: +{len(kids)} "
                      f"(total unique {len(seen)})", file=sys.stderr)
            if not after or not kids:
                break
            time.sleep(DELAY_SECONDS)
    if seen:
        tmp = cache_path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(list(seen.values()), fh)
        os.replace(tmp, cache_path)
    return list(seen.values())


def fetch_comments(sess: requests.Session, post: dict) -> list:
    """Top-level comments for one post, from cache when present."""
    os.makedirs(CACHE_DIR, exist_ok=True)
    path = os.path.join(CACHE_DIR, f"{post['id']}.json")
    if os.path.exists(path):
        try:
            with open(path) as fh:
                return json.load(fh)
        except Exception:
            pass                      # a truncated cache file is a miss, not a crash
    blob = None
    for attempt in range(len(_RATE_LIMIT_BACKOFFS) + 1):
        r = sess.get(f"{BASE}{post['permalink']}.json",
                     params={"limit": "50", "sort": "top", "raw_json": "1"}, timeout=TIMEOUT)
        if r.status_code != 429:
            r.raise_for_status()
            blob = r.json()
            break
        if attempt >= len(_RATE_LIMIT_BACKOFFS):
            raise RateLimited(f"429 after {attempt} backoffs")
        wait = _RATE_LIMIT_BACKOFFS[attempt]
        try:                            # honour the server's own number when it gives one
            wait = max(wait, int(float(r.headers.get("retry-after", 0))))
        except (TypeError, ValueError):
            pass
        print(f"    429 - waiting {wait}s", file=sys.stderr, flush=True)
        time.sleep(wait)
    if blob is None:
        raise RateLimited("no response after backoffs")
    out = []
    if isinstance(blob, list) and len(blob) > 1:
        for k in (blob[1].get("data") or {}).get("children") or []:
            if k.get("kind") != "t1":
                continue
            c = k.get("data") or {}
            if c.get("body"):
                out.append({"score": c.get("score", 0), "author": c.get("author", ""),
                            "body": c["body"]})
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(out, fh)
    os.replace(tmp, path)
    return out


def _block(post: dict, comments: list) -> str:
    """One Q&A thread as a text block, or "" if no answer survives the filters."""
    answers = [c for c in comments
               if c["score"] >= MIN_ANSWER_SCORE
               and len(c["body"].strip()) >= MIN_ANSWER_CHARS
               and c["body"].strip() not in ("[deleted]", "[removed]")
               and (c.get("author") or "").lower() not in ("automoderator",)]
    answers.sort(key=lambda c: -c["score"])
    answers = answers[:MAX_ANSWERS]
    if not answers:
        return ""
    when = datetime.fromtimestamp(post.get("created_utc") or 0, timezone.utc).strftime("%Y-%m")
    title = _clean(post.get("title"))
    lines = [f"=== {title} (r/OrnaRPG {when}, {post.get('score', 0)}up, "
             f"{BASE}{post.get('permalink', '')}) ==="]
    body = _clean(post.get("selftext"))
    if body:
        lines.append(f"Q: {body}")
    for c in answers:
        dev = " DEV" if (c.get("author") or "").lower() in DEV_AUTHORS else ""
        lines.append(f"A [{c['score']}up{dev}]: {_clean(c['body'])}")
    return "\n".join(lines) + "\n"


def build_text(posts: list, comments_by_id: dict, heldout_ids=frozenset()) -> tuple:
    """-> (corpus_text, heldout_text). Two files so the evaluation threads are
    crawled but not searchable by the bot."""
    main, held = [], []
    for p in posts:
        blk = _block(p, comments_by_id.get(p["id"]) or [])
        if not blk:
            continue
        (held if p["id"] in heldout_ids else main).append(blk)
    return "\n".join(main), "\n".join(held)


def _load_eval_ids() -> frozenset:
    """The held-out evaluation post ids, from the environment so the list is not
    a second hardcoded copy to drift."""
    raw = os.environ.get("QA_HELDOUT_IDS", "")
    return frozenset(x.strip() for x in raw.split(",") if x.strip())


def _demo() -> None:
    """Parser checks - no network, so they pin the formatting rules."""
    post = {"id": "abc", "title": "How to get multiple followers?",
            "selftext": "New to orna, just got T8 and I see people with  multiple followers",
            "score": 24, "permalink": "/r/OrnaRPG/comments/abc/x/", "created_utc": 1770000000}
    comments = [{"score": 22, "author": "lunar", "body": "You're fighting a summoner."},
                {"score": 1, "author": "low", "body": "This score is below the floor entirely"},
                {"score": 6, "author": "OrnaOdie", "body": "Dev confirming the summon behaviour here."},
                {"score": 3, "author": "x", "body": "[deleted]"}]
    out = _block(post, comments)
    assert out.startswith("=== How to get multiple followers? (r/OrnaRPG 2026-"), out[:70]
    assert "24up" in out and "/r/OrnaRPG/comments/abc/x/" in out
    assert "Q: New to orna, just got T8" in out
    assert "multiple followers" in out and "  " not in out.split("Q: ")[1][:60], "runs of spaces collapse"
    assert "A [22up]: You're fighting a summoner." in out
    assert "A [6up DEV]: Dev confirming" in out, "a dev answer must be flagged"
    assert "below the floor" not in out, "a 1-up answer is dropped"
    assert "[deleted]" not in out
    # answers are ordered by score, best first
    assert out.index("22up") < out.index("6up")
    # a thread with no surviving answer produces nothing rather than a stub
    assert _block(post, [{"score": 1, "author": "a", "body": "nah"}]) == ""
    assert _block(post, []) == ""
    main, held = build_text([post], {"abc": comments}, heldout_ids={"abc"})
    assert main == "" and "multiple followers" in held, "a held-out post must not reach the corpus"
    print("orna_scrape_qa: all parser checks passed")


if __name__ == "__main__":
    if "--check" in sys.argv:
        _demo()
        sys.exit(0)
    _demo()
    held_ids = _load_eval_ids()
    sess = _session()
    print("listing posts...", file=sys.stderr)
    posts = list_posts(sess)
    print(f"{len(posts)} unique QUESTION posts", file=sys.stderr)
    worth = [p for p in posts if (p.get("num_comments") or 0) >= 2]
    print(f"{len(worth)} with >=2 comments - fetching (cached runs are instant)", file=sys.stderr)
    by_id, failed = {}, 0
    for i, p in enumerate(worth, 1):
        cached = os.path.exists(os.path.join(CACHE_DIR, f"{p['id']}.json"))
        try:
            by_id[p["id"]] = fetch_comments(sess, p)
        except RateLimited as exc:
            # Stop rather than grind: the cache makes a later run resume from
            # here, and continuing would just collect hundreds more 429s.
            print(f"  [{i}/{len(worth)}] RATE LIMITED ({exc}) - stopping, re-run later to resume",
                  file=sys.stderr)
            break
        except Exception as exc:
            failed += 1
            print(f"  [{i}/{len(worth)}] FAIL {p['id']}: {exc!r}", file=sys.stderr)
            continue
        if i % 25 == 0 or i == len(worth):
            print(f"  [{i}/{len(worth)}] ok ({failed} failed)", file=sys.stderr)
        if not cached:
            time.sleep(DELAY_SECONDS)
    # Build from the whole CACHE, not just what this run fetched - a resumed
    # crawl must still write the complete corpus.
    for pst in worth:
        if pst["id"] not in by_id:
            cached = os.path.join(CACHE_DIR, f"{pst['id']}.json")
            if os.path.exists(cached):
                try:
                    with open(cached) as fh:
                        by_id[pst["id"]] = json.load(fh)
                except Exception:
                    pass
    main, held = build_text(worth, by_id, held_ids)
    if not main.strip():
        raise RuntimeError("no Q&A blocks built - refusing to write an empty corpus")
    with open(OUT_PATH, "w", encoding="utf-8") as fh:
        fh.write(main)
    with open(HELDOUT_PATH, "w", encoding="utf-8") as fh:
        fh.write(held)
    print(f"wrote {OUT_PATH}: {main.count('=== ')} threads, {len(main):,} chars", file=sys.stderr)
    print(f"wrote {HELDOUT_PATH}: {held.count('=== ')} held-out threads", file=sys.stderr)
