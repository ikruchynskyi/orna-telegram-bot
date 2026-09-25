"""
orna_classes.py
===============
Reader and stats estimator over `orna_classes.json` (see
orna_scrape_classes.py for where that comes from and why it is committed
rather than fetched).

Two different kinds of entry live in there, and confusing them produces
numbers that look plausible and are wrong:

* **specializations** (19, tier 10): Gilgamesh, Heretic, Realmshifter,
  Beowulf, Grand Summoner, Diety and their sub-specs. These carry
  ABSOLUTE base stats - Gilgamesh is hp 12509, attack 1304.
* **classes** (40, tiers 1-10): Brawler, Duelist, Magus, ... These carry
  PERCENT `statModifiers` - Brawler is hp +5%, attack +2%.

The estimator applies, in order:
  1. a specialization's absolute base stats (or a caller-supplied base),
  2. the class's percent modifiers,
  3. Ascension Level: +1% per level, so AL 100 doubles everything. Applied
     to every stat, multiplicatively with the class modifier.
  4. PVP: doubles HP only.

Steps 3 and 4 are the game's own rules as stated by the guild, not
something derivable from aussiescodex's data - they are implemented here
and pinned in _demo so a later edit cannot silently change them.
"""
from __future__ import annotations

import difflib
import json
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

DATA_PATH = Path(__file__).with_name("orna_classes.json")
_data: Optional[dict] = None

# The stats a specialization's base table carries, in the order they are
# most useful to read.
STAT_ORDER = ("hp", "mana", "attack", "defense", "magic", "resistance",
              "dexterity", "foresight", "crit", "view_distance")


