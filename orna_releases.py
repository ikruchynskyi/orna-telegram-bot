"""
orna_releases.py
================
playorna.com/releases/ - the official patch notes, cached to disk for a
week, same shape as orna_aussies.py's codex.json cache.

Why this exists at all, given the codex and the knowledge sheets already
describe the game: those two say what IS, and say nothing about what
CHANGED. The community sheets behind orna_knowledge.txt are
hand-maintained and can lag a balance patch by weeks, so an answer built
from them can be confidently stale - "this boss resists X", "that gear
gives Y%" - with nothing in the data hinting it's out of date. The patch
notes are the one source that does hint it, e.g. "Added 5% Ward Power
bonus to each piece of the Judge Trifecta warrior gear". So the loop can
read them and caveat an answer it would otherwise state flatly.

No `codex-bootstrap` JSON here, unlike every codex page - this is plain
server-rendered HTML, same as playorna.com/calendar/ (see orna_calendar.py).
The markup is stable and simple:

    <article class="release-note" id="release-1.334.0">
      <header>
        <div class="release-meta">
          <span class="content-tag">Server update</span>
          <time datetime="2026-09-14">Sept. 14, 2026</time>
        </div>
        <h2><a href="/releases/1.334.0/">Server version 1.334</a></h2>
      </header>
      <section><h3>Changes</h3><ul><li>...</li></ul></section>
    </article>

The page carries the ~15 most recent notes with no pagination, which at
the observed cadence is roughly three months - exactly the window where
"did a patch change this?" is a live question, so there's nothing to page
through and no history to keep.

Cached in `.releases_cache/` (gitignored) with a 1-week TTL, matching
orna_aussies.CACHE_TTL_SECONDS: patch notes appear every week or two, so a
week-old copy can at worst miss the very newest note, and /update_codex
force-refreshes both when that matters.
"""
from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Optional

import httpx
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

RELEASES_URL = "https://playorna.com/releases/"
CACHE_DIR = Path(__file__).parent / ".releases_cache"
CACHE_FILE = "releases.json"
CACHE_TTL_SECONDS = 7 * 24 * 3600
HTTP_TIMEOUT = 20.0
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; orna-telegram-bot/1.0)"}

_cache: Optional[list] = None


def _parse(html: str) -> list:
    """Every release note on the page, newest first, as
    {version, title, url, date, kind, items[]}. `items` flattens each
    section's bullets into "<heading>: <bullet>" lines - the heading
    ("Changes", "Fixes") is what tells a reader whether a line is a buff or
    a bug fix, and losing it would make a bullet like "Rogue's Dagger" say
    nothing."""
    soup = BeautifulSoup(html, "html.parser")
    notes = []
    for art in soup.find_all("article", class_="release-note"):
        link = art.find("h2")
        anchor = link.find("a") if link else None
        time_tag = art.find("time")
        tag = art.find("span", class_="content-tag")
        href = (anchor.get("href") or "") if anchor else ""
        items = []
        for section in art.find_all("section"):
            heading = section.find(["h3", "h4"])
            head = heading.get_text(" ", strip=True) if heading else ""
            for li in section.find_all("li"):
                text = li.get_text(" ", strip=True)
                if text:
                    items.append(f"{head}: {text}" if head else text)
        notes.append({
            "version": (art.get("id") or "").replace("release-", ""),
            "title": (anchor.get_text(" ", strip=True) if anchor else "").strip(),
            "url": f"https://playorna.com{href}" if href.startswith("/") else href,
            "date": (time_tag.get("datetime") or "") if time_tag else "",
            "kind": tag.get_text(" ", strip=True) if tag else "",
            "items": items,
        })
    return notes


