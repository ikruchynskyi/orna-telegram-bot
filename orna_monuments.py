"""This week's Monument rewards - which monument and floor gives what.

Source: floorchart.top ("Orna Monument Rewards" by Truffles), a community
tracker whose contributors enter each week's chart by hand. The page is an
empty shell; its data comes from a public Google Apps Script endpoint that
returns one JSON document:

    {"week": 41, "ithra": grid, "thor": grid, "vulcan": grid, "demeter": grid}

Each grid is rows x columns: row 0 is the header (the monument name, then
"Reward 1", "Reward 2", "Reward 3", "Material", "Potion"), column 0 is the
FLOOR, and each cell is what that floor gives in that slot. The three Reward
slots hold a CATEGORY ("Materials", "Armor", "Proofs", ...), while the
Material and Potion slots hold the SPECIFIC thing ("Adamantine", "Silk",
"Nostrum", ...) - which is what makes "where do I get Adamantine this week"
answerable at all.

THINGS THAT ARE NOT OBVIOUS
  * Abbreviations are not uniform. The site writes "P Runestone", "P Darkstone",
    "C Ortanite"; the codex says Perfect Runestone, Pure Darkstone, Cursed
    Ortanite - so "P" means Perfect for one and Pure for the other, and a
    hardcoded "P = Pure" rule would be wrong. Each is expanded by looking it up
    in the codex database (_expand) instead.
  * The data is crowd-entered weekly. "week" is the ISO week number (week 41 is
    the week of Monday 2026-10-05), and early in a new week the site may still
    serve LAST week's chart. That is reported as stale rather than presented as
    this week's rewards - a stale answer that looks current is the failure.
  * Cached to disk with a short TTL like orna_calendar (this is someone's Apps
    Script quota - a few requests a day, not one per question), atomically, and
    an empty or malformed response is never cached: the shared convention.

Run: python3 orna_monuments.py    (self-check, no network)
"""

from __future__ import annotations

import paths
import datetime
import difflib
import json
import logging
import os
import re
import time
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

DATA_URL = ("https://script.google.com/macros/s/AKfycbyY7Tv_hkYrYCgfvKiy9mGOShuMYXIGBMfRTDX8l3Ln"
            "zKNEk7XCgTsQcOXZ_ogYh5a9gg/exec")
SITE_URL = "https://floorchart.top/"
MONUMENTS = ("ithra", "thor", "vulcan", "demeter")
CACHE_DIR = paths.CACHE / "monuments"
CACHE_PATH = CACHE_DIR / "rewards.json"
CACHE_TTL_SECONDS = 6 * 3600
HEADERS = {"User-Agent": "orna-telegram-bot/1.0 (guild helper bot; contact via github ikruchynskyi)"}

# What a player means by a word -> the site's own category names. "items" and
# "gear" mean equipment; "materials" means BOTH the generic "Materials" reward
# slots AND every specific material in the Material column, because that is
# what someone asking "where can I get materials" wants to see.
_CATEGORY_ALIASES = {
    "materials": "materials", "material": "materials", "mats": "materials", "mat": "materials",
    "rare materials": "rare materials", "rare material": "rare materials", "rare mats": "rare materials",
    "potions": "potions", "potion": "potions",
    "items": "gear", "item": "gear", "gear": "gear", "equipment": "gear",
    "armor": "armor", "armour": "armor", "weapon": "weapon", "weapons": "weapon",
    "accessory": "accessory", "accessories": "accessory",
    "proofs": "proofs", "proof": "proofs", "orns": "orns", "orn": "orns",
    "skeleton keys": "skeleton keys", "skeleton key": "skeleton keys", "keys": "skeleton keys",
    "arena tokens": "arena tokens", "arena token": "arena tokens", "tokens": "arena tokens",
    "monster remains": "monster remains", "remains": "monster remains",
    "astralseed": "astralseed", "astralseeds": "astralseed",
}
_GEAR = {"armor", "weapon", "accessory"}
# The site's own "Rare Materials" filter list (its colorRules), as named there.
_RARE_ON_SITE = {"avalon ore", "c ortanite", "hardened steel", "p runestone", "p draconite",
                 "realm ore", "solarite", "titanium", "red draconite", "p darkstone"}


class MonumentsError(RuntimeError):
    pass


def current_iso_week(today: Optional[datetime.date] = None) -> int:
    return (today or datetime.datetime.now(datetime.timezone.utc).date()).isocalendar()[1]


# ---------------------------------------------------------------------------
# Fetch + cache
# ---------------------------------------------------------------------------

def _valid(data) -> bool:
    """Shape check before anything is cached: a week number and four non-empty
    grids. An Apps Script error comes back as HTTP 200 with {"error": ...}, so
    the status code alone proves nothing."""
    if not isinstance(data, dict) or data.get("error"):
        return False
    try:
        int(data.get("week"))
    except (TypeError, ValueError):
        return False
    return all(isinstance(data.get(m), list) and len(data[m]) > 1 for m in MONUMENTS)


