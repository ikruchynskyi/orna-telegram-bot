"""
telegram_nlp.py
================
Natural-language helpers backed by a local Ollama instance (gpt-oss:20b by
default), so the bot can understand free-text resource requests instead of
only slash commands.

Uses Ollama's REST API directly over httpx (no extra pip dependency) with
format="json" to force well-formed structured output.
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Dict, List

import httpx

logger = logging.getLogger(__name__)

OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "gpt-oss:20b")
OLLAMA_API_KEY = os.environ.get("OLLAMA_API_KEY")
OLLAMA_TIMEOUT = 60.0

_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.S)


class OllamaError(RuntimeError):
    """Raised when the local Ollama server can't be reached or replies unusably."""


async def _chat_json(system: str, user: str, retries: int = 1) -> dict:
    try:
        return await _chat_json_once(system, user)
    except OllamaError:
        if retries <= 0:
            raise
        return await _chat_json(system, user, retries - 1)


async def _chat_json_once(system: str, user: str) -> dict:
    payload = {
        "model": OLLAMA_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "stream": False,
        "format": "json",
        # Verified empirically (2026-09-22): "think": False makes this
        # model/quantization return empty or truncated-mid-reasoning
        # content instead of the requested JSON, near 100% of the time in
        # testing - the exact flakiness this module's docstring already
        # warned about. "think": True is completely reliable in the same
        # tests (Ollama separates the reasoning out on its own, content
        # comes back as clean JSON) at the cost of a bit more latency.
        "think": True,
    }
    headers = {"Authorization": f"Bearer {OLLAMA_API_KEY}"} if OLLAMA_API_KEY else {}
    try:
        async with httpx.AsyncClient(timeout=OLLAMA_TIMEOUT) as client:
            response = await client.post(
                f"{OLLAMA_HOST}/api/chat", json=payload, headers=headers
            )
            response.raise_for_status()
            data = response.json()
    except httpx.HTTPError as e:
        raise OllamaError(f"Ollama request failed: {e}") from e

    content = data.get("message", {}).get("content", "")
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        # format="json" should guarantee valid JSON, but be tolerant of a
        # stray code fence or preamble anyway.
        m = _JSON_OBJECT_RE.search(content)
        if not m:
            raise OllamaError(f"Ollama returned non-JSON content: {content[:200]!r}")
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError as e:
            raise OllamaError(f"Ollama returned malformed JSON: {content[:200]!r}") from e


async def extract_resources(text: str, known: List[str]) -> List[str]:
    """
    Ask the model which materials from `known` the free-text `text` refers to.

    Returns canonical names (exact spelling from `known`), de-duplicated, in
    the order the model listed them. Empty list if none matched.
    Raises OllamaError if the model couldn't be reached at all.
    """
    system = (
        "You identify which Orna RPG crafting materials a player is asking "
        "about. You are given a fixed list of valid material names and a "
        "free-text message (English or Ukrainian, possibly with typos or "
        "abbreviations). Reply with strict JSON of the form "
        '{"resources": ["Exact Name From List", ...]}. '
        "Only include names that are in the list, spelled exactly as given "
        "there. Never invent a name that isn't in the list. If the message "
        'doesn\'t clearly ask about materials from the list, reply '
        '{"resources": []}.'
    )
    user = "Material list:\n" + ", ".join(known) + f"\n\nMessage:\n{text}"
    data = await _chat_json(system, user)
    raw = data.get("resources")
    if not isinstance(raw, list):
        return []
    lookup = {k.lower(): k for k in known}
    out: List[str] = []
    seen = set()
    for item in raw:
        canon = lookup.get(str(item).strip().lower())
        if canon and canon not in seen:
            seen.add(canon)
            out.append(canon)
    return out


_ORNA_CATEGORIES = (
    "items", "monsters", "bosses", "raids", "followers",
    "classes", "spells", "buildings", "dungeons",
)


