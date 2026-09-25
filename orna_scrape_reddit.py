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

There is no ANONYMOUS path any more. Verified 2026-09-24:
`/user/<name>/submitted.json` returns 403 for any User-Agent,
`old.reddit.com` 302s to a login page, and `api.reddit.com` 403s too. Two
ways to get the bytes, and the parser doesn't care which:

1. OAuth, as above - read-only "application-only"
   (`grant_type=client_credentials`) from a **script** app at
   https://www.reddit.com/prefs/apps. No password, no account link. Note
   registering one is gated behind Reddit's API terms sign-up for some
   accounts, which is why (2) exists.
2. `--from-dir <dir>` - build from listing JSON already saved from a
   LOGGED-IN browser, no credentials and no network. Visit
   `https://www.reddit.com/user/<name>/<comments|submitted>.json?limit=100&raw_json=1`
   while signed in, save the response, repeat with `&after=<the "after"
   value from the last page>`, and name the files so each contains the
   username and the listing kind (`OrnaOdie-comments-1.json`). See
   _items_from_dir.

Listing depth: the widely-repeated ~1000-item cap did not apply here -
measured 2026-09-24, both comment listings were still paging past 1200.
`MAX_PAGES` is a runaway guard, not a target; paging stops when Reddit
stops returning an `after` cursor. `_MIN_BODY_CHARS` then drops the
one-liners ("Fixed!", "thanks") that make up much of any dev's history.
"""
from __future__ import annotations

import html
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Optional

import httpx

OUTPUT_PATH = Path(__file__).with_name("orna_reddit.txt")
TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
API_BASE = "https://oauth.reddit.com"
USER_AGENT = "macos:orna-telegram-bot:1.0 (knowledge-base builder)"
HTTP_TIMEOUT = 30.0
PAGE_LIMIT = 100          # Reddit's per-request maximum
# No, Reddit's oft-cited ~1000-item listing cap does NOT apply to these user
# listings - measured 2026-09-24: both comment listings were still returning
# a fresh `after` cursor at 1200 items. Set high enough to reach the end of a
# decade of developer comments; the loop stops on its own when `after` is
# null, so this is a runaway guard rather than a target.
MAX_PAGES = 60
SLEEP_BETWEEN = 1.0       # be polite; the OAuth limit is 100 req/min

# (username, listing) - listing is "submitted" or "comments".
_AUTHORS = [
    ("OrnaOdie", "submitted"),
    ("OrnaOdie", "comments"),
    ("Widogeist", "comments"),
]

# Below this, a comment is almost always "Fixed!", "Thanks for the report" or
# a one-word answer - noise that bloats the corpus and dilutes a fuzzy search
# over it. Tuned DOWN from 120 after a fixture run showed 120 discarding a
# real one: "Ward absorbs magic damage before HP does, and it does not
# regenerate outside of town. That is intentional." is 105 characters and is
# exactly the kind of hidden-mechanic statement this corpus exists for.
# Losing signal costs more than keeping some noise here, because search ranks
# by word overlap and an acknowledgement will never outrank an explanation.
_MIN_BODY_CHARS = 80

# These devs also post outside the game's subs (r/buildinpublic, r/SipsTea,
# ...). Measured on the real crawl that is only ~1% of entries, but it is
# pure noise in a game knowledge base, so drop it. "aethric" is kept
# deliberately: Hero of Aethric is the same studio's other game and the
# mechanics discussions cross over constantly.
_SUBREDDIT_RE = re.compile(r"orna|aethric", re.IGNORECASE)


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
        where = item.get("subreddit") or "?"
        if not _SUBREDDIT_RE.search(where):
            continue
        when = time.strftime("%Y-%m-%d", time.gmtime(item.get("created_utc") or 0))
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


def _items_from_dir(directory: Path, user: str, kind: str) -> list:
    """Items for one user+listing out of locally saved Reddit JSON pages.

    Reddit's API access keeps moving (app registration is gated behind terms
    sign-up for some accounts), so HOW the JSON is obtained is deliberately
    not this module's problem: OAuth, a logged-in browser saving
    `/user/<name>/<kind>.json?limit=100&after=...`, or anything else all
    produce the same payload. Files are matched by name containing
    "<user>" and "<kind>" (case-insensitive), so `OrnaOdie-comments-1.json`
    and `ornaodie_comments_page2.json` both work, and are read in sorted
    order so `after` paging stays chronological.

    Accepts either a full listing response ({"data": {"children": [...]}})
    or a bare list of items, since hand-saved pages arrive in both shapes."""
    items = []
    for path in sorted(directory.glob("*.json")):
        name = path.name.lower()
        if user.lower() not in name or kind.lower() not in name:
            continue
        try:
            blob = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError) as e:
            print(f"  skipping {path.name}: {e}")
            continue
        if isinstance(blob, dict):
            children = (blob.get("data") or {}).get("children") or []
            items.extend(c.get("data") or {} for c in children if isinstance(c, dict))
        elif isinstance(blob, list):
            items.extend(x.get("data", x) if isinstance(x, dict) else {} for x in blob)
    return items


def build_text(client_id: str = "", client_secret: str = "", progress=None,
               from_dir: Optional[Path] = None) -> str:
    token = _token(client_id, client_secret) if from_dir is None else ""
    sections, seen = [], set()
    for user, kind in _AUTHORS:
        items = _items_from_dir(from_dir, user, kind) if from_dir is not None else _listing(token, user, kind)
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
    # --from-dir <path>: build from JSON pages already saved locally, no
    # credentials and no network. See _items_from_dir.
    if "--from-dir" in sys.argv:
        directory = Path(sys.argv[sys.argv.index("--from-dir") + 1]).expanduser()
        if not directory.is_dir():
            sys.exit(f"{directory} is not a directory")
        text = build_text(progress=print, from_dir=directory)
        OUTPUT_PATH.write_text(text, encoding="utf-8")
        print(f"Wrote {OUTPUT_PATH} ({OUTPUT_PATH.stat().st_size} bytes)")
        return

    client_id = os.environ.get("REDDIT_CLIENT_ID")
    client_secret = os.environ.get("REDDIT_CLIENT_SECRET")
    if not client_id or not client_secret:
        sys.exit(
            "REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET are required - Reddit blocks anonymous\n"
            "listing reads (verified: 403 on www and api, login redirect on old.reddit).\n"
            "Create a 'script' app at https://www.reddit.com/prefs/apps and re-run with:\n"
            "  REDDIT_CLIENT_ID=... REDDIT_CLIENT_SECRET=... python3 orna_scrape_reddit.py\n"
            "\nOr, if app registration is gated for your account, save the listing JSON from a\n"
            "logged-in browser and build from that instead - no credentials needed:\n"
            "  python3 orna_scrape_reddit.py --from-dir ~/reddit_json"
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
