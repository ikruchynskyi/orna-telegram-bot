"""
orna_scrape_reddit.py
=====================
One-off scraper that builds `orna_reddit.txt` from what Orna's own
developers have written on Reddit - the hidden mechanics, formulas,
"here's why that actually happens" explanations and bug-fix notes that
exist nowhere in the codex, the community sheets, or the patch notes.

Sources (`_AUTHORS`): u/OrnaOdie's submissions and comments, and
u/Widogeist's comments.

Why a SEPARATE static file rather than another tab in
orna_knowledge.txt: that corpus is refreshed weekly from Google Sheets
(see orna_knowledge._corpus_text) because the sheets are live documents.
Reddit history is append-only and years old - re-crawling it weekly would
spend a rate-limited API budget re-fetching thousands of unchanged
comments to learn nothing. So this is committed to the repo and re-run by
hand, the same pattern as orna_scrape_material_names.py. Run:

    REDDIT_CLIENT_ID=... REDDIT_CLIENT_SECRET=... python3 orna_scrape_reddit.py

CREDENTIALS ARE REQUIRED - there is no anonymous path any more. Verified
2026-09-24: `/user/<name>/submitted.json` returns 403 for any User-Agent,
`old.reddit.com` 302s to a login page, and `api.reddit.com` 403s too.
Reddit's read-only "application-only" OAuth is enough (no password, no
account link): create a **script** app at
https://www.reddit.com/prefs/apps, then pass its id/secret above. The
token this requests is client-credentials only, so it can read public
listings and nothing else.

Listing limits worth knowing: Reddit caps any listing at ~1000 items, so
a very prolific account's oldest history simply isn't reachable this way.
That is fine here - the goal is the substantive explanations, and
`_MIN_BODY_CHARS` drops the one-liners ("Fixed!", "thanks") that make up
most of the tail anyway.
"""
from __future__ import annotations

import html
import json
import os
import sys
import time
from pathlib import Path

import httpx

OUTPUT_PATH = Path(__file__).with_name("orna_reddit.txt")
TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
API_BASE = "https://oauth.reddit.com"
USER_AGENT = "macos:orna-telegram-bot:1.0 (knowledge-base builder)"
HTTP_TIMEOUT = 30.0
PAGE_LIMIT = 100          # Reddit's per-request maximum
MAX_PAGES = 12            # ~1200 items, past Reddit's own ~1000 listing cap
SLEEP_BETWEEN = 1.0       # be polite; the OAuth limit is 100 req/min

# (username, listing) - listing is "submitted" or "comments".
_AUTHORS = [
    ("OrnaOdie", "submitted"),
    ("OrnaOdie", "comments"),
    ("Widogeist", "comments"),
]

# Below this, a comment is almost always "Fixed!", "Thanks for the report",
# or a one-word answer - noise that would bloat the corpus and dilute a
# fuzzy search over it. The substantive explanations are much longer.
_MIN_BODY_CHARS = 120


def _token(client_id: str, client_secret: str) -> str:
    resp = httpx.post(
        TOKEN_URL,
        data={"grant_type": "client_credentials"},
        auth=(client_id, client_secret),
        headers={"User-Agent": USER_AGENT},
        timeout=HTTP_TIMEOUT,
    )
    resp.raise_for_status()
    token = resp.json().get("access_token")
    if not token:
        raise RuntimeError(f"no access_token in reddit response: {resp.text[:200]}")
    return token


