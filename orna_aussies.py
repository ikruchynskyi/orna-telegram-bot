"""
orna_aussies.py - Orna's full structured item/monster/etc. database, as
exposed by aussiescodex.com's own frontend data files (not to be confused
with orna_codex.py, which reads playorna.com's per-page rendered JSON -
good for browsing one entry, but each page has to be fetched individually
and doesn't expose buffs/debuffs/immunities as a searchable list).

Two files:
  - codex.json: every item/monster/boss/class/follower/raid/spell/
    building/dungeon record in one dump. Buffs/debuffs/immunities/things
    an item *causes* on a target are encoded as short internal codes
    (e.g. "mag_u" = Mag Up I, "t__mag_uuu" = Team Mag Up III) - the
    game's own internal effect-string vocabulary, not a display name.
  - translations.en.json: maps every one of those codes straight to its
    human-readable name/arrow notation (e.g. "mag_u" -> "Mag ↑",
    "t__mag_uuu" -> "T. Mag ↑↑↑"), plus a `main` section
    keyed by record id -> {name, description}. Decoding is a flat
    dictionary lookup; no need to hand-parse the up/down/team encoding -
    _build_stem_directions derives the valid tiers per stat from this
    file directly, so it stays correct if the game adds more later.

This is what makes "what items give immunity to stunned" or "what gives
T Mag 3" answerable as a real structured search (query_records) instead
of free-text scraping. Record ids are the same slugs playorna.com uses
for its own /codex/<category>/<id>/ URLs (verified directly against
several items and a boss with a disambiguating suffix, e.g.
"ankou-eef994e0") - so a match here can be hand off straight into
orna_codex.fetch_codex_json for the full rendered page.

query_records generalizes this further: any combination of stat
comparisons (mag > 250), description/name substring search, effect
matches, and flat-attribute filters (rarity, tier, useable_by, ...),
combined with AND/OR - see _eval_condition for the condition shapes.
Results link to aussiescodex.com's own pages (build_url) rather than
playorna - but aussiescodex only actually has browsable pages for
items/bosses/followers/spells (verified: every other category 404s
there, and its own nav doesn't link them either), so build_url falls
back to playorna.com for monsters/raids/classes/buildings/dungeons.

Network: httpx. Both files (~2.2MB + ~0.8MB) are cached on disk
(.aussies_cache/, gitignored) with a 1-week TTL, not re-downloaded on
every query - the game's data doesn't change day to day, and a week is a
reasonable staleness bound against Orna's own patch cadence.
"""
from __future__ import annotations

import difflib
import json
import logging
import operator
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

BASE = "https://www.aussiescodex.com"
CODEX_URL = f"{BASE}/api/data/codex.json"
TRANSLATIONS_URL = f"{BASE}/api/data/translations.en.json"
HEADERS = {
    "accept": "*/*",
    "referer": f"{BASE}/orna-items",
    "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0 Safari/537.36",
}
HTTP_TIMEOUT = 30.0

CACHE_DIR = Path(__file__).parent / ".aussies_cache"
CACHE_TTL_SECONDS = 7 * 24 * 3600


def _cache_path(name: str) -> Path:
    return CACHE_DIR / name