async def route_query(text: str) -> Dict[str, str]:
    """
    Classify a free-text /orna message (English or Ukrainian) into one of
    four intents, translating to English along the way in one round trip.

    Returns {"intent": "today"|"next"|"codex"|"query", "query": "<English
    text>"}. For "query", "query" is passed to parse_conditions (a
    separate call - keeps each prompt's schema simple rather than one
    mega-prompt doing classification and structured condition extraction
    at once, which proved less reliable during development).

    Falls back to {"intent": "codex", "query": text} if the model can't be
    reached - codex search's own "no results" reply is a safer default
    than silently failing the whole command.
    """
    system = (
        'You route a Telegram message about the mobile RPG "Orna" (English or '
        'Ukrainian) into exactly one intent. Reply with strict JSON: {"intent": '
        '"today"|"next"|"codex"|"query", "query": "<English text>"}.\n'
        '"today": asks what crafting materials/resources are available today, no '
        'specific material named. "query" empty.\n'
        '"next": asks about a SPECIFIC named crafting material and when/where it '
        'becomes available - "query" is just that material\'s name, translated to '
        "English.\n"
        '"query": asks which items/monsters/etc. match one or more criteria - a '
        "game effect (immunity to a status, causes a status on a target, grants a "
        'stat buff/debuff), a stat threshold (e.g. "magic over 250", "crit above '
        '3%"), text that should appear in the description, or an attribute like '
        'rarity/tier/useable-by. Covers both a single simple ask ("what gives '
        'immunity to stunned") and combined ones ("mag > 250 and crit > 3%"). '
        '"query" is the request translated to English, otherwise unchanged - exact '
        "wording matters, it gets parsed into structured conditions separately.\n"
        '"codex": anything else - a lookup about one specific item, monster, boss, '
        'class, spell, building, dungeon, or general Orna info by name - "query" '
        "is the translated English search text.\n"
        'Always translate Ukrainian in "query" to English. For "today", "query" '
        "can be empty."
    )
    try:
        data = await _chat_json(system, text)
    except OllamaError:
        return {"intent": "codex", "query": text}
    intent = data.get("intent") if data.get("intent") in ("today", "next", "codex", "query") else "codex"
    query = str(data.get("query") or text).strip()
    return {"intent": intent, "query": query}


_CONDITION_KINDS = ("stat", "effect", "text", "attr")