def _listing(token: str, user: str, kind: str) -> list:
    """Every item Reddit will hand back for one user+listing, newest first."""
    out, after = [], None
    for _page in range(MAX_PAGES):
        params = {"limit": PAGE_LIMIT, "raw_json": 1}
        if after:
            params["after"] = after
        resp = httpx.get(
            f"{API_BASE}/user/{user}/{kind}",
            params=params,
            headers={"User-Agent": USER_AGENT, "Authorization": f"Bearer {token}"},
            timeout=HTTP_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json().get("data") or {}
        children = data.get("children") or []
        out.extend(c.get("data") or {} for c in children)
        after = data.get("after")
        if not after or not children:
            break
        time.sleep(SLEEP_BETWEEN)
    return out


def _entry_text(item: dict) -> str:
    """A submission's title+selftext, or a comment's body - HTML-unescaped
    and whitespace-normalised, or "" if there's nothing substantive."""
    title = (item.get("title") or "").strip()
    body = (item.get("selftext") or item.get("body") or "").strip()
    if body in ("[deleted]", "[removed]"):
        body = ""
    text = f"{title}\n{body}".strip() if title else body
    text = html.unescape(text)
    # Collapse the blank-line runs reddit markdown is full of, but KEEP single
    # newlines - Odie's explanations are often numbered lists where the line
    # breaks carry the structure.
    lines = [l.rstrip() for l in text.split("\n")]
    cleaned, blank = [], False
    for line in lines:
        if line:
            cleaned.append(line)
            blank = False
        elif not blank:
            cleaned.append("")
            blank = True
    return "\n".join(cleaned).strip()


def format_entries(user: str, kind: str, items: list) -> list:
    """Items as the flat records orna_reddit.txt stores, newest first.

    Kept deliberately close to orna_knowledge.txt's shape - a header line
    the reader can show for context, then the text - because the same
    fuzzy search reads both."""
    out = []
    for item in items:
        text = _entry_text(item)
        if len(text) < _MIN_BODY_CHARS:
            continue
        when = time.strftime("%Y-%m-%d", time.gmtime(item.get("created_utc") or 0))
        where = item.get("subreddit") or "?"
        # link_title is the post a comment sits under - real context for a
        # bare reply like "it's capped at 30%".
        context = (item.get("link_title") or item.get("title") or "").strip()
        permalink = item.get("permalink") or ""
        url = f"https://www.reddit.com{permalink}" if permalink.startswith("/") else permalink
        head = f"[{when}] u/{user} in r/{where}"
        if context:
            head += f" - re: {context}"
        out.append(f"{head}\n{url}\n{text}")
    return out


def build_text(client_id: str, client_secret: str, progress=None) -> str:
    token = _token(client_id, client_secret)
    sections, seen = [], set()
    for user, kind in _AUTHORS:
        items = _listing(token, user, kind)
        entries = []
        for entry in format_entries(user, kind, items):
            key = entry.split("\n", 2)[-1][:200]   # dedupe on the text itself
            if key in seen:
                continue
            seen.add(key)
            entries.append(entry)
        sections.append(f"=== u/{user} {kind} (reddit, developer commentary) ===\n" + "\n\n".join(entries))
        if progress:
            progress(f"u/{user} {kind}: {len(items)} fetched, {len(entries)} kept")
        time.sleep(SLEEP_BETWEEN)

    header = (
        "# orna_reddit.txt - what Orna's own developers (u/OrnaOdie, u/Widogeist) have\n"
        "# written on Reddit: hidden mechanics, formulas, why-it-works explanations and\n"
        "# fix notes that are in no codex page, community sheet or patch note. Generated\n"
        "# by orna_scrape_reddit.py; committed to the repo and re-run by hand, unlike the\n"
        "# weekly-refreshed sheets, because reddit history is append-only and years old.\n"
        "# These are DEVELOPER statements - more authoritative than the community sheets,\n"
        "# but some are years old and a later patch may have changed the numbers.\n"
    )
    return header + "\n\n".join(sections) + "\n"


def main() -> None:
    client_id = os.environ.get("REDDIT_CLIENT_ID")
    client_secret = os.environ.get("REDDIT_CLIENT_SECRET")
    if not client_id or not client_secret:
        sys.exit(
            "REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET are required - Reddit blocks anonymous\n"
            "listing reads (verified: 403 on www and api, login redirect on old.reddit).\n"
            "Create a 'script' app at https://www.reddit.com/prefs/apps and re-run with:\n"
            "  REDDIT_CLIENT_ID=... REDDIT_CLIENT_SECRET=... python3 orna_scrape_reddit.py"
        )
    text = build_text(client_id, client_secret, progress=print)
    OUTPUT_PATH.write_text(text, encoding="utf-8")
    print(f"Wrote {OUTPUT_PATH} ({OUTPUT_PATH.stat().st_size} bytes)")


def _demo() -> None:
    """Formatting checks against realistic API payloads - no network, so this
    still guards the parser when credentials aren't to hand. Run
    `python3 orna_scrape_reddit.py --demo`."""
    long_body = ("Orn bonus from gear is multiplicative with the party bonus, not additive. "
                 "So two 10% pieces give 1.1 * 1.1 = 1.21, not 1.2. This trips people up constantly.")
    items = [
        {"body": long_body, "created_utc": 1700000000, "subreddit": "OrnaRPG",
         "link_title": "How does orn bonus stack?", "permalink": "/r/OrnaRPG/comments/abc/x/"},
        {"body": "Fixed!", "created_utc": 1700000000, "subreddit": "OrnaRPG", "permalink": "/r/x/"},
        {"body": "[deleted]", "created_utc": 1700000000, "subreddit": "OrnaRPG", "permalink": "/r/y/"},
    ]
    out = format_entries("OrnaOdie", "comments", items)
    assert len(out) == 1, out                      # short and deleted both dropped
    assert out[0].startswith("[2023-11-14] u/OrnaOdie in r/OrnaRPG - re: How does orn bonus stack?"), out[0]
    assert "https://www.reddit.com/r/OrnaRPG/comments/abc/x/" in out[0]
    assert "multiplicative" in out[0]

    # a submission uses title + selftext, and HTML entities are decoded
    sub = [{"title": "Patch notes &amp; clarifications", "selftext": long_body,
            "created_utc": 1700000000, "subreddit": "OrnaRPG", "permalink": "/r/z/"}]
    got = format_entries("OrnaOdie", "submitted", sub)[0]
    assert "Patch notes & clarifications" in got, got

    # blank-line runs collapse but single newlines survive (numbered lists)
    multi = {"body": "Step one\n\n\n\nStep two\nStep three" + "x" * 120,
             "created_utc": 1700000000, "subreddit": "OrnaRPG", "permalink": "/r/w/"}
    text = format_entries("OrnaOdie", "comments", [multi])[0]
    assert "Step one\n\nStep two\nStep three" in text, repr(text[-120:])
    print("orna_scrape_reddit: all checks passed")


if __name__ == "__main__":
    if "--demo" in sys.argv:
        _demo()
    else:
        main()