def _load() -> dict:
    """The dataset, or empty stubs if the file is missing - a checkout
    without it should degrade to "no class data", not break callers."""
    global _data
    if _data is None:
        try:
            _data = json.loads(DATA_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            logger.warning("orna_classes: %s unreadable - class data disabled", DATA_PATH.name)
            _data = {"classes": {}, "spec_stats": {}, "spec_extras": {}}
    return _data


def _resolve(name: str, pool: dict) -> Optional[str]:
    """Match a user-typed name against `pool`'s keys: exact, then
    case-insensitive, then fuzzy. The fuzzy leg earns its place on this
    data specifically - aussiescodex spells it "Diety", players type
    "deity", and "Grand Summoner" gets typed "summoner"."""
    if not name:
        return None
    # "None" is aussiescodex's all-zero UI placeholder, never a real answer -
    # and leaving it in matched anything containing that substring ("none" is
    # inside "nonexistent"), so an unknown name resolved to a class of zeros.
    pool = {k: v for k, v in pool.items() if k != "None"}
    if name in pool:
        return name
    low = {k.lower(): k for k in pool}
    key = name.strip().lower()
    if key in low:
        return low[key]
    # Only "typed name is part of the real name" ("summoner" -> "Grand
    # Summoner"). The reverse direction is deliberately NOT allowed: it lets
    # any sentence containing a short class name resolve to it.
    for k_low, k in low.items():
        if key and key in k_low:
            return k
    close = difflib.get_close_matches(key, list(low), n=1, cutoff=0.7)
    return low[close[0]] if close else None


def find_class(name: str, kind: str = "") -> Optional[dict]:
    """{"name", "kind", ...} for a class or specialization, or None.

    `kind` ("class" or "specialization") forces which pool is searched
    FIRST, and matters more than it looks. Live bug 2026-09-24: a caller
    passing class="Heretic" got the tier-10 SPECIALIZATION back, because
    specializations are searched first by default - so its (nonexistent)
    stat_modifiers were applied, silently dropping the real class's
    modifiers from the estimate. With kind="class" the same string resolves
    inside the class pool instead, or returns None rather than quietly
    handing back the wrong kind of thing."""
    data = _load()
    pools = [("specialization", "spec_stats"), ("class", "classes")]
    if kind == "class":
        pools.reverse()
    if kind in ("class", "specialization"):
        pools = [pl for pl in pools if pl[0] == kind]
    for want, pool_key in pools:
        found = _find_in(data, want, pool_key, name)
        if found:
            return found
    return None


def _find_in(data: dict, want: str, pool_key: str, name: str) -> Optional[dict]:
    key = _resolve(name, data.get(pool_key) or {})
    if not key:
        return None
    if want == "specialization":
        extras = (data.get("spec_extras") or {}).get(key) or {}
        return {"name": key, "kind": "specialization", "tier": 10,
                "base_stats": data["spec_stats"][key],
                "stat_modifiers": {},
                "bonus_stats": extras.get("bonusStats") or {},
                "passives": extras.get("passiveEffects") or []}
    entry = data["classes"][key]
    return {"name": key, "kind": "class", "tier": entry.get("tier"),
            "stat_modifiers": entry.get("statModifiers") or {},
            "bonus_stats": entry.get("bonusStats") or {},
            "passives": entry.get("passiveEffects") or []}


def estimate(name: str, ascension_level: int = 0, pvp: bool = False,
             base_stats: Optional[dict] = None) -> Optional[dict]:
    """Projected stats for a class/specialization.

    `base_stats` overrides the table - needed for a non-tier-10 CLASS,
    which only knows its percent modifiers and has no absolute numbers of
    its own. Without it, a class returns modifiers only, rather than
    inventing a base it does not have."""
    entry = find_class(name)
    if entry is None:
        return None

    al = max(0, int(ascension_level or 0))
    al_mult = 1 + al / 100.0                 # AL 100 => x2 on every stat
    base = dict(base_stats or entry.get("base_stats") or {})
    mods = entry.get("stat_modifiers") or {}

    stats = {}
    if base:
        for stat, value in base.items():
            if not isinstance(value, (int, float)):
                continue
            scaled = value * (1 + mods.get(stat, 0) / 100.0) * al_mult
            if stat == "hp" and pvp:
                scaled *= 2              # PVP doubles HP only
            stats[stat] = round(scaled, 1)

    return {
        "name": entry["name"], "kind": entry["kind"], "tier": entry.get("tier"),
        "ascension_level": al, "pvp": pvp,
        "stat_modifiers": mods, "bonus_stats": entry.get("bonus_stats") or {},
        "passives": entry.get("passives") or [], "stats": stats,
    }


def format_entry(name: str, ascension_level: int = 0, pvp: bool = False) -> str:
    """Human/model-readable summary, or "" if the name is unknown."""
    est = estimate(name, ascension_level, pvp)
    if est is None:
        return ""
    head = f"{est['name']} ({est['kind']}, tier {est['tier']})"
    if est["ascension_level"] or est["pvp"]:
        head += f" — AL {est['ascension_level']}" + (", PVP" if est["pvp"] else "")
    lines = [head]
    if est["stats"]:
        ordered = [s for s in STAT_ORDER if s in est["stats"]]
        lines.append("Stats: " + ", ".join(f"{s} {est['stats'][s]:g}" for s in ordered))
    if est["stat_modifiers"]:
        lines.append("Stat modifiers: " + ", ".join(
            f"{k} {v:+g}%" for k, v in sorted(est["stat_modifiers"].items())))
    if est["bonus_stats"]:
        lines.append("Bonus stats: " + ", ".join(
            f"{k.replace('_', ' ')} {v:+g}" for k, v in sorted(est["bonus_stats"].items())))
    if est["passives"]:
        lines.append("Passive effects: " + ", ".join(est["passives"]))
    return "\n".join(lines)


def search(query: str, limit: int = 4) -> str:
    """Entries matching `query` - by name, by a bonus stat, or by a passive
    effect - as text for the model. Empty string if nothing matches."""
    q = (query or "").strip().lower()
    if not q:
        return ""
    data = _load()
    named = find_class(q)
    hits = [named["name"]] if named else []
    if not hits:
        for pool_name in ("spec_stats", "classes"):
            for key in (data.get(pool_name) or {}):
                entry = find_class(key) or {}
                haystack = " ".join([key] + list(entry.get("bonus_stats") or {})
                                    + list(entry.get("passives") or [])).lower()
                if any(w in haystack for w in q.split() if len(w) > 2):
                    hits.append(key)
    out = []
    for name_ in list(dict.fromkeys(hits))[:limit]:   # dedupe, keep order
        text = format_entry(name_)
        if text:
            out.append(text)
    return "\n\n".join(out)


def _demo() -> None:
    """Pins the estimator's arithmetic and the two game rules that are NOT
    in the source data (AL scaling, PVP HP). Run `python3 orna_classes.py`."""
    data = _load()
    assert len(data["classes"]) == 40 and len(data["spec_stats"]) == 19, (
        len(data["classes"]), len(data["spec_stats"]))

    # a specialization carries ABSOLUTE stats
    gil = find_class("Gilgamesh")
    assert gil["kind"] == "specialization" and gil["base_stats"]["hp"] == 12509, gil

    # AL 100 doubles every stat
    at0 = estimate("Gilgamesh", ascension_level=0)
    at100 = estimate("Gilgamesh", ascension_level=100)
    assert at0["stats"]["hp"] == 12509, at0["stats"]["hp"]
    assert at100["stats"]["hp"] == 25018, at100["stats"]["hp"]
    assert at100["stats"]["attack"] == 2608, at100["stats"]["attack"]
    # ...and AL 50 is +50%, not half of the doubling applied twice
    assert estimate("Gilgamesh", ascension_level=50)["stats"]["hp"] == 18763.5

    # PVP doubles HP ONLY
    pvp = estimate("Gilgamesh", pvp=True)
    assert pvp["stats"]["hp"] == 25018, pvp["stats"]["hp"]
    assert pvp["stats"]["attack"] == at0["stats"]["attack"], "PVP must not touch attack"
    both = estimate("Gilgamesh", ascension_level=100, pvp=True)
    assert both["stats"]["hp"] == 50036, both["stats"]["hp"]

    # a CLASS carries percent modifiers and no base of its own
    brawler = find_class("Brawler")
    assert brawler["kind"] == "class" and brawler["stat_modifiers"]["hp"] == 5
    assert estimate("Brawler")["stats"] == {}, "a class must not invent a base"
    # ...but applies them to a supplied base: 1000 hp +5%, AL 100 -> 2100
    supplied = estimate("Brawler", ascension_level=100, base_stats={"hp": 1000, "attack": 100})
    assert supplied["stats"]["hp"] == 2100, supplied["stats"]
    assert supplied["stats"]["attack"] == 204, supplied["stats"]   # +2% then x2

    # name resolution: their "Diety" vs the spelling players use, and a
    # partial name for a two-word specialization
    assert find_class("deity")["name"] == "Diety", find_class("deity")
    assert find_class("summoner")["name"].endswith("Summoner"), find_class("summoner")
    assert find_class("nonexistent zzz") is None

    text = format_entry("Duelist")
    assert "dexterity +25%" in text and "Duelist Weapon Power" in text, text

    # kind= forces the pool. "Heretic" is BOTH a specialization name and a
    # class-ish word; a caller saying class= must not get the spec back with
    # its empty modifiers, which silently dropped the real class's numbers.
    assert find_class("Heretic")["kind"] == "specialization"
    assert find_class("Heretic", kind="class") is None, find_class("Heretic", kind="class")
    seq = find_class("Sequencer", kind="class")
    assert seq["kind"] == "class" and seq["stat_modifiers"]["dexterity"] == 15, seq
    assert find_class("Gilgamesh", kind="specialization")["base_stats"]["hp"] == 12509

    # search() by name, by a bonus-stat key, and a miss. Pinned because the
    # first version sliced a dict and raised TypeError on every single call.
    assert "Gilgamesh" in search("Gilgamesh")
    assert "Duelist" in search("duelist")
    by_bonus = search("weapon_power")
    assert "Duelist" in by_bonus, by_bonus[:200]
    assert search("zzz no such thing") == ""
    print("orna_classes: all checks passed")


if __name__ == "__main__":
    _demo()
