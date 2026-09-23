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
from typing import Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)

OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "gpt-oss:20b")
OLLAMA_API_KEY = os.environ.get("OLLAMA_API_KEY")
OLLAMA_TIMEOUT = 60.0


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
        # stray code fence/preamble, or trailing garbage after an
        # otherwise-valid object (seen live in telegram_go.py's identical
        # call pattern: "Extra data" - the model appended more content
        # right after a complete JSON object). raw_decode parses just the
        # first complete object starting at the first "{" and stops there,
        # instead of a greedy regex that would span across the garbage too.
        start = content.find("{")
        if start == -1:
            raise OllamaError(f"Ollama returned non-JSON content: {content[:200]!r}")
        try:
            obj, _ = json.JSONDecoder().raw_decode(content, start)
            return obj
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
    text>"}. For "query", "query" is passed to plan_queries (a
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
        'rarity/tier/useable-by/place. Covers a single simple ask ("what gives '
        'immunity to stunned"), combined ones ("mag > 250 and crit > 3%"), AND a '
        "NAME or set/family fragment combined with a filter - e.g. \"Last Martyr "
        'items for mage" or "Rainsong stuff except weapons" is "query", NOT '
        '"codex", because it is really two things ANDed together (the name/set '
        'text, plus a class/slot/attribute restriction) - "query" is still the '
        'whole request translated to English, unchanged wording (e.g. "last '
        'martyr items for mage"), parsed into a text-on-name condition plus '
        "attribute condition(s) separately. Only a name/set fragment ALONE, with "
        'no other restriction attached, is "codex".\n'
        '"codex": a lookup about one specific item, monster, boss, class, spell, '
        'building, dungeon, or general Orna info by NAME ALONE (no class/slot/'
        'attribute restriction attached) - "query" is the translated English '
        "search text.\n"
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


_CONDITION_KINDS = ("stat", "effect", "text", "attr", "ability")