def _fetch_json(url: str, cache_name: str) -> dict:
    path = _cache_path(cache_name)
    if path.exists() and time.time() - path.stat().st_mtime < CACHE_TTL_SECONDS:
        return json.loads(path.read_text())
    resp = httpx.get(url, headers=HEADERS, timeout=HTTP_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    CACHE_DIR.mkdir(exist_ok=True)
    path.write_text(json.dumps(data))
    return data


_codex_cache: Optional[dict] = None
_translations_cache: Optional[dict] = None
_reverse_status_cache: Optional[dict] = None
_stem_directions_cache: Optional[dict] = None


def _codex() -> dict:
    global _codex_cache
    if _codex_cache is None:
        _codex_cache = _fetch_json(CODEX_URL, "codex.json")
    return _codex_cache


def _translations() -> dict:
    global _translations_cache
    if _translations_cache is None:
        _translations_cache = _fetch_json(TRANSLATIONS_URL, "translations.en.json")
    return _translations_cache


def refetch_now() -> dict:
    """Force a fresh download right now, ignoring the TTL, and return
    small stats about what came back (record count per category, size of
    the stats/status vocabularies) - for a status reply to whoever
    triggered it, e.g. /update_codex."""
    refresh_cache()
    codex = _codex()["main"]
    translations = _translations()
    return {
        "categories": {cat: len(records) for cat, records in codex.items()},
        "stats_vocab": len(translations.get("stats", {})),
        "status_vocab": len(translations.get("status", {})),
    }


def refresh_cache() -> None:
    """Force a re-download next time either file is needed."""
    global _codex_cache, _translations_cache, _reverse_status_cache, _stem_directions_cache, _stat_field_cache, _attr_field_cache
    _codex_cache = _translations_cache = _reverse_status_cache = _stem_directions_cache = _stat_field_cache = _attr_field_cache = None
    for name in ("codex.json", "translations.en.json"):
        _cache_path(name).unlink(missing_ok=True)


def decode(code: str) -> str:
    """Translate one raw code to its human-readable name."""
    t = _translations()
    if code in t.get("status", {}):
        return t["status"][code]
    ab = t.get("abilities", {}).get(code)
    if ab:
        return ab.get("name", code)
    for section in ("stats", "place", "type", "item_type", "useable_by", "family", "rarity", "element", "targets", "spell_type", "tags"):
        val = t.get(section, {}).get(code)
        if val:
            return val
    return code.replace("_", " ").title()


def display_name(category: str, record_id: str) -> str:
    entry = _translations().get("main", {}).get(record_id)
    if entry:
        return entry.get("name", record_id)
    return record_id.replace("-", " ").title()


def description(record_id: str) -> str:
    return _translations().get("main", {}).get(record_id, {}).get("description", "")


# aussiescodex.com's own URL scheme (https://www.aussiescodex.com/orna-<segment>/<id>),
# verified directly - only these four categories actually have pages there
# (every other category 404s, and the site's own nav doesn't link them).
_AUSSIES_URL_SEGMENTS = {
    "items": "orna-items",
    "bosses": "orna-bosses",
    "followers": "orna-followers",
    "spells": "orna-skills",  # note: not "orna-spells"
}


def build_url(category: str, record_id: str) -> str:
    """aussiescodex.com's own page for this record if it has one, else
    fall back to playorna.com's /codex/<category>/<id>/ (which does cover
    every category) so a monster/raid/class/building/dungeon match still
    links somewhere real instead of a dead aussiescodex 404."""
    segment = _AUSSIES_URL_SEGMENTS.get(category)
    if segment:
        return f"{BASE}/{segment}/{record_id}"
    return f"https://playorna.com/codex/{category}/{record_id}/"


def has_aussies_page(category: str) -> bool:
    """True only for the 4 categories aussiescodex actually has pages
    for (see _AUSSIES_URL_SEGMENTS) - used to decide whether an "Assess"
    link is worth showing at all, rather than one that's just a redundant
    second link back to the same playorna page build_url already falls
    back to for every other category."""
    return category in _AUSSIES_URL_SEGMENTS


# -----------------------------------------------------------------------------
# resolving a human search term back to one or more effect codes
# -----------------------------------------------------------------------------

_TEAM_RE = re.compile(r"^\s*(?:team|t\.?)\s*", re.IGNORECASE)
_MAGNITUDE_WORDS = {"i": 1, "ii": 2, "iii": 3, "1": 1, "2": 2, "3": 3}
_STAT_ALIASES = {
    "att": "att", "attack": "att",
    "def": "def", "defense": "def", "defence": "def",
    "mag": "mag", "magic": "mag",
    "dex": "dex", "dexterity": "dex",
    "res": "res", "resistance": "res",
    "crit": "crit",
    "all": "all",
    "dmg": "dmg", "damage": "dmg",
    "foresight": "foresight",
}


def _build_stem_directions() -> dict:
    """{(team, base_stem): {ud strings}} derived live from
    translations['status'] keys - not hardcoded, so a new tier/stat the
    game adds still resolves correctly. Team and non-team are genuinely
    asymmetric in the real data (e.g. non-team "Att Down" only goes to
    tier 1, "T. Att Down" goes to tier 3) so they're kept as separate
    keys rather than merged into one set per stem."""
    global _stem_directions_cache
    if _stem_directions_cache is not None:
        return _stem_directions_cache
    stems: dict = {}
    for k in _translations().get("status", {}):
        m = re.match(r"^(t__)?([a-z_]+?)_([ud]+)$", k)
        if m:
            team_prefix, base, ud = m.groups()
            stems.setdefault((bool(team_prefix), base), set()).add(ud)
    _stem_directions_cache = stems
    return stems


def _parse_buff_query(term: str) -> Optional[str]:
    """Try to parse "T Mag 3" / "team attack down 2" / "Mag Up" /
    "t.mag ++" / "Def ↓↓" into an exact code like "t__mag_uuu". None if
    it doesn't look like this pattern at all - caller falls back to
    fuzzy status matching."""
    text = term.strip().lower()
    team = bool(_TEAM_RE.match(text))
    text = _TEAM_RE.sub("", text)

    direction = None
    magnitude = 1

    # arrow runs encode direction + magnitude together in one token,
    # exactly like the game's own display ("Mag ↑↑" = tier 2 up).
    arrows = re.search(r"(↑{1,3}|↓{1,3})", text)
    if arrows:
        token = arrows.group(1)
        direction = "u" if token[0] == "↑" else "d"
        magnitude = len(token)
        text = (text[:arrows.start()] + text[arrows.end():]).strip()
    else:
        if re.search(r"\bup\b", text):
            direction = "u"
        elif re.search(r"\bdown\b", text):
            direction = "d"
        text = re.sub(r"\b(up|down)\b", "", text).strip()

        # "+"/"-" run notation is common shorthand for the same thing
        # ("T Mag ++" = tier 2 up, "Def --" = tier 2 down).
        signs = re.search(r"([+]{1,3}|-{1,3})", text)
        if signs:
            token = signs.group(1)
            if direction is None:
                direction = "u" if token[0] == "+" else "d"
            magnitude = len(token)
            text = (text[:signs.start()] + text[signs.end():]).strip()
        else:
            m = re.search(r"\b(i{1,3}|[123])\b", text)
            if m:
                magnitude = _MAGNITUDE_WORDS.get(m.group(1), 1)
                text = text[:m.start()].strip()

    stat = _STAT_ALIASES.get(text.strip())
    if not stat:
        return None
    if direction is None:
        direction = "u"  # bare "T Mag 3" - buffs are the far more common ask than debuffs

    stems = _build_stem_directions()
    same_direction = {ud for ud in stems.get((team, stat), set()) if ud[0] == direction}
    ud = direction * magnitude
    if ud not in same_direction:
        if not same_direction:
            return None
        ud = max(same_direction, key=len)  # requested tier doesn't exist - use the highest that does
    return f"t__{stat}_{ud}" if team else f"{stat}_{ud}"


def _reverse_status() -> dict:
    """normalized (lowercased, arrows spelled out) human text -> code."""
    global _reverse_status_cache
    if _reverse_status_cache is None:
        rev = {}
        for code, human in _translations().get("status", {}).items():
            norm = human.lower().replace("↑", " up").replace("↓", " down").replace(".", "")
            rev[re.sub(r"\s+", " ", norm).strip()] = code
        _reverse_status_cache = rev
    return _reverse_status_cache


def resolve_codes(term: str, limit: int = 3) -> list:
    """Best-effort: turn a human search term into one or more candidate
    effect codes, most-likely first."""
    exact = _parse_buff_query(term)
    if exact and exact in _translations().get("status", {}):
        return [exact]

    rev = _reverse_status()
    norm = re.sub(r"\s+", " ", term.strip().lower())
    if norm in rev:
        return [rev[norm]]
    close = difflib.get_close_matches(norm, rev.keys(), n=limit, cutoff=0.6)
    if close:
        return [rev[c] for c in close]
    subset = [code for human, code in rev.items() if norm in human]
    return subset[:limit]


# -----------------------------------------------------------------------------
# searching records
# -----------------------------------------------------------------------------

@dataclass
class EffectMatch:
    category: str
    id: str
    name: str
    field: str  # "immunities" | "causes" | "gives"
    code: str
    chance: Optional[str] = None
    tier: Optional[int] = None
    sort_value: Optional[str] = None  # set when query_records was called with sort_by



# -----------------------------------------------------------------------------
# generic multi-attribute query - any stat comparison, text substring,
# effect, or flat attribute, combined with AND/OR
# -----------------------------------------------------------------------------

_CMP_OPS = {">": operator.gt, "<": operator.lt, ">=": operator.ge, "<=": operator.le,
            "=": operator.eq, "==": operator.eq, "!=": operator.ne}
_NUM_RE = re.compile(r"-?\d+(?:\.\d+)?")
_EFFECT_LIST_FIELDS = ("immunities", "causes", "gives", "cures")
_USEABLE_BY_ALIASES = {
    "mage": "magic", "mages": "magic", "magic": "magic", "magic_user": "magic", "magic_users": "magic",
    "warrior": "warrior", "warriors": "warrior", "melee": "melee",
    "thief": "thief", "thieves": "thief", "rogue": "thief", "rogues": "thief",
    "summoner": "summoner", "summoners": "summoner", "valhallan": "valhallan",
}

_stat_field_cache: Optional[dict] = None


def _all_stat_fields() -> dict:
    """normalized (lowercase, spaces/hyphens->underscore) -> real stats key,
    from translations['stats'] - the authoritative list of every stat name
    the game data uses (attack/magic/... plus long-tail ones like
    follower_stats, crit_damage, multi-target_damage)."""
    global _stat_field_cache
    if _stat_field_cache is None:
        keys = _translations().get("stats", {}).keys()
        _stat_field_cache = {k.lower().replace(" ", "_").replace("-", "_"): k for k in keys}
    return _stat_field_cache


def _resolve_stat_field(field: str, record_keys=()) -> Optional[str]:
    """LLM-provided field name -> real stats dict key. Tries an exact
    (normalized) match first, then fuzzy match, so minor spelling/plural
    drift from the model (e.g. 'follower_stat' vs 'follower_stats') still
    resolves."""
    norm = field.strip().lower().replace(" ", "_").replace("-", "_")
    fields = _all_stat_fields()
    if norm in fields:
        return fields[norm]
    pool = list(fields.keys()) + list(record_keys)
    close = difflib.get_close_matches(norm, pool, n=1, cutoff=0.6)
    if not close:
        return None
    return fields.get(close[0], close[0])


# flat top-level fields that are cross-links to OTHER codex entries (an
# item's "dropped_by" monster, a spell's "learned_by" class, ...) - these
# are already browsable via codex-bootstrap's own "sections", not
# meaningful as a query_records filter value, so excluded from the
# discovered attribute vocabulary below.
_EXCLUDED_ATTR_FIELDS = {
    "id", "category", "stats", "immunities", "causes", "gives", "cures",
    "drops", "dropped_by", "skills", "abilities", "upgrade_materials",
    "learned_by", "used_by", "off-hands", "summons", "celestial_classes",
    "bestial_bond", "source", "ability", "follower",
}

_attr_field_cache: Optional[dict] = None


def _all_attr_fields() -> dict:
    """normalized -> real top-level field name, discovered by scanning
    every real record across every category - the flat, filterable
    (non-cross-link) attribute vocabulary. Built from live data rather
    than hand-maintained, so a field like "events" or "exotic" (or
    whatever the game adds next) is usable without a code change."""
    global _attr_field_cache
    if _attr_field_cache is None:
        keys = set()
        for records in _codex()["main"].values():
            for rec in records.values():
                keys.update(rec.keys())
        keys -= _EXCLUDED_ATTR_FIELDS
        _attr_field_cache = {k.lower().replace(" ", "_").replace("-", "_"): k for k in keys}
    return _attr_field_cache


def _resolve_attr_field(field: str) -> Optional[str]:
    norm = field.strip().lower().replace(" ", "_").replace("-", "_")
    fields = _all_attr_fields()
    if norm in fields:
        return fields[norm]
    close = difflib.get_close_matches(norm, fields.keys(), n=1, cutoff=0.6)
    return fields[close[0]] if close else None


def _parse_number(raw) -> Optional[float]:
    """'130', '+5', '2%', '-10', '2,500_orns', 5 -> 130.0, 5.0, 2.0, -10.0,
    2500.0, 5.0. None on failure."""
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    s = str(raw).replace(",", "").strip().rstrip("%").lstrip("+")
    try:
        return float(s)
    except ValueError:
        m = _NUM_RE.search(s)
        return float(m.group(0)) if m else None


def _eval_condition(record: dict, cond: dict) -> bool:
    """One leaf condition. `record` must carry its own "id"/"category"
    (every raw codex.json record already does). Shapes:
      {"kind": "stat", "field": "<stats key e.g. magic/attack/crit>",
       "cmp": ">"|"<"|">="|"<="|"=", "value": <number>}
      {"kind": "text", "field": "description"|"name"|"", "value": "<substring>"}
      {"kind": "effect", "field": "immunities"|"causes"|"gives"|"cures"|"", "value": "<human text>"}
      {"kind": "attr", "field": "<any flat record field - tier/rarity/useable_by/
       place/type/item_type/family/element/events/tags/exotic/new/hidden/price/...>",
       "cmp": "="|">"|"<"|">="|"<=", "value": <text, number, or true/false>}
    An unrecognised kind/field never matches (fails closed, not open)."""
    kind = cond.get("kind")
    field = cond.get("field") or ""

    if kind == "stat":
        stats = record.get("stats") or {}
        real_field = field if field in stats else _resolve_stat_field(field, stats.keys())
        val = _parse_number(stats.get(real_field))
        target = _parse_number(cond.get("value"))
        op = _CMP_OPS.get(cond.get("cmp", ">"))
        return val is not None and target is not None and op is not None and op(val, target)

    if kind == "text":
        needle = str(cond.get("value", "")).strip().lower()
        if not needle:
            return False
        haystacks = []
        if field in ("", "description"):
            haystacks.append(description(record["id"]))
        if field in ("", "name"):
            haystacks.append(display_name(record["category"], record["id"]))
        return any(needle in h.lower() for h in haystacks if h)

    if kind == "ability":
        # "this item grants a spell/skill when equipped" has THREE
        # different real encodings, all seen live - genuinely a different
        # thing from an "effect" (a buff/debuff code). Live reports: (1)
        # "gives an additional spell" was tried as kind:"effect" value:
        # "spell", which can never match since "spell" isn't a status/buff
        # name; (2) even after adding this kind checking only the top-
        # level "ability" cross-link, "Hyades Wreath" (which DOES grant
        # Rainsong) was still missed, because it encodes the grant as
        # stats["+spell"] = "Rainsong" (a plain string VALUE, not a
        # [category, id] link) - a "stat" condition can't reach this
        # either, since _parse_number("Rainsong") is never a number.
        value = str(cond.get("value", "")).strip().lower()
        candidates = []
        ability = record.get("ability")
        if isinstance(ability, list) and len(ability) == 2:
            spell_cat, spell_id = ability
            candidates.append(display_name(spell_cat, spell_id))
            candidates.append(spell_id.replace("-", " "))
        stats = record.get("stats") or {}
        for key in ("+spell", "+skill"):
            granted = stats.get(key)
            if isinstance(granted, str):
                candidates.append(granted)
        if not candidates:
            return False
        if not value:
            return True  # bare "has any bonus ability/spell" check
        return any(value in c.lower() for c in candidates)

    if kind == "effect":
        codes = resolve_codes(str(cond.get("value", "")))
        if not codes:
            return False
        target_fields = [field] if field in _EFFECT_LIST_FIELDS else list(_EFFECT_LIST_FIELDS)
        return any(e.get("name") in codes for f in target_fields for e in (record.get(f) or []))

    if kind == "attr":
        real_field = field if field in record else _resolve_attr_field(field)
        raw = record.get(real_field) if real_field else None
        if raw is None:
            # a handful of stats-dict entries (e.g. items' "element") aren't
            # numeric and don't belong in the "stat" kind - fall back to
            # the stats dict for anything not found as a top-level field.
            raw = (record.get("stats") or {}).get(field)
        cmp_op = cond.get("cmp", "=")
        if cmp_op in (">", "<", ">=", "<="):
            val, target = _parse_number(raw), _parse_number(cond.get("value"))
            op = _CMP_OPS.get(cmp_op)
            return val is not None and target is not None and op is not None and op(val, target)
        target_text = str(cond.get("value", "")).strip().lower()
        if real_field == "useable_by":
            # real values are "magic_users"/"melee_classes"/"thief_classes"/
            # "warrior_classes"/"valhallan_summoner_classes"/"all_classes" -
            # the natural class NAME a player types ("mage", "thief") often
            # isn't a literal substring of that (e.g. "mage" isn't in
            # "magic_users" - "magi" is, "mage" isn't), so map common class
            # nicknames onto a substring that actually IS. And a query for
            # one specific class must ALSO match "all_classes" - that class
            # genuinely can use those too (live case: Hyades Wreath, an
            # "all_classes" item, is a valid answer to "something for a
            # mage" and was wrongly excluded before this).
            target_text = _USEABLE_BY_ALIASES.get(target_text, target_text)
            # every real item has this field populated today (verified
            # directly - 0/2764 missing or empty), but a record with no
            # restriction stated at all should read the same as
            # "all_classes", not "nothing" - defensive, not currently
            # load-bearing.
            raw_text = str(raw or "all_classes").strip().lower()
            return target_text in raw_text or raw_text == "all_classes"
        if isinstance(raw, bool) or target_text in ("true", "yes", "1", "false", "no", "0"):
            # boolean-flag fields (exotic/new/hidden/...) are presence-only
            # in the source data - the key exists and is True on a match,
            # and is simply ABSENT (never explicitly False) otherwise - so
            # "false"/"no" must treat a missing field as a match too.
            if target_text in ("true", "yes", "1"):
                return raw is True
            if target_text in ("false", "no", "0"):
                return raw is False or raw is None
            return False
        if isinstance(raw, list):
            # aussiescodex sometimes encodes a single string as a list of
            # its individual characters (seen on items' stats.element,
            # e.g. "arcane" -> ['a','r','c','a','n','e']) - rejoin before
            # comparing rather than doing per-character matching.
            if raw and all(isinstance(x, str) and len(x) == 1 for x in raw):
                raw_text = "".join(raw).strip().lower()
                return bool(target_text) and (raw_text == target_text or target_text in raw_text)
            norm_items = [str(x).strip().lower().replace(" ", "_") for x in raw]
            return bool(target_text) and any(target_text.replace(" ", "_") in item for item in norm_items)
        raw_text = str(raw or "").strip().lower()
        return bool(target_text) and (raw_text == target_text or target_text in raw_text)

    return False


def query_records(conditions: list, combinator: str = "and", category: Optional[str] = None,
                   limit: int = 50, sort_by: Optional[str] = None, sort_dir: str = "desc",
                   offset: int = 0) -> list:
    """Generic multi-attribute search: evaluate `conditions` (see
    _eval_condition) against every record, combined with AND/OR. Returns
    EffectMatch-shaped results (field/code/chance left blank - only
    category/id/name/tier/sort_value apply to a multi-attribute query
    result).

    `sort_by`, when given, ranks matches by that stat (resolved the same
    fuzzy way as a "stat" condition's field) instead of codex.json's own
    order - lets "the item with the biggest mag" work as sort_by="magic"
    with no filter conditions at all (conditions may be empty in that
    case). Records missing that stat entirely are excluded, since there's
    nothing to rank them by. `offset` skips the top N ranked results
    (e.g. the 2nd-highest)."""
    if not conditions and not sort_by:
        return []
    combine = any if combinator == "or" else all
    codex = _codex()["main"]
    categories = [category] if category and category in codex else list(codex.keys())

    matched: list = []
    for cat in categories:
        for record in codex.get(cat, {}).values():
            if conditions and not combine(_eval_condition(record, c) for c in conditions):
                continue
            sort_value = None
            if sort_by:
                stats = record.get("stats") or {}
                real_field = sort_by if sort_by in stats else _resolve_stat_field(sort_by, stats.keys())
                sort_value = _parse_number(stats.get(real_field)) if real_field else None
                if sort_value is None:
                    continue
            matched.append((sort_value, EffectMatch(
                category=record["category"], id=record["id"],
                name=display_name(record["category"], record["id"]),
                field="", code="", tier=record.get("tier"),
                sort_value=f"{sort_value:g}" if sort_value is not None else None,
            )))

    if sort_by:
        matched.sort(key=lambda pair: pair[0], reverse=(sort_dir != "asc"))
    results = [m for _, m in matched]
    return results[offset:offset + limit]