def _fetch() -> list:
    path = CACHE_DIR / CACHE_FILE
    if path.exists() and time.time() - path.stat().st_mtime < CACHE_TTL_SECONDS:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            # Same reasoning as orna_aussies._fetch_json: a file truncated by
            # a crash or a launchctl reload mid-write would otherwise raise on
            # every call until the week-long TTL expired. Treat it as a miss.
            logger.warning("releases: cache unreadable, refetching")

    resp = httpx.get(RELEASES_URL, headers=HEADERS, timeout=HTTP_TIMEOUT)
    resp.raise_for_status()
    notes = _parse(resp.text)
    if not notes:
        # Never cache an empty parse - that would pin a silent "no patch
        # notes exist" for a week if the markup ever changes under us.
        raise ValueError("no release notes parsed - playorna markup may have changed")
    CACHE_DIR.mkdir(exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(notes, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)
    return notes


def all_notes() -> list:
    """Every cached release note, newest first. Blocking on a cache miss -
    call it via asyncio.to_thread from a handler."""
    global _cache
    if _cache is None:
        _cache = _fetch()
    return _cache


def refresh_cache() -> None:
    """Force a re-download next time the notes are needed."""
    global _cache
    _cache = None
    (CACHE_DIR / CACHE_FILE).unlink(missing_ok=True)


def refetch_now() -> dict:
    """Force a fresh download now, ignoring the TTL; returns small stats for
    a confirmation reply (see /update_codex)."""
    refresh_cache()
    notes = all_notes()
    return {"notes": len(notes), "newest": notes[0]["date"] if notes else "", "latest": notes[0]["title"] if notes else ""}


def search(query: str, limit: int = 6) -> list:
    """Release notes mentioning `query`, newest first - matched against the
    title and every bullet, case-insensitively. An empty query returns the
    most recent notes, which is what "what changed lately?" wants."""
    notes = all_notes()
    q = query.strip().lower()
    if not q:
        return notes[:limit]
    words = [w for w in re.findall(r"[^\W_]+", q) if len(w) > 2]
    hits = []
    for note in notes:
        matching = [i for i in note["items"] if q in i.lower()]
        if not matching and words:
            # Fall back to any single significant word - a patch bullet names
            # an item exactly ("Judge Trifecta Falx") while the question says
            # "judge falx", so the whole phrase rarely appears verbatim.
            matching = [i for i in note["items"] if any(w in i.lower() for w in words)]
        if matching or q in note["title"].lower():
            hits.append({**note, "items": matching or note["items"]})
        if len(hits) >= limit:
            break
    return hits


def format_notes(notes: list, max_items: int = 8) -> str:
    """Notes as plain text for the model to read - see telegram_orna's
    releases tool, which hands this back as an observation rather than
    posting it, same as knowledge_search."""
    out = []
    for note in notes:
        head = f"{note['date']} - {note['title']} ({note['kind']})".strip()
        bullets = "\n".join(f"  - {i}" for i in note["items"][:max_items])
        extra = len(note["items"]) - max_items
        if extra > 0:
            bullets += f"\n  - (+{extra} more)"
        out.append(f"{head}\n{bullets}" if bullets else head)
    return "\n\n".join(out)


def _demo() -> None:
    """Parses a captured copy of the real page - no network - so a markup
    change upstream fails here loudly. Run `python3 orna_releases.py`."""
    sample = """
    <article class="release-note" id="release-1.334.0">
      <header><div class="release-meta">
        <span class="content-tag">Server update</span><time datetime="2026-09-14">Sept. 14, 2026</time>
      </div><h2><a href="/releases/1.334.0/">Server version 1.334</a></h2></header>
      <section><h3>Changes</h3><ul>
        <li>Added 5% Ward Power bonus to each piece of the Judge Trifecta warrior gear</li>
        <li>Fixed an issue with the Zeta Riftlock</li>
      </ul></section>
    </article>
    <article class="release-note" id="release-1.333.0">
      <header><div class="release-meta">
        <span class="content-tag">App update</span><time datetime="2026-09-01">Sept. 1, 2026</time>
      </div><h2><a href="/releases/1.333.0/">App update v3.26.2</a></h2></header>
      <section><h3>Fixes</h3><ul><li>Crash on startup</li></ul></section>
    </article>
    """
    notes = _parse(sample)
    assert len(notes) == 2, notes
    n = notes[0]
    assert n["version"] == "1.334.0" and n["kind"] == "Server update", n
    assert n["url"] == "https://playorna.com/releases/1.334.0/", n["url"]
    assert n["date"] == "2026-09-14"
    # the section heading must survive onto each bullet
    assert n["items"][0].startswith("Changes: Added 5% Ward Power"), n["items"]
    assert len(n["items"]) == 2

    # an empty parse must never be cached as "no notes exist"
    try:
        globals()["_parse_probe"] = _parse("<html><body>nothing</body></html>")
        assert _parse("<html><body>nothing</body></html>") == []
    finally:
        globals().pop("_parse_probe", None)

    text = format_notes(notes)
    assert "Judge Trifecta" in text and "1.334" in text, text
    print("orna_releases: all checks passed")


if __name__ == "__main__":
    _demo()