def _download() -> dict:
    resp = httpx.get(DATA_URL, headers=HEADERS, timeout=30, follow_redirects=True)
    resp.raise_for_status()
    data = resp.json()
    if not _valid(data):
        raise MonumentsError(f"floorchart returned no usable chart: {str(data)[:200]}")
    return data


def _read_cache() -> Optional[dict]:
    try:
        return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return None


def _write_cache(data: dict) -> None:
    CACHE_DIR.mkdir(exist_ok=True)
    tmp = CACHE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps({"fetched": time.time(), "data": data}), encoding="utf-8")
    os.replace(tmp, CACHE_PATH)


def load(force: bool = False) -> dict:
    """The current chart: from cache while fresh, otherwise refetched. A
    cached chart from a PREVIOUS ISO week is refetched even inside the TTL,
    since the whole point is this week's rewards. If the refetch fails, the
    cached copy is served rather than nothing - load() reports whether it is
    stale, the caller says so."""
    cached = _read_cache()
    fresh = (cached and not force
             and time.time() - cached.get("fetched", 0) < CACHE_TTL_SECONDS
             and int(cached["data"].get("week", -1)) == current_iso_week())
    if fresh:
        return cached["data"]
    try:
        data = _download()
        _write_cache(data)
        return data
    except Exception as e:
        if cached:
            logger.warning("monuments: refetch failed (%s) - serving the cached chart", e)
            return cached["data"]
        raise


def refetch_now() -> dict:
    """For /update_codex: force a refetch. Returns the week and a count."""
    data = load(force=True)
    return {"week": int(data["week"]), "rewards": len(parse(data))}


# ---------------------------------------------------------------------------
# Parse
# ---------------------------------------------------------------------------

_codex_names: Optional[list] = None


def _item_names() -> list:
    global _codex_names
    if _codex_names is None:
        try:
            import orna_codex_db
            _codex_names = [r[0] for r in orna_codex_db.connect().execute(
                "SELECT name FROM records WHERE category = 'items' AND name IS NOT NULL")]
        except Exception:
            logger.warning("monuments: codex names unavailable, abbreviations left as written",
                           exc_info=True)
            _codex_names = []
    return _codex_names


def _expand(name: str, names: Optional[list] = None) -> str:
    """"P Runestone" -> "Perfect Runestone", by finding the ONE codex item whose
    name ends with the base word and whose first word starts with the letter.
    Left as written when there is no unique match - an unexpanded name is
    still findable, a wrongly expanded one is not."""
    m = re.match(r"^([A-Za-z])\.?\s+(\S.*)$", name.strip())
    if not m:
        return name
    letter, base = m.group(1).lower(), m.group(2).strip().lower()
    hits = {n for n in (names if names is not None else _item_names())
            if n.lower().endswith(" " + base) and n.lower()[0] == letter
            and len(n.split()) == len(base.split()) + 1}
    return hits.pop() if len(hits) == 1 else name


def parse(data: dict, names: Optional[list] = None) -> list:
    """Flatten the four grids into one record per non-empty cell."""
    out = []
    for mon in MONUMENTS:
        grid = data.get(mon) or []
        if not grid:
            continue
        slots = [str(c).strip() for c in grid[0]]
        for row in grid[1:]:
            if not row:
                continue
            try:
                floor = int(str(row[0]).strip())
            except ValueError:
                continue
            for slot, cell in zip(slots[1:], row[1:]):
                value = str(cell).strip()
                if not value:
                    continue
                kind = ("material" if slot.lower() == "material" else
                        "potion" if slot.lower() == "potion" else "category")
                full = _expand(value, names) if kind != "category" else value
                out.append({"monument": mon.title(), "floor": floor, "slot": slot,
                            "kind": kind, "value": value, "name": full})
    return out


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

def _category_match(rec: dict, cat: str) -> bool:
    v = rec["value"].lower()
    if cat == "materials":
        return rec["kind"] == "material" or v == "materials"
    if cat == "rare materials":
        return rec["kind"] == "material" and v in _RARE_ON_SITE
    if cat == "potions":
        return rec["kind"] == "potion" or v == "potions"
    if cat == "gear":
        return rec["kind"] == "category" and v in _GEAR
    return rec["kind"] == "category" and v == cat