async def parse_conditions(text: str) -> Dict:
    """
    Turn an already-English "query"-intent request into structured search
    conditions for orna_aussies.query_records. A separate, focused call
    from route_query (see its docstring for why) - this one's whole job
    is producing a list of:
      {"kind":"stat","field":"<snake_case stat name, any real Orna stat -
       not just the common ones, e.g. follower_stats, crit_damage>",
       "cmp":">|<|>=|<=|=","value":<number, may be negative>}
      {"kind":"effect","field":"immunities|causes|gives|","value":"<text,
       e.g. 'stunned' or 'T Mag 3'>"}
      {"kind":"text","field":"description|name|","value":"<substring>"}
      {"kind":"attr","field":"<tier|rarity|useable_by|place|type|
       item_type|family|element>","cmp":"=|>|<|>=|<=","value":"<text or number>"}

    Returns {"conditions": [...], "combinator": "and"|"or", "category":
    "<one of _ORNA_CATEGORIES or empty>", "sort_by": "<stat field or
    empty>", "sort_dir": "asc"|"desc"}. Never returns an empty conditions
    list with no sort_by either (orna_aussies.query_records treats that
    as "nothing matches") - falls back to one {"kind":"text"} condition
    on the raw text if the model can't be reached or returns nothing
    usable, so a genuinely-asked query doesn't just dead-end silently.
    """
    system = (
        "Parse an Orna RPG database search into structured conditions. Reply with "
        'strict JSON: {"conditions": [...], "combinator": "and"|"or", "category": '
        '"<one of items, monsters, bosses, raids, followers, classes, spells, '
        'buildings, dungeons, or empty>", "sort_by": "<stat field or empty>", '
        '"sort_dir": "asc"|"desc"}.\n'
        "Each condition is one of:\n"
        '  {"kind":"stat","field":"<snake_case stat name>","cmp":">|<|>=|<=|=",'
        '"value":<number, may be negative>} - a numeric stat threshold, e.g. '
        '"magic > 250", "crit chance above 3%" (strip the % sign, value is just '
        "the number). field is NOT limited to a fixed list - Orna items have "
        "dozens of stats beyond the obvious ones (hp, mana, attack, magic, "
        "defense, resistance, dexterity, ward, foresight, crit, crit_chance, "
        "crit_damage, follower_stats, summon_stats, view_distance, gold_bonus, "
        "exp_bonus, healing, life_siphon, dodge_chance, accuracy, ...) - infer the "
        "best snake_case field name straight from the user's wording (e.g. "
        '"follower stats" -> "follower_stats", "summon stats" -> "summon_stats").\n'
        '  IMPORTANT crit distinction: "crit" or "crit chance" -> field "crit" or '
        '"crit_chance" (how often you crit). "crit damage" -> field "crit_damage" '
        "(a SEPARATE stat - how much extra damage a crit does). Never conflate the "
        "two.\n"
        '  Negative values: a stat CAN be asked as negative, e.g. "items with '
        'negative defense" or "defense below 0" -> {"kind":"stat","field":'
        '"defense","cmp":"<","value":0}. But "what lowers defense" / "what '
        'reduces attack" (asking for a DEBUFF effect, not an item\'s own stat) is '
        'usually "kind":"effect" instead - see below.\n'
        '  {"kind":"effect","field":"immunities|causes|gives|cures|","value":'
        '"<effect text>"} - immunity to / causes / grants / cures a NAMED status or '
        'buff/debuff (e.g. value "stunned", "T Mag 3", or "Def Down" - a specific '
        "effect name, never a bare number). field: \"immunities\" for \"immune\"/"
        '"resistant to", "causes" for inflicts-on-enemy (e.g. "what lowers enemy '
        'defense" -> value "Def Down", field "causes"), "gives" for grants/self-or-'
        'team buffs, "cures" for "what cures poisoned"/"removes stun", empty if '
        "unclear.\n"
        "IMPORTANT: if the request has a NUMBER/threshold on a stat (magic, "
        'attack, crit damage, follower stats, etc.) it is ALWAYS "kind":"stat", '
        'even if the wording uses "gives"/"has"/"with" - e.g. "what gives magic '
        'over 220" and "items with magic > 220" are BOTH {"kind":"stat","field":'
        '"magic","cmp":">","value":220}, NOT an effect lookup for a "Mag Up" buff. '
        'Reserve "kind":"effect" for when the request names an actual status/buff '
        "by name (stunned, poisoned, Mag Up, T. Att Down, ...) with no numeric "
        "stat threshold attached.\n"
        '  {"kind":"text","field":"description|name|","value":"<substring>"} - the '
        "name or description should contain this text.\n"
        '  {"kind":"attr","field":"<snake_case field name>","cmp":"=|>|<|>=|<=",'
        '"value":"<text, number, or true/false>"} - a flat attribute, not limited '
        "to a fixed list - infer the field from the record structure Orna data "
        'uses: "tier" (number), "rarity" (common/legendary/godly/arisen/...), '
        '"useable_by" (which classes), "place" (where found), "type" (e.g. '
        'weapon subtype - daggers/axes_&_hammers/curved_swords), "item_type" (the '
        'equipment SLOT - armor/weapon/off-hand/field/...), "family" (monster '
        'family, e.g. magical/undead), "element" (item elemental type - fire/'
        'water/arcane/...), "events" (which game event, e.g. "which items are '
        'from thronemakers"), "tags" (misc labels like found_in_chests), "price" '
        '(classes only), and boolean flags "exotic"/"new"/"hidden" (value "true" '
        'or "false" - e.g. "what new items were added" -> field "new", value '
        '"true"). Example: rarity="legendary" or tier>=8.\n'
        '"combinator": "and" if ALL conditions must hold, "or" if ANY - default '
        '"and" unless the user clearly says "or"/"either".\n'
        '"category": set only if the user named a specific category (e.g. "which '
        'spells..." -> "spells"), else empty to search everything.\n'
        '"sort_by"/"sort_dir": set these (instead of, or together with, '
        "conditions) when the user asks for a RANKING rather than a plain filter - "
        '"the item with the biggest mag", "most powerful mag item", "weakest '
        'defense follower", "top crit damage weapon". sort_by is the stat field '
        "(same naming rules as a stat condition's field - any real Orna stat, "
        'e.g. "magic", "crit_damage", "follower_stats"). sort_dir is "desc" for '
        'biggest/highest/most/best/strongest, "asc" for smallest/lowest/least/'
        'worst/weakest. conditions can be EMPTY when the ask is pure ranking with '
        'no other filter (e.g. "item with the biggest mag" -> conditions: [], '
        'sort_by: "magic", sort_dir: "desc") - only add a condition too if the '
        'user also gave an explicit filter (e.g. "best mag item that also gives T '
        'Mag 3" -> one effect condition PLUS sort_by "magic"). Leave sort_by empty '
        "for a plain filter query with no ranking language.\n"
        'Example: "mag > 250 and crit > 3%" -> conditions: '
        '[{"kind":"stat","field":"magic","cmp":">","value":250},'
        '{"kind":"stat","field":"crit","cmp":">","value":3}], combinator: "and".\n'
        'Example: "what is the item with the biggest mag" -> conditions: [], '
        'sort_by: "magic", sort_dir: "desc".'
    )
    fallback = {"conditions": [{"kind": "text", "field": "", "value": text}], "combinator": "and",
                "category": "", "sort_by": "", "sort_dir": "desc"}
    try:
        data = await _chat_json(system, text)
    except OllamaError:
        return fallback

    raw_conditions = data.get("conditions")
    conditions = [c for c in raw_conditions if isinstance(c, dict) and c.get("kind") in _CONDITION_KINDS] \
        if isinstance(raw_conditions, list) else []
    sort_by = str(data.get("sort_by") or "").strip()
    if not conditions and not sort_by:
        return fallback

    combinator = data.get("combinator") if data.get("combinator") in ("and", "or") else "and"
    category = data.get("category") if data.get("category") in _ORNA_CATEGORIES else ""
    sort_dir = data.get("sort_dir") if data.get("sort_dir") in ("asc", "desc") else "desc"
    return {"conditions": conditions, "combinator": combinator, "category": category,
            "sort_by": sort_by, "sort_dir": sort_dir}