_CONDITION_RULES = (
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
        "TIER SHORTHAND: a buff/debuff tier is often written as a run of +/- "
        '("T Mag ++"), repeated arrows ("Mag ↑↑↑"), or a plain digit/roman '
        'numeral ("T Mag 2", "Def III") - these are ALWAYS "kind":"effect" '
        "(never \"stat\" - there's no stat named 't_mag' or similar, tier "
        'shorthand is a buff strength, not a stat value). Copy the tier marker '
        'into "value" EXACTLY as the user wrote it (same +/-/arrow count, same '
        'digit) - never translate it into a different notation, invent a tier '
        "number that wasn't there, or drop it. E.g. \"t.mag ++\" -> value "
        '"t.mag ++" verbatim (not "T Mag 3", not "T Mag" with the tier '
        'dropped).\n'
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
        '  {"kind":"attr","field":"<snake_case field name>","cmp":"=|!=|>|<|>=|<=",'
        '"value":"<text, number, or true/false>"} - a flat attribute, not limited '
        'to a fixed list. Use "cmp":"!=" for exclusion language - "not"/"except"/'
        '"excluding"/"other than" - e.g. "helmets and armor for mages, not '
        'weapons" -> a place/item_type condition for armor/head PLUS {"kind":'
        '"attr","field":"item_type","cmp":"!=","value":"weapon"}; "any slot '
        'except weapon" -> {"kind":"attr","field":"place","cmp":"!=","value":'
        '"weapon"}. Infer the field from the record structure Orna data '
        'uses: "tier" (number), "rarity" (common/legendary/godly/arisen/...), '
        '"useable_by" (which classes can equip it - magic_users/melee_classes/'
        'thief_classes/warrior_classes/valhallan_summoner_classes/all_classes - a '
        'partial word like "magic" or "thief" is fine, matched as a substring), '
        '"place" (the body slot an item goes in - head/torso/legs/weapon/'
        "off-hand/accessory/material - use THIS for \"what goes on "
        'legs"/"head slot items"/"accessories", never "type"), "type" (e.g. '
        'weapon SUBTYPE, only meaningful for weapons - daggers/axes_&_hammers/'
        'curved_swords), "item_type" (the broad equipment category - armor/'
        'weapon/off-hand/field/...), "family" (monster '
        'family, e.g. magical/undead), "element" (item elemental type - fire/'
        'water/arcane/...), "events" (which game event, e.g. "which items are '
        'from thronemakers"), "tags" (misc labels like found_in_chests), "price" '
        '(classes only), and boolean flags "exotic"/"new"/"hidden" (value "true" '
        'or "false" - e.g. "what new items were added" -> field "new", value '
        '"true"). Example: rarity="legendary" or tier>=8.\n'
        '  {"kind":"ability","value":"<spell/skill name, or empty>"} - the item '
        "ITSELF grants access to cast a specific spell/skill when equipped - a "
        'completely DIFFERENT thing from "kind":"effect" (a buff/debuff code, e.g. '
        'Mag Up/Stunned/T Def 2 - words like Up/Down/a tier number, or a status '
        'ailment). "gives an extra/bonus/additional spell", "grants a skill", '
        '"gives access to a spell" -> {"kind":"ability","value":""} (empty = has '
        'ANY bonus spell, no specific one named). "what grants/gives Fireball" or '
        '"what weapon grants Crush" -> {"kind":"ability","value":"fireball"} / '
        '"crush" (a spell/skill\'s own NAME, not a buff word, is named - default '
        'to "ability" whenever the named thing could plausibly be a spell rather '
        'than obviously being a stat buff). Never use "kind":"effect" with value '
        '"spell"/"skill"/"ability" literally - there is no buff/debuff by that '
        "name, it will never match anything.\n"
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


def _sanitize_query_block(raw) -> Optional[Dict]:
    """One "queries[]" entry -> a clean {"label","conditions","combinator",
    "category","sort_by","sort_dir"}, or None if it has neither usable
    conditions nor a sort_by (nothing to run)."""
    if not isinstance(raw, dict):
        return None
    raw_conditions = raw.get("conditions")
    conditions = [c for c in raw_conditions if isinstance(c, dict) and c.get("kind") in _CONDITION_KINDS] \
        if isinstance(raw_conditions, list) else []
    sort_by = str(raw.get("sort_by") or "").strip()
    if not conditions and not sort_by:
        return None
    combinator = raw.get("combinator") if raw.get("combinator") in ("and", "or") else "and"
    category = raw.get("category") if raw.get("category") in _ORNA_CATEGORIES else ""
    sort_dir = raw.get("sort_dir") if raw.get("sort_dir") in ("asc", "desc") else "desc"
    label = str(raw.get("label") or "").strip()
    return {"label": label, "conditions": conditions, "combinator": combinator,
            "category": category, "sort_by": sort_by, "sort_dir": sort_dir}


async def plan_queries(text: str, clarified: bool = False) -> Dict:
    """
    Turn an already-English "query"-intent request into one or more
    structured searches for orna_aussies.query_records, optionally asking
    a clarifying question first - the multi-query and clarification
    counterpart to the old single-query parse_conditions.

    Returns {"needs_clarification": bool, "question": str, "options":
    [...], "queries": [<query block>, ...]}. A query block is
    {"label", "conditions", "combinator", "category", "sort_by",
    "sort_dir"} - see _CONDITION_RULES for what a condition/sort_by/
    category/combinator can be; identical rules to the old single-query
    schema, just nested one level so several independent searches (e.g.
    "best mag item for thieves and for mages" -> one block per class)
    can ride in a single reply.

    When needs_clarification is True, "queries" is empty and the caller
    should ask "question" via 2-4 tappable "options" (button-only, same
    reasoning as /go's "ask" action - a local model's clarifying
    questions are themselves unreliable enough that free-text follow-up
    would just compound the uncertainty) rather than run anything yet.
    Pass clarified=True on the follow-up call (after the user picked an
    option) to forbid asking again - mirrors /go's "don't ask more than
    once" rule, so this can never loop.

    Never returns a totally empty "queries" with needs_clarification
    False too (falls back to one {"kind":"text"} block on the raw text)
    - a genuinely-asked query shouldn't just dead-end silently.
    """
    fallback = {"needs_clarification": False, "question": "", "options": [], "queries": [
        {"label": "", "conditions": [{"kind": "text", "field": "", "value": text}],
         "combinator": "and", "category": "", "sort_by": "", "sort_dir": "desc"},
    ]}

    clarify_rules = (
        'Set "needs_clarification": true (with "question" and 2-4 short '
        '"options") ONLY when a specific missing detail would materially change '
        'which items come back and you\'d otherwise have to guess it - e.g. '
        '"good gear for my class" names no class, "strongest weapon" with no '
        "stat in sight and several equally-plausible readings (attack? magic?). "
        "Most requests do NOT need this - don't ask when a reasonable default "
        'exists or the request is already clear ("legendary items", "what '
        'cures poisoned", "mag > 250" all need zero clarification). Never ask '
        "about anything already answered elsewhere in the request. The user "
        "can only tap one of your option buttons, never type a free-text "
        "reply, so options must be short, concrete, and self-sufficient.\n"
        if not clarified else
        "The user has already been asked one clarifying question this turn and "
        'picked an answer (folded into the request below) - "needs_clarification" '
        "MUST be false now; commit to your best-effort reading and always return "
        "at least one query block.\n"
    )
    multi_rules = (
        '"queries" normally has exactly ONE block. Use MORE than one only when '
        "the request explicitly names several separate things to look up side "
        'by side that don\'t collapse into one AND/OR filter - e.g. "best mag '
        'item for thieves and for mages" -> two blocks (one per class, each '
        'with its own useable_by condition + sort_by "magic"), "top attack '
        'weapon and top defense armor" -> two blocks (different stats/slots). '
        'Give each block a short "label" naming what makes it distinct (e.g. '
        '"Thief", "Mage", "Top Attack Weapon") - empty label is fine for a '
        "single-block reply. A request that's naturally one combined filter "
        '(e.g. "mag > 250 and crit > 3%") stays ONE block - don\'t split an '
        'AND/OR into multiple blocks.\n'
    )
    system = (
        "Parse an Orna RPG database search. Reply with strict JSON: "
        '{"needs_clarification": bool, "question": "<question or empty>", '
        '"options": ["<opt1>", "<opt2>", ...], "queries": [<query block>, ...]}.\n'
        + clarify_rules + multi_rules +
        "Each query block is {\"conditions\": [...], \"combinator\": \"and\"|\"or\", "
        '"category": "<one of items, monsters, bosses, raids, followers, classes, '
        'spells, buildings, dungeons, or empty>", "sort_by": "<stat field or '
        'empty>", "sort_dir": "asc"|"desc", "label": "<short name or empty>"}.\n'
        + _CONDITION_RULES
    )
    try:
        data = await _chat_json(system, text)
    except OllamaError:
        return fallback

    if data.get("needs_clarification") and not clarified:
        options = [str(o).strip() for o in (data.get("options") or []) if str(o).strip()][:4]
        question = str(data.get("question") or "").strip()
        if question and options:
            return {"needs_clarification": True, "question": question, "options": options, "queries": []}

    raw_queries = data.get("queries")
    queries = [b for b in (_sanitize_query_block(q) for q in raw_queries) if b] \
        if isinstance(raw_queries, list) else []
    if not queries:
        return fallback
    return {"needs_clarification": False, "question": "", "options": [], "queries": queries}


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
