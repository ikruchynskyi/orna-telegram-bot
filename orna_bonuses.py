"""
orna_bonuses.py
===============
Amities and Crucibles from aussiescodex.com, cached to disk for a week -
the same shape as orna_releases.py.

Why these needed their own source: they are in NOTHING we already had.
Checked before writing a line of this (2026-09-24) - aussiescodex's own
`codex.json`, which orna_aussies.py already downloads, has nine categories
(items, monsters, bosses, raids, followers, classes, spells, buildings,
dungeons) and amities and crucibles are not among them. The community
sheets mention "amity" four times and "crucible" once, in passing. The
reddit dev corpus talks about them constantly but as prose, never as the
actual numbers. So "what range does the Defending amity roll?" or "which
slots can take a Crit Chance crucible?" had no answer anywhere.

Two pages, one module, because they are the same kind of thing (a bonus
you roll onto gear) and a question rarely knows which one it wants:

* /orna-amities - 184 blocks, one per bonus per tier, each with its
  adjective names, tier, roll range and description. Some are genuinely
  rangeless (Arch-Alchemy, The Hybrid are boolean effects), which is why
  parsing does not insist on a range.
* /orna-crucibles - one 46-row table, "Bonus | Crucible | Min | Max" plus
  a can/cannot column per equipment slot. The Bonus cell uses a rowspan,
  so it appears only on a group's first row and has to be carried forward.

Flattened to " | "-joined text rather than typed records, for the reason
CLAUDE.md gives for orna_knowledge.txt: this is scraped community data
with irregular shape, and a fuzzy search the model reads and interprets
beats bespoke parsing per field. That also means a page tweak degrades
into slightly messier text instead of a crash.
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

AMITIES_URL = "https://www.aussiescodex.com/orna-amities"
CRUCIBLES_URL = "https://www.aussiescodex.com/orna-crucibles"
CACHE_DIR = Path(__file__).parent / ".bonuses_cache"
CACHE_FILE = "bonuses.json"
CACHE_TTL_SECONDS = 7 * 24 * 3600
HTTP_TIMEOUT = 30.0
HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                         "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36"}

_cache: Optional[dict] = None

_RANGE_RE = re.compile(r"^\d+(?:[.,]\d+)?[–\-]\d+(?:[.,]\d+)?%?$")
# Interactive widgets on the amity page ("Check my roll" etc). They carry no
# information and would otherwise land in the middle of every entry.
_AMITY_NOISE = {"Check my roll", "Roll", "known range", "· previously rolled",
                "+ Bonus", "max", "%", "T", "Choice"}


def _block_strings(heading) -> list:
    """The bare text nodes between one heading and the next - the amity page
    has no per-entry container to select, so the entry is "everything up to
    the following h3"."""
    out = []
    for el in heading.next_elements:
        if getattr(el, "name", None) == "h3" and el is not heading:
            break
        if getattr(el, "name", None) is None:
            text = str(el).strip()
            if text:
                out.append(text)
    return out


def parse_amities(html: str) -> list:
    """One flattened line per amity block: name, tier, roll range, the
    adjective names it can appear under, and the description."""
    soup = BeautifulSoup(html, "html.parser")
    rows, seen = [], set()
    for heading in soup.find_all("h3"):
        name = heading.get_text(" ", strip=True)
        if not name:
            continue
        block = _block_strings(heading)
        tier = ""
        for i, token in enumerate(block):
            if token == "T" and i + 1 < len(block) and block[i + 1].isdigit():
                tier = block[i + 1]
                break
        rng = next((t for t in block if _RANGE_RE.match(t)), "")
        cut = block.index("Check my roll") if "Check my roll" in block else len(block)
        # The description is the one long sentence in the block; everything
        # else is a label, a number or widget chrome.
        desc = max((t for t in block[:cut] if len(t) > 25 and t not in _AMITY_NOISE),
                   key=len, default="")
        adjectives = ""
        if len(block) > 1:
            candidate = block[1]
            if candidate != name and candidate not in _AMITY_NOISE and not _RANGE_RE.match(candidate):
                adjectives = candidate
        parts = [name]
        if tier:
            parts.append(f"tier {tier}")
        if rng:
            parts.append(f"range {rng}")
        if adjectives:
            parts.append(f"names: {adjectives}")
        if desc:
            parts.append(desc)
        line = " | ".join(parts)
        if line not in seen:
            seen.add(line)
            rows.append(line)
    return rows


def parse_crucibles(html: str) -> list:
    """One flattened line per crucible table row. The Bonus cell is a
    rowspan, present only on a group's first row, so it is carried forward -
    without that, every row after the first in each group loses the name of
    the bonus it is actually about."""
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table")
    if table is None:
        return []
    rows, headers, bonus = [], [], ""
    for tr in table.find_all("tr"):
        cells = [c.get_text(" ", strip=True) for c in tr.find_all(["th", "td"])]
        if not cells:
            continue
        if tr.find("th") and not headers:
            headers = cells
            continue
        if len(cells) == len(headers):
            bonus = cells[0]
            values = cells[1:]
        else:                       # rowspan: this row inherits the last bonus
            values = cells
        labels = headers[1:] if headers else []
        parts = [bonus] if bonus else []
        for i, value in enumerate(values):
            label = labels[i] if i < len(labels) else ""
            # "— Unavailable on Weapon" / "Can apply to Weapon" already name
            # the slot, so prefixing the column label would just stutter.
            parts.append(value if label and label.lower() in value.lower() else
                         (f"{label}: {value}" if label else value))
        rows.append(" | ".join(p for p in parts if p))
    return rows


