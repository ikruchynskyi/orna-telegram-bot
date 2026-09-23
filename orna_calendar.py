"""
orna_calendar.py - playorna.com's live event calendar
=======================================================
Unlike every /codex/... page, https://playorna.com/calendar/ has no
`codex-bootstrap` JSON blob - verified directly (curl + grep): it's plain
server-rendered HTML, `<article class="event-card">` per event, with a
predictable structure (name in <h3>, a "YYYY-MM-DD H:MM AM/PM TZ – ..."
date range in <p class="event-dates">, description in
<p class="event-description">, a ".live-badge" span when the event is
currently running, and one <div class="event-roster"> per associated
category (Raids/Bosses/Followers/Monsters/...) whose <a href> links are
already "/codex/<category>/<id>/" - the exact same cross-link shape
codex-bootstrap sections already use, so a roster entry drops straight
into telegram_orna.py's existing _result_list_keyboard/_send_entry
browsing UI with no new rendering code.

Cached to disk like orna_aussies.py, but with a much shorter TTL - event
state (what's live, what's next) is time-sensitive in a way the slow-
changing item/monster database isn't.
"""
from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Optional

import httpx
from bs4 import BeautifulSoup

CALENDAR_URL = "https://playorna.com/calendar/"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0 Safari/537.36",
}
HTTP_TIMEOUT = 15.0

CACHE_DIR = Path(__file__).parent / ".aussies_cache"
CACHE_PATH = CACHE_DIR / "calendar.html"
CACHE_TTL_SECONDS = 6 * 3600

_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2}\s+\d{1,2}:\d{2}\s*[AP]M)\s*(\S*)\s*[–-]\s*"
                       r"(\d{4}-\d{2}-\d{2}\s+\d{1,2}:\d{2}\s*[AP]M)\s*(\S*)")


def _fetch_html(force: bool = False) -> str:
    if not force and CACHE_PATH.exists() and time.time() - CACHE_PATH.stat().st_mtime < CACHE_TTL_SECONDS:
        return CACHE_PATH.read_text()
    resp = httpx.get(CALENDAR_URL, headers=HEADERS, timeout=HTTP_TIMEOUT)
    resp.raise_for_status()
    html = resp.text
    CACHE_DIR.mkdir(exist_ok=True)
    CACHE_PATH.write_text(html)
    return html


def refresh_cache() -> None:
    """Force a re-download next time fetch_events() is called."""
    CACHE_PATH.unlink(missing_ok=True)


def _parse_dates(raw: str) -> tuple[str, str]:
    """"2026-09-11 12:00 PM EDT – 2026-09-15 12:00 PM EDT" -> (start, end),
    each "2026-09-11 12:00 PM EDT". Kept as display strings, not parsed
    into real datetimes - the trailing zone is an abbreviation (EDT/EST)
    Python's %Z can't reliably round-trip, and sorting/precision here isn't
    worth fighting that; same tradeoff telegram_remind._bundle_fire_at
    already accepts for reminder scheduling."""
    m = _DATE_RE.search(raw)
    if not m:
        return raw.strip(), ""
    start = f"{m.group(1)} {m.group(2)}".strip()
    end = f"{m.group(3)} {m.group(4)}".strip()
    return start, end


def _parse_event(article) -> dict:
    name_el = article.select_one("h3")
    dates_el = article.select_one("p.event-dates")
    desc_el = article.select_one("p.event-description")
    starts, ends = _parse_dates(dates_el.get_text(strip=True)) if dates_el else ("", "")

    roster: dict[str, list[dict]] = {}
    for block in article.select(".event-roster"):
        h4 = block.select_one("h4")
        category = h4.get_text(strip=True) if h4 else "?"
        entries = []
        for a in block.select("a[href]"):
            href = a.get("href", "")
            img = a.select_one("img")
            label = (img.get("title") or img.get("alt")) if img else None
            if href:
                entries.append({"name": label or href, "url": href})
        if entries:
            roster[category] = entries

    return {
        "name": name_el.get_text(strip=True) if name_el else "?",
        "starts": starts,
        "ends": ends,
        "live": bool(article.select_one(".live-badge")),
        "description": desc_el.get_text(strip=True) if desc_el else "",
        "roster": roster,
    }


def fetch_events(force: bool = False) -> list[dict]:
    """Every event currently listed on playorna.com/calendar/ (roughly:
    live now + the near-term upcoming window the site itself shows - there
    is no further pagination to walk). Each entry:
    {"name", "starts", "ends", "live", "description",
     "roster": {"<Category>": [{"name", "url"}, ...]}}."""
    html = _fetch_html(force=force)
    soup = BeautifulSoup(html, "html.parser")
    return [_parse_event(a) for a in soup.select("article.event-card")]
