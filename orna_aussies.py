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
    (e.g. "mag_u" = Mag Up I, "t__mag_uuu" = Temp. Mag Up III) - the
    game's own internal effect-string vocabulary, not a display name. The
    "t__" prefix's own displayed abbreviation ("T.") reads like it could
    mean "Team", but it doesn't - verified directly against playorna.com's
    own served icon filenames (e.g. "T. Def ↑" -> defense_up_temp.png,
    vs. plain "Def ↑" -> defense_up.png, same pairing for Res): it's
    "Temp[orary]", a status that came from something time-limited (a
    follower's bond proc, a consumable) rather than a normal spell/skill
    grant - not a whole-party effect.
  - translations.en.json: maps every one of those codes straight to its
    human-readable name/arrow notation (e.g. "mag_u" -> "Mag ↑",
    "t__mag_uuu" -> "T. Mag ↑↑↑"), plus a `main` section
    keyed by record id -> {name, description}. Decoding is a flat
    dictionary lookup; no need to hand-parse the up/down/temp encoding -
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
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            # A cache file truncated by a crash/kill mid-write (this repo
            # reloads via launchctl often) would otherwise raise an uncaught
            # JSONDecodeError on every call until the week-long TTL expires -
            # treat an unreadable cache as a miss and re-fetch instead.
            logger.warning("aussies: cache %s unreadable, refetching", cache_name)
    resp = httpx.get(url, headers=HEADERS, timeout=HTTP_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    CACHE_DIR.mkdir(exist_ok=True)
    # Atomic write (temp then rename) so a crash mid-write can't leave a
    # half-written, unparseable cache file behind for the read path above.
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    tmp.replace(path)
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
    global _codex_cache, _translations_cache, _reverse_status_cache, _stem_directions_cache, _stat_field_cache, _attr_field_cache, _NAME_INDEX, _ALL_NAMES
    _codex_cache = _translations_cache = _reverse_status_cache = _stem_directions_cache = _stat_field_cache = _attr_field_cache = _NAME_INDEX = _ALL_NAMES = None
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

# The "t__" prefix means "Temp[orary]", not "Team" (see the module
# docstring for the icon-filename evidence) - "team" is still accepted as
# a recognized input synonym since that's the natural guess a player
# would make from the "T." abbreviation alone, just not what it actually
# means internally. Longest/most-specific alternative first ("temporary"
# before "temp" before "team" before the bare "t\.?") - a bare "t" placed
# earlier would greedily match and leave the rest of the word dangling,
# the exact ordering bug already hit once with "team" vs "t\.?" alone.
_TEMP_RE = re.compile(r"^\s*(?:temporary|temp|team|t\.?)\s*", re.IGNORECASE)
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
    """{(is_temp, base_stem): {ud strings}} derived live from
    translations['status'] keys - not hardcoded, so a new tier/stat the
    game adds still resolves correctly. Temp and non-temp are genuinely
    asymmetric in the real data (e.g. non-temp "Att Down" only goes to
    tier 1, "T. Att Down" goes to tier 3) so they're kept as separate
    keys rather than merged into one set per stem."""
    global _stem_directions_cache
    if _stem_directions_cache is not None:
        return _stem_directions_cache
    stems: dict = {}
    for k in _translations().get("status", {}):
        m = re.match(r"^(t__)?([a-z_]+?)_([ud]+)$", k)
        if m:
            temp_prefix, base, ud = m.groups()
            stems.setdefault((bool(temp_prefix), base), set()).add(ud)
    _stem_directions_cache = stems
    return stems


def _parse_buff_query(term: str) -> Optional[str]:
    """Try to parse "T Mag 3" / "temp attack down 2" / "Mag Up" /
    "t.mag ++" / "Def ↓↓" into an exact code like "t__mag_uuu". None if
    it doesn't look like this pattern at all - caller falls back to
    fuzzy status matching."""
    text = term.strip().lower()
    is_temp = bool(_TEMP_RE.match(text))
    text = _TEMP_RE.sub("", text)

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
    same_direction = {ud for ud in stems.get((is_temp, stat), set()) if ud[0] == direction}
    ud = direction * magnitude
    if ud not in same_direction:
        if not same_direction:
            return None
        ud = max(same_direction, key=len)  # requested tier doesn't exist - use the highest that does
    return f"t__{stat}_{ud}" if is_temp else f"{stat}_{ud}"


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


_CLASS_ABILITY_INDEX: Optional[dict] = None


def _class_ability_index() -> dict:
    """alias (lowercased) -> class record id, for every class in codex.json.

    aussies names the gendered pairs as ONE entry ("Beowulf / Bestla",
    "Heretic Ara / Hera Ara"), so an exact lookup for "Beowulf" finds nothing -
    each side of the slash is registered as its own alias."""
    global _CLASS_ABILITY_INDEX
    if _CLASS_ABILITY_INDEX is None:
        index: dict = {}
        for rid in _codex()["main"].get("classes", {}):
            full = display_name("classes", rid) or ""
            for alias in [full] + full.split(" / "):
                alias = alias.strip().lower()
                if alias:
                    index.setdefault(alias, rid)
        _CLASS_ABILITY_INDEX = index
    return _CLASS_ABILITY_INDEX


def class_abilities(name: str) -> list:
    """Every ability of a class or tier-10 specialization, WITH what it does:
    [{"slug", "name", "description"}].

    This is the general answer to "the bot should work the nuances out itself
    rather than having them hand-coded": all 82 classes - including every
    tier-10 specialization and its celestial variants - carry a structured
    `abilities` list in codex.json, and translations.en.json describes all 134
    of them in plain English ("Resurgence: You become more powerful as your HP
    decreases in battle"). So a conditional passive can be SURFACED for any
    class without anyone writing a rule per specialization.

    Note the two sources are complementary, not redundant: orna_classes.json
    carries `passiveEffects` for 13 classes (and is the only place naming the
    Dual Staffs / Dual Wield conditions) but has NOTHING for the tier-10
    specializations, while this has all of them. Callers should show both."""
    rid = _class_ability_index().get((name or "").strip().lower())
    if rid is None:                      # fall back to fuzzy, as elsewhere here
        close = difflib.get_close_matches((name or "").strip().lower(),
                                          list(_class_ability_index()), n=1, cutoff=0.82)
        rid = _class_ability_index().get(close[0]) if close else None
    if rid is None:
        return []
    record = _codex()["main"]["classes"].get(rid) or {}
    table = (_translations().get("abilities") or {})
    out = []
    for entry in record.get("abilities") or []:
        slug = entry.get("name") if isinstance(entry, dict) else str(entry)
        if not slug:
            continue
        info = table.get(slug) or {}
        out.append({"slug": slug,
                    "name": info.get("name") or slug.replace("_", " ").title(),
                    "description": (info.get("description") or "").strip()})
    return out


_ALL_NAMES: Optional[list] = None


def all_codex_names() -> list:
    """Every display name in codex.json, across all nine categories (~5k)."""
    global _ALL_NAMES
    if _ALL_NAMES is None:
        out = []
        for category, records in _codex()["main"].items():
            for rid in records:
                name = display_name(category, rid)
                if name:
                    out.append(name)
        _ALL_NAMES = out
    return _ALL_NAMES


def fuzzy_codex_name(query: str, cutoff: float = 0.72) -> str:
    """The real codex name `query` most likely MEANT, or "".

    Live 2026-09-26: "/orna what crest of feeling does?" - the real item is
    `Crest of the Felling`, one substituted letter away - and the loop answered
    "no such item exists in the current codex database" while its own search for
    "crest" had listed the right name. `search_codex`'s mechanical ladder cannot
    reach it (it strips quality words, possessives and trailing words; a typo
    INSIDE a word is a different shape), so this matches the whole query against
    the real name vocabulary instead. Same "fuzzy-correct against the corpus's
    own words" fix orna_knowledge.search and _resolve_stat_field already use.

    Fast enough to call inline - difflib over ~5k names measured at under 10ms -
    but callers still go through asyncio.to_thread because building the
    vocabulary can trigger the aussies cache fetch."""
    query = (query or "").strip()
    if len(query) < 4:
        return ""                      # too short to disambiguate anything
    names = all_codex_names()
    matches = difflib.get_close_matches(query, names, n=1, cutoff=cutoff)
    if matches and matches[0].lower() != query.lower():
        return matches[0]
    return ""


_NAME_INDEX: Optional[dict] = None  # name.lower() -> [(category, id), ...]

# For a name that spans categories, a bare mention most often means the
# fightable thing ("how do I beat / what drops X") over an item of the same
# name; items next; the rest after.
_CATEGORY_PRIORITY = ("raids", "bosses", "monsters", "items", "followers",
                      "spells", "classes", "dungeons", "buildings")


def _name_index() -> dict:
    """name.lower() -> [(category, id), ...], built once from codex.json +
    translations. Records carry no name in codex.json (names live in
    translations), so this is the reverse of display_name over every record."""
    global _NAME_INDEX
    if _NAME_INDEX is None:
        idx: dict = {}
        for category, records in _codex()["main"].items():
            for rid in records:
                nm = display_name(category, rid)
                if nm:
                    idx.setdefault(nm.lower(), []).append((category, rid))
        _NAME_INDEX = idx
    return _NAME_INDEX


def resolve_entity(name: str) -> dict:
    """Resolve a display name to a codex (category, id), entirely from the
    local dump (no network). Exact name first, then fuzzy_codex_name for a
    typo/transliteration. A name in several categories returns the
    priority-ordered pick with the rest in `alternatives`, so the caller can
    note them without a second round-trip. Nothing resolvable -> {"unresolved": name}."""
    q = (name or "").strip()
    if not q:
        return {"unresolved": name}
    hits = _name_index().get(q.lower())
    if not hits:
        fuzzy = fuzzy_codex_name(q)
        if fuzzy:
            hits = _name_index().get(fuzzy.lower())
    if not hits:
        return {"unresolved": name}
    ordered = sorted(hits, key=lambda ci: _CATEGORY_PRIORITY.index(ci[0])
                     if ci[0] in _CATEGORY_PRIORITY else 99)
    cat, rid = ordered[0]
    return {
        "category": cat, "id": rid, "name": display_name(cat, rid) or rid,
        "alternatives": [(c, i, display_name(c, i) or i) for c, i in ordered[1:]],
    }


# Every one of these edge fields is a list of [category, id] cross-links in
# codex.json (verified live). The default expand set is category-aware.
_GRAPH_EDGES = ("drops", "skills", "dropped_by", "upgrade_materials",
                "used_by", "learned_by")
_DEFAULT_EXPAND = {
    "raids": ("drops", "skills"),
    "bosses": ("drops", "skills"),
    "monsters": ("drops", "skills"),
    "dungeons": ("drops",),
    "items": ("dropped_by", "upgrade_materials"),
    "followers": ("skills",),
    "spells": ("learned_by", "used_by"),
    "classes": ("skills",),
    "buildings": (),
}
# ponytail: bestial_bond (a follower's spell/bond grants) is a nested
# list-of-tiers, not [cat,id] pairs, so it is not expanded here. Add it in
# phase 2 if a request needs "which follower grants X"; the drops/skills use
# cases this tool targets don't touch it.


def _effect_names(record: dict) -> list:
    """Human effect labels from a record's causes/gives/immunities/cures
    lists. Each entry is {"name": <status-code>, "chance"?: "10%"}; the code
    is humanized via translations['status'], falling back to the raw code so
    an unknown code degrades to text rather than crashing."""
    status = _translations().get("status", {})
    out = []
    for field in ("causes", "gives", "immunities", "cures"):
        for e in record.get(field) or []:
            code = e.get("name") if isinstance(e, dict) else e
            if not code:
                continue
            human = status.get(code, code)
            chance = e.get("chance") if isinstance(e, dict) else None
            out.append(f"{field}:{human}" + (f"({chance})" if chance else ""))
    return out


def _leaf(category: str, rid: str) -> dict:
    """Compact analysis view of one cross-linked record. A dangling id (not in
    the dump) degrades to just its id as the name, empty everything else."""
    r = _codex()["main"].get(category, {}).get(rid) or {}
    return {
        "category": category, "id": rid,
        "name": display_name(category, rid) or rid,
        "useable_by": r.get("useable_by"),
        "place": r.get("place"), "item_type": r.get("item_type"),
        "tier": r.get("tier"), "rarity": r.get("rarity"),
        "stats": dict(r.get("stats") or {}),
        "effects": _effect_names(r),
    }


def _entity_facts(rec: dict) -> dict:
    facts = {}
    for k in ("tier", "rarity", "hp", "place", "item_type", "useable_by", "events"):
        v = rec.get(k)
        if v not in (None, "", [], {}):
            facts[k] = v
    if rec.get("stats"):
        facts["stats"] = dict(rec["stats"])
    eff = _effect_names(rec)
    if eff:
        facts["effects"] = eff
    return facts


def build_supergraph(names, expand=None, per_relation_cap: int = 12) -> dict:
    """One-level subgraph for one or more entity names, built entirely from
    codex.json + translations (no network). Returns
      {"entities": [entity, ...], "unresolved": [name, ...]}
    where entity = {"category","id","name","facts":dict,
                    "alternatives":[(cat,id,name)],"relations":[relation,...]},
          relation = {"field","title","total","partial","members":[leaf,...]},
          leaf     = see _leaf.
    per_relation_cap bounds each relation; an over-cap relation is truncated
    with partial=True and the true total kept. Pure/in-memory; call via
    asyncio.to_thread."""
    if isinstance(names, str):
        names = [names]
    entities, unresolved = [], []
    codex = _codex()["main"]
    for name in names:
        res = resolve_entity(name)
        if "id" not in res:
            unresolved.append(name)
            continue
        cat, rid = res["category"], res["id"]
        rec = codex[cat][rid]
        fields = expand if expand is not None else _DEFAULT_EXPAND.get(cat, ())
        relations = []
        for field in fields:
            raw = rec.get(field) or []
            pairs = [(x[0], x[1]) for x in raw
                     if isinstance(x, (list, tuple)) and len(x) == 2]
            if not pairs:
                continue
            members = [_leaf(c, i) for c, i in pairs[:per_relation_cap]]
            relations.append({
                "field": field,
                "title": field.replace("_", " ").title(),
                "total": len(pairs),
                "partial": len(pairs) > per_relation_cap,
                "members": members,
            })
        entities.append({
            "category": cat, "id": rid, "name": res["name"],
            "facts": _entity_facts(rec),
            "alternatives": res.get("alternatives") or [],
            "relations": relations,
        })
    return {"entities": entities, "unresolved": unresolved}


def unresolvable_condition_fields(conditions: list) -> list:
    """Which of `conditions`' field names resolve to nothing, as
    [(kind, field, [suggestions])].

    An unresolvable field used to be indistinguishable from a real absence:
    _eval_condition simply never matched it, so query_records returned 0 rows
    and the caller read that as "nothing in the game has this". Live 2026-09-25
    the loop filtered on `dropped_by` (deliberately excluded as a cross-link
    field, so not in the attr vocabulary), got 0, and reported "this boss drops
    nothing usable by mages" - the right answer, reached from no evidence at
    all; the same 0 would have been produced had the answer been yes. A caller
    that tells the model its FIELD was unusable gets a corrected retry, which
    is the whole point of the loop; a silent 0 gets a confident guess.

    Only `attr` and `stat` conditions have a resolvable field vocabulary.
    `text`/`effect`/`ability` fields are fixed small sets handled in
    _eval_condition itself, so they are not checked here."""
    bad = []
    for cond in conditions or []:
        if not isinstance(cond, dict):
            continue
        kind = str(cond.get("kind", "")).strip().lower()
        field = str(cond.get("field", "") or "").strip()
        if not field:
            continue
        if kind == "attr" and _resolve_attr_field(field) is None:
            vocab = _all_attr_fields()
        elif kind == "stat" and _resolve_stat_field(field) is None:
            vocab = _all_stat_fields()
        else:
            continue
        norm = field.lower().replace(" ", "_").replace("-", "_")
        bad.append((kind, field, difflib.get_close_matches(norm, list(vocab), n=4, cutoff=0.4)))
    return bad


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
        # Followers encode "grants a spell when bonded" completely
        # differently from items: record["bestial_bond"] is a list of bond
        # tiers, each a list of {"name","type",...} entries - type "ABILITY"
        # is a spell/skill slug (e.g. "earth-sigil-2"), as opposed to
        # "BOND" (a status-code proc) or "BONUS" (a passive % stat, see the
        # "effect" branch below and its ponytail note). Live report: "which
        # follower gives earth sigil" found nothing until this was added -
        # verified directly against codex.json that ancient-jinn/anubis
        # both carry an ABILITY entry named "earth-sigil-2".
        for tier in (record.get("bestial_bond") or []):
            for entry in tier:
                if entry.get("type") == "ABILITY":
                    slug = entry.get("name", "")
                    candidates.append(display_name("spells", slug))
                    candidates.append(slug.replace("-", " "))
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
        matched = any(e.get("name") in codes for f in target_fields for e in (record.get(f) or []))
        if not matched and field in ("", "gives"):
            # A follower's bond can also proc a status effect ("BOND"-type
            # bestial_bond entries, e.g. "t__def_uu") - these are already
            # real codes in the same status vocabulary resolve_codes just
            # used, just reached through a different record field than
            # items' own "gives" list. type "BONUS" entries (orn_bonus,
            # crit_chance, ...) are deliberately NOT covered here - they're
            # named % bonuses, not status codes, so resolve_codes can never
            # match them.
            # ponytail: no kind covers BONUS-type bond entries yet - add a
            # kind:"bond_bonus" (field/value against translations['bestial_bond']
            # vocabulary) if that's ever asked for.
            matched = any(
                entry.get("type") == "BOND" and entry.get("name") in codes
                for tier in (record.get("bestial_bond") or []) for entry in tier
            )
        return matched

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
        # "!=" ("not"/"except"/"excluding") negates whatever the equality-
        # style match below would have returned - computed once at the end
        # so every non-numeric branch (bool/list/scalar/useable_by) gets it
        # for free instead of each needing its own negation logic.
        negate = cmp_op in ("!=", "<>")
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
            # Every real ITEM has this field populated (verified directly -
            # 0/2764 missing or empty). An earlier version defaulted a missing
            # value to "all_classes" as a defensive no-op for items, but a
            # query runs over EVERY category, and raids/monsters/bosses/
            # buildings/dungeons legitimately have no useable_by at all - so
            # that default made every one of them match every class filter.
            # Live 2026-09-25: `name ~ "Judge Trifecta" AND useable_by =
            # magic_users` returned the RAID "Judge Trifecta Maximus", i.e. a
            # confident "yes, mages can use it" about a raid. An absent field
            # is now no-match: it costs nothing for items (none are missing
            # it) and is the only correct reading everywhere else.
            raw_text = str(raw).strip().lower() if raw else ""
            # bool(target_text) guard matches the other branches: an empty
            # value must fail closed, not match every record via "" in raw_text.
            matched = bool(target_text) and (target_text in raw_text or raw_text == "all_classes")
        elif isinstance(raw, bool) or (raw is None and target_text in ("true", "yes", "1", "false", "no", "0")):
            # boolean-flag fields (exotic/new/hidden/...) are presence-only
            # in the source data - the key exists and is True on a match,
            # and is simply ABSENT (never explicitly False) otherwise - so
            # "false"/"no" must treat a missing field as a match too. The
            # `raw is None` guard is load-bearing: without it, ANY attr "="
            # query whose value is 0/1 (e.g. {tier "=" 1}) fell in here and
            # `raw is True`/`raw is False` (identity, not ==) is always False
            # for a concrete int like 1, so real tier=0/tier=1 records never
            # matched (and "!=" matched them all). A concrete value goes to
            # the scalar branch below; only an absent field is a flag "false".
            if target_text in ("true", "yes", "1"):
                matched = raw is True
            elif target_text in ("false", "no", "0"):
                matched = raw is False or raw is None
            else:
                matched = False
        elif isinstance(raw, list):
            # aussiescodex sometimes encodes a single string as a list of
            # its individual characters (seen on items' stats.element,
            # e.g. "arcane" -> ['a','r','c','a','n','e']) - rejoin before
            # comparing rather than doing per-character matching.
            if raw and all(isinstance(x, str) and len(x) == 1 for x in raw):
                raw_text = "".join(raw).strip().lower()
                matched = bool(target_text) and (raw_text == target_text or target_text in raw_text)
            else:
                norm_items = [str(x).strip().lower().replace(" ", "_") for x in raw]
                matched = bool(target_text) and any(target_text.replace(" ", "_") in item for item in norm_items)
        else:
            # `str(raw or "")` would turn a legitimate falsy value (0, 0.0)
            # into "" and never match {field "=" 0}; guard on None instead.
            raw_text = ("" if raw is None else str(raw)).strip().lower()
            matched = bool(target_text) and (raw_text == target_text or target_text in raw_text)
        return (not matched) if negate else matched

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
    # Normalize model-supplied literals - a stray "OR"/"ASC" casing must not
    # silently flip to the opposite default (and/desc) with no error.
    combine = any if str(combinator).strip().lower() == "or" else all
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
        matched.sort(key=lambda pair: pair[0], reverse=(str(sort_dir).strip().lower() != "asc"))
    results = [m for _, m in matched]
    return results[offset:offset + limit]


def _demo() -> None:
    """Pins _parse_buff_query's tier-shorthand parsing against known-good
    phrasings - this function has two documented past regressions (arrow/
    plus-run magnitude not counted at all, and the t./team alternation
    ordering bug), exactly the "worked before, quietly stopped" shape a
    future edit nearby could reintroduce with no other signal. Needs
    network/cache access (translations.en.json) like the rest of this
    module. Run directly: python3 orna_aussies.py"""
    cases = {
        "T Mag 3": "t__mag_uuu",
        "team attack down 2": "t__att_dd",  # "team" is still accepted input - see _TEMP_RE
        "Mag Up": "mag_u",
        "t.mag ++": "t__mag_uu",
        "Def ↓↓": "def_dd",
        "T. Att Down": "t__att_d",
        # Both of these ask for tier 3, but non-temp Def Up / Mag Up only
        # go to tier 2 in the real game data (asymmetric temp-vs-non-temp
        # tiers, see _build_stem_directions) - _parse_buff_query falls back
        # to the highest tier that actually exists rather than inventing
        # one, so these correctly resolve one tier lower than requested.
        "Def III": "def_uu",
        "Mag ↑↑↑": "mag_uu",
    }
    for term, expected in cases.items():
        got = _parse_buff_query(term)
        assert got == expected, f"_parse_buff_query({term!r}) = {got!r}, expected {expected!r}"
    # A bogus field must be REPORTED, not silently return 0 rows - a 0 that
    # means "nothing was searched" got read as "nothing exists" live.
    bad = unresolvable_condition_fields([{"kind": "attr", "field": "dropped_by", "value": "x"}])
    assert len(bad) == 1 and bad[0][0] == "attr" and bad[0][1] == "dropped_by", bad
    assert unresolvable_condition_fields([
        {"kind": "attr", "field": "useable_by", "value": "mage"},
        {"kind": "attr", "field": "place", "value": "head"},
        {"kind": "stat", "field": "magic", "cmp": ">", "value": 250},
        {"kind": "text", "field": "name", "value": "judge"},      # fixed vocab, not checked
        {"kind": "effect", "field": "gives", "value": "Def Down"},
    ]) == [], "real fields must not be flagged"
    # fuzzy drift still resolves, so it must NOT be flagged as unresolvable
    assert unresolvable_condition_fields([{"kind": "stat", "field": "follower_stat", "value": 1}]) == []

    # a record with NO useable_by (raids/monsters) must not match a class filter
    assert not _eval_condition({"category": "raids", "id": "x", "name": "X"},
                               {"kind": "attr", "field": "useable_by", "cmp": "=", "value": "mage"})
    # ...while an explicit all_classes item still does
    assert _eval_condition({"category": "items", "id": "y", "name": "Y", "useable_by": "all_classes"},
                           {"kind": "attr", "field": "useable_by", "cmp": "=", "value": "mage"})

    # --- research supergraph: name resolution ---
    r = resolve_entity("Fallen King Centaurus")
    assert r.get("category") == "raids" and r.get("id") == "fallen-king-centaurus", r
    # typo/transliteration recovered via fuzzy_codex_name
    assert resolve_entity("Fallen King Centaurs").get("id") == "fallen-king-centaurus", \
        resolve_entity("Fallen King Centaurs")
    # a name spanning categories picks by priority and surfaces the rest
    amb = resolve_entity("aaru cobra")
    assert amb.get("category") == "monsters", amb          # monsters outranks followers
    assert any(c == "followers" for c, _i, _n in amb.get("alternatives", [])), amb
    # nothing resolvable -> unresolved, no crash
    assert resolve_entity("zzzptqx no such entity").get("unresolved"), resolve_entity("zzzptqx no such entity")

    # --- research supergraph: builder ---
    g = build_supergraph("Fallen King Centaurus")
    ent = g["entities"][0]
    assert ent["category"] == "raids" and not g["unresolved"], g
    assert ent["facts"].get("hp") and ent["facts"].get("tier") == 10, ent["facts"]
    rels = {r["field"]: r for r in ent["relations"]}
    drops = rels["drops"]
    assert drops["total"] >= 6 and not drops["partial"], drops
    assert all(m["useable_by"] for m in drops["members"]), drops["members"]
    assert all(m["stats"] for m in drops["members"]), "each drop must carry stats"
    bow = next(m for m in drops["members"] if m["name"] == "Cretan Compound Bow")
    assert "attack" in bow["stats"], bow
    assert any("Crit" in e for e in bow["effects"]), bow["effects"]      # gives:T. Crit ↑
    helm = next(m for m in drops["members"] if m["name"] == "Horned Corinthian Helmet")
    assert any("Blind" in e for e in helm["effects"]), helm["effects"]   # immunities:Blind
    assert rels["skills"]["total"] >= 6, rels["skills"]
    # oversized relation -> capped + PARTIAL with the true total
    capped = build_supergraph("Fallen King Centaurus", per_relation_cap=2)
    cdrops = {r["field"]: r for r in capped["entities"][0]["relations"]}["drops"]
    assert cdrops["partial"] and cdrops["total"] >= 6 and len(cdrops["members"]) == 2, cdrops
    # multi-entity bundles both
    two = build_supergraph(["Fallen King Centaurus", "Cretan Compound Bow"])
    assert len(two["entities"]) == 2, two
    # not in the dump -> unresolved, no crash
    miss = build_supergraph("zzzptqx no such entity")
    assert miss["unresolved"] == ["zzzptqx no such entity"] and not miss["entities"], miss
    # malformed edge / missing target must not crash: a dangling id degrades to
    # an empty leaf (display_name titleizes the unknown id, stats/effects empty)
    dangling = _leaf("items", "does-not-exist")
    assert dangling["stats"] == {} and dangling["effects"] == [] and dangling["name"], dangling
    # effect code with no status entry falls back to the raw code
    assert _effect_names({"gives": [{"name": "totally_made_up_code"}]}) == ["gives:totally_made_up_code"]
    assert _effect_names({"immunities": [{"name": "blind"}]}) == ["immunities:Blind"]

    print(f"orna_aussies: all {len(cases)} tier-shorthand self-checks passed")


if __name__ == "__main__":
    _demo()