def _fetch() -> dict:
    path = CACHE_DIR / CACHE_FILE
    if path.exists() and time.time() - path.stat().st_mtime < CACHE_TTL_SECONDS:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            logger.warning("bonuses: cache unreadable, refetching")

    data = {}
    for key, url, parser in (("amities", AMITIES_URL, parse_amities),
                             ("crucibles", CRUCIBLES_URL, parse_crucibles)):
        resp = httpx.get(url, headers=HEADERS, timeout=HTTP_TIMEOUT)
        resp.raise_for_status()
        data[key] = parser(resp.text)
    if not data.get("amities") or not data.get("crucibles"):
        # Never cache a half-empty scrape: aussiescodex is a JS app whose
        # server-rendered markup could change, and pinning "there are no
        # crucibles" for a week would be worse than retrying next call.
        raise ValueError(f"empty parse (amities={len(data.get('amities') or [])}, "
                         f"crucibles={len(data.get('crucibles') or [])}) - markup may have changed")
    CACHE_DIR.mkdir(exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)
    return data


def all_bonuses() -> dict:
    """{"amities": [...], "crucibles": [...]}. Blocking on a cache miss -
    call it via asyncio.to_thread from a handler."""
    global _cache
    if _cache is None:
        _cache = _fetch()
    return _cache


def refresh_cache() -> None:
    """Force a re-scrape next time the data is needed."""
    global _cache
    _cache = None
    (CACHE_DIR / CACHE_FILE).unlink(missing_ok=True)


def refetch_now() -> dict:
    """Re-scrape right now, ignoring the TTL; counts for a confirmation
    reply (see /update_codex)."""
    refresh_cache()
    data = all_bonuses()
    return {"amities": len(data["amities"]), "crucibles": len(data["crucibles"])}


def search(query: str, limit: int = 12) -> str:
    """Matching lines, grouped under their section header, or "" if nothing
    matches. Exact substring first, then a word-overlap fallback - the same
    two-stage shape orna_knowledge.search uses, and for the same reason: a
    question says "crit crucible on legs" while the row says "Crit Chance |
    Regular Anguish 2.0 | ... | Can apply to Legs"."""
    q = query.strip().lower()
    if not q:
        return ""
    data = all_bonuses()
    words = {w for w in re.findall(r"[^\W_]+", q) if len(w) > 2}
    out = []
    for section in ("amities", "crucibles"):
        lines = data.get(section) or []
        hits = [l for l in lines if q in l.lower()]
        if not hits and words:
            scored = []
            for line in lines:
                low = line.lower()
                score = sum(1 for w in words if w in low)
                if score >= max(2, len(words) // 2):
                    scored.append((score, line))
            scored.sort(key=lambda t: -t[0])
            hits = [l for _s, l in scored]
        if hits:
            out.append(f"[{section} - aussiescodex]\n" + "\n".join(hits[:limit]))
    return "\n\n".join(out)


def _demo() -> None:
    """Parses captured fragments of the real markup - no network - so an
    upstream change fails loudly here. Run `python3 orna_bonuses.py`."""
    # Shaped like the real page: every value is its own element, and the tier
    # arrives as "T" + a comment + the digit (a React text-node split), which
    # is exactly why _block_strings walks text nodes instead of splitting a
    # blob on whitespace.
    amity_html = """
    <div><span>+ Bonus</span><h3>% Ignore Ward</h3>
      <span>Acute · Perspicacious</span><span>T<!-- -->1</span>
      <span>1\u20132%</span>
      <p>There is a % chance that your skills or spells will ignore opponent Ward</p>
      <button>Check my roll</button><span>Roll</span><span>known range</span></div>
    <div><h3>Arch-Alchemy</h3><span>Alchemist</span><span>T<!-- -->2</span>
      <p>You may find random materials while defeating monsters in the world</p>
      <button>Check my roll</button></div>
    <div><h3>Next</h3></div>
    """
    rows = parse_amities(amity_html)
    assert any("% Ignore Ward" in r and "tier 1" in r and "range 1–2%" in r for r in rows), rows
    assert any("ignore opponent Ward" in r for r in rows), rows
    # a rangeless (boolean) amity must still be kept, not dropped as a failure
    arch = [r for r in rows if r.startswith("Arch-Alchemy")]
    assert arch and "random materials" in arch[0] and "range" not in arch[0], arch

    crucible_html = """
    <table>
      <tr><th>Bonus</th><th>Crucible</th><th>Min</th><th>Max</th><th>Head</th><th>Legs</th></tr>
      <tr><td>Accuracy</td><td>Regular Anguish 2.0</td><td>1 %</td><td>2 %</td>
          <td>— Unavailable on Head</td><td>Can apply to Legs</td></tr>
      <tr><td>Darkrift Riftfall event</td><td>4 %</td><td>7 %</td>
          <td>Can apply to Head</td><td>Can apply to Legs</td></tr>
    </table>
    """
    crows = parse_crucibles(crucible_html)
    assert len(crows) == 2, crows
    assert crows[0].startswith("Accuracy |") and "Can apply to Legs" in crows[0], crows[0]
    # the rowspan row must INHERIT the bonus name rather than lose it
    assert crows[1].startswith("Accuracy |"), crows[1]
    assert "Darkrift Riftfall event" in crows[1], crows[1]
    # column labels are added, but not stuttered onto cells that name the slot
    assert "Min: 1 %" in crows[0], crows[0]
    assert "Head: — Unavailable on Head" not in crows[0], crows[0]

    assert parse_crucibles("<html><body>no table</body></html>") == []
    print("orna_bonuses: all checks passed")


if __name__ == "__main__":
    _demo()