def search(query: str = "", monument: str = "", floor: Optional[int] = None,
           data: Optional[dict] = None, names: Optional[list] = None) -> dict:
    """-> {week, current_week, stale, matched_as, matches: [records]}.

    `query` may be a specific thing ("adamantine", "perfect runestone"), a
    category the site filters by ("materials", "proofs", "items"), or empty for
    the whole chart. A name is matched against BOTH the site's spelling and the
    expanded codex name, then fuzzily, so "runestone", "perfect runestone" and
    "p runestone" all find the same cell."""
    data = data if data is not None else load()
    records = parse(data, names)
    week, now = int(data["week"]), current_iso_week()
    if monument:
        records = [r for r in records if r["monument"].lower() == monument.strip().lower()]
    if floor is not None:
        records = [r for r in records if r["floor"] == int(floor)]

    q = (query or "").strip().lower()
    matched_as = "everything"
    if q:
        cat = _CATEGORY_ALIASES.get(q)
        if cat:
            records, matched_as = [r for r in records if _category_match(r, cat)], f"category '{cat}'"
        else:
            direct = [r for r in records if q in r["value"].lower() or q in r["name"].lower()]
            if not direct:
                pool = sorted({r["name"].lower() for r in records} | {r["value"].lower() for r in records})
                close = difflib.get_close_matches(q, pool, n=3, cutoff=0.75)
                direct = [r for r in records if r["name"].lower() in close or r["value"].lower() in close]
                matched_as = f"closest name(s) {close}" if close else f"'{q}'"
            else:
                matched_as = f"'{q}'"
            records = direct
    return {"week": week, "current_week": now, "stale": week != now,
            "matched_as": matched_as, "matches": records}


def _demo() -> None:
    names = ["Runestone", "Perfect Runestone", "Darkstone", "Pure Darkstone", "Ortanite",
             "Cursed Ortanite", "Draconite", "Red Draconite", "Pure Draconite", "Adamantine",
             "Pirate Darkstone"]
    # "P" is NOT uniform - one lookup per name, never a hardcoded rule
    assert _expand("P Runestone", names) == "Perfect Runestone"
    assert _expand("C Ortanite", names) == "Cursed Ortanite"
    # two codex items start with P and end in Darkstone -> ambiguous -> left alone
    assert _expand("P Darkstone", names) == "P Darkstone", "ambiguous must not be guessed"
    assert _expand("P Darkstone", names[:-1]) == "Pure Darkstone"
    assert _expand("Adamantine", names) == "Adamantine", "a plain name is not an abbreviation"

    data = {"week": 41,
            "ithra": [["Ithra", "Reward 1", "Reward 2", "Reward 3", "Material", "Potion"],
                      ["2", "Materials", "Accessory", "Potions", "Red Draconite", ""],
                      ["4", "Materials", "Weapon", "Orns", "C Ortanite", ""],
                      ["9", "Proofs", "Armor", "", "Adamantine", "Nostrum"]],
            "thor": [["Thor", "Reward 1 ", "Reward 2 ", "Reward 3", "Material ", "Potion "],
                     ["5", "Monster Remains", "Materials", "Skeleton Keys", "P Runestone", ""]],
            "vulcan": [["Vulcan", "Reward 1", "Reward 2", "Reward 3", "Material", "Potion"],
                       ["7", "Orns", "Proofs", "Potions", "Adamantine", "Panacea"]],
            "demeter": [["Demeter", "Reward 1", "Reward 2", "Reward 3", "Material", "Potion"],
                        ["3", "Potions", "Armor", "Proofs", "", "Nostrum"]]}
    names = names[:-1]
    assert _valid(data) and not _valid({"error": "quota"}) and not _valid({"week": 41})

    recs = parse(data, names)
    assert all(r["slot"] == r["slot"].strip() for r in recs), "header padding stripped"
    assert not any(r["value"] == "" for r in recs), "empty cells are not rewards"

    # a specific material -> every monument and floor, both of them
    r = search("adamantine", data=data, names=names)
    assert {(m["monument"], m["floor"]) for m in r["matches"]} == {("Ithra", 9), ("Vulcan", 7)}, r
    # the codex name finds the site's abbreviation, and so does the bare base word
    assert [m["value"] for m in search("perfect runestone", data=data, names=names)["matches"]] == ["P Runestone"]
    assert search("runestone", data=data, names=names)["matches"][0]["monument"] == "Thor"
    # a typo still lands
    assert search("adamantyne", data=data, names=names)["matches"], "fuzzy fallback"
    # the site's filters: "materials" = the generic slots AND the specific ones
    mats = search("materials", data=data, names=names)["matches"]
    assert any(m["kind"] == "material" for m in mats) and any(m["value"] == "Materials" for m in mats)
    gear = {m["value"] for m in search("items", data=data, names=names)["matches"]}
    assert gear == {"Accessory", "Weapon", "Armor"}, gear
    assert {m["value"] for m in search("rare materials", data=data, names=names)["matches"]} == \
        {"Red Draconite", "C Ortanite", "P Runestone"}
    pots = {m["value"] for m in search("potions", data=data, names=names)["matches"]}
    assert {"Nostrum", "Panacea", "Potions"} <= pots, pots
    # scoping, and an honest miss
    assert {m["monument"] for m in search("", monument="thor", data=data, names=names)["matches"]} == {"Thor"}
    assert all(m["floor"] == 9 for m in search("", floor=9, data=data, names=names)["matches"])
    assert search("excalibur", data=data, names=names)["matches"] == []
    # staleness is reported, never hidden
    assert search("adamantine", data=dict(data, week=current_iso_week() - 1), names=names)["stale"]
    assert current_iso_week(datetime.date(2026, 10, 5)) == 41, "the site's week 41 is ISO week 41"
    print("orna_monuments: all checks passed")


if __name__ == "__main__":
    _demo()