async def extract_quantities(text: str, resources: List[str]) -> Dict[str, int]:
    """
    Ask the model how many of each of `resources` the user's reply specifies.

    Returns {name: qty} only for names the user actually gave a positive
    number for; names not mentioned are omitted (caller should re-ask about
    those). Raises OllamaError if the model couldn't be reached.
    """
    system = (
        "You extract requested quantities of Orna RPG materials from a "
        "player's reply. You are given the list of material names being "
        "asked about and the player's free-text reply (English or "
        "Ukrainian). Reply with strict JSON of the form "
        '{"quantities": {"Exact Name From List": <positive integer>, ...}}. '
        "Only include a material if the reply gives a clear number for it; "
        "omit materials the reply doesn't mention. Never invent names not "
        "in the list."
    )
    user = "Materials:\n" + ", ".join(resources) + f"\n\nReply:\n{text}"
    data = await _chat_json(system, user)
    raw = data.get("quantities")
    if not isinstance(raw, dict):
        return {}
    lookup = {k.lower(): k for k in resources}
    out: Dict[str, int] = {}
    for k, v in raw.items():
        canon = lookup.get(str(k).strip().lower())
        if not canon:
            continue
        try:
            qty = int(round(float(v)))
        except (TypeError, ValueError):
            continue
        if qty > 0:
            out[canon] = qty
    return out
