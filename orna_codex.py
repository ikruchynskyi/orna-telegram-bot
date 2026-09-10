"""
orna_codex.py - Search and parse playorna.com codex pages
==========================================================

Given a free-text item name, search the codex, take the first result,
fetch its page, and parse out the base stats + flags into a CodexEntry
that can be fed to orna_assess.

Network: uses `requests`. Pages are cached in-process with lru_cache.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Dict, List, Optional, Tuple
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from orna_assess import CodexEntry


CODEX_BASE = "https://playorna.com"
SEARCH_URL = f"{CODEX_BASE}/codex/items/"
USER_AGENT = "OrnaAssessAPI/1.0 (compatible; codex-lookup)"
HTTP_TIMEOUT = 10.0


# -----------------------------------------------------------------------------
# Stat label mapping
# -----------------------------------------------------------------------------
# Codex pages display human labels like "Defense:" or "Adornment Slots:".
# Map them (lowercased, no trailing colon) to the internal keys used by
# orna_assess.

STAT_LABEL_MAP: Dict[str, str] = {
    # ---- English ----
    "hp": "hp",
    "mana": "mana",
    "attack": "attack",
    "magic": "magic",
    "defense": "defense",
    "resistance": "resistance",
    "dexterity": "dexterity",
    "ward": "ward",
    "crit": "crit",
    "foresight": "foresight",
    "adornment slots": "adornment_slots",
    "orn bonus": "orn_bonus",
    "exp bonus": "exp_bonus",
    "experience bonus": "exp_bonus",
    "luck bonus": "luck_bonus",
    "gold bonus": "gold_bonus",
    "monster encounters": "monster_encounters",
    "mana flask power": "manaflask_power",
    "manaflask power": "manaflask_power",
    "apex": "apex",
    "no follower bonus": "no_follower_bonus",
    "bestial bond": "bestial_bond",
    "act first chance (pve)": "act_first_chance__pve_",
    "swap defense and resistance": "swap_defense_resistance",

    # ---- Ukrainian (?lang=uk) ----
    # Core stats
    "хр": "hp",
    "оз": "hp",
    "атака": "attack",
    "магія": "magic",
    "захист": "defense",
    "опір": "resistance",
    "спритність": "dexterity",
    "вард": "ward",
    "крит": "crit",
    "ініціатива": "foresight",
    "слоти для прикрас": "adornment_slots",
    # Bonuses (best-effort - extend as needed)
    "бонус орн": "orn_bonus",
    "бонус досвіду": "exp_bonus",
    "бонус удачі": "luck_bonus",
    "бонус золота": "gold_bonus",
}

# Lines whose label starts with one of these are non-numeric flavour text and
# we skip them entirely.
SKIP_LABEL_PREFIXES: Tuple[str, ...] = (
    # English
    "+follower/summon spell", "+spell", "+ability",
    "follower/summon spell", "spell", "ability",
    "immunity", "immunities",
    "useable by", "place", "tier", "rarity", "type",
    # Ukrainian metadata labels
    "+заклинання",  # +Spell
    "імунітет",     # Immunity
    "доступно",     # Useable by
    "тип",          # Type / Place (Ukrainian uses "Тип:" for both)
    "ранг",         # Tier
    "рідкість",     # Rarity
)


# -----------------------------------------------------------------------------
# Item-name prefix knowledge
# -----------------------------------------------------------------------------
# Inventory item names are formatted as
#   [Quality] [Enchantment] <CodexName>
# We need to strip the Quality + Enchantment words before searching so the
# codex actually finds the base item.

QUALITY_PREFIXES: frozenset = frozenset({
    # English quality tiers (also used as in-game name prefix)
    "broken", "poor", "regular", "superior", "famed", "legendary",
    "ornate", "masterforged", "demonforged", "godforged",
    # Special enchant
    "anguished",
    # Ukrainian quality tiers — verified: "Легендарне" (Legendary).
    # The rest are best-effort translations; unknown forms still get
    # caught by the OCR-tolerant matcher below (_looks_like_quality).
    "якісне", "якісний", "якісна",       # Famed (?)
    "легендарне", "легендарний", "легендарна",  # Legendary
    "прикрашене", "прикрашений", "прикрашена",  # Ornate
    "майстерне", "майстерний", "майстерна",     # Masterforged
    "демонічне", "демонічний", "демонічна",     # Demonforged
    "божественне", "божественний", "божественна",  # Godforged
})


# OCR mangles the brackets around the tier suffix in Ukrainian items, e.g.
#   "Вбрання льосальфарів [Легендарне]" -> "Вбрання льосальфарів ІЛегендарне!"
# Recognise such tokens leniently: any Cyrillic word ending with one of the
# quality-tier suffixes is treated as a quality marker, even when the leading
# bracket got eaten or the trailing punctuation is wrong.
_UKR_QUALITY_SUFFIXES: Tuple[str, ...] = (
    "якісне", "якісний", "якісна",
    "легендарне", "легендарний", "легендарна",
    "прикрашене", "прикрашений", "прикрашена",
    "майстерне", "майстерний", "майстерна",
    "демонічне", "демонічний", "демонічна",
    "божественне", "божественний", "божественна",
)


def _looks_like_quality(token: str) -> bool:
    """Tolerant check: does this token look like a Ukrainian quality tier?

    Strips OCR garbage (leading non-Cyrillic chars from mangled brackets,
    trailing punctuation), lowercases, and matches against known suffixes.
    """
    # Drop leading non-letter chars (e.g. 'І' substituted for '[')
    stripped = token.lstrip("[(І!|/\\")
    # Strip trailing punctuation/garbage
    stripped = stripped.rstrip("])}!.,;:'\"|/\\")
    lower = stripped.lower()
    if lower in QUALITY_PREFIXES:
        return True
    # Tolerant match for OCR-mangled forms: leading "І" misread for "[",
    # or stretched/glued tokens
    return any(lower.endswith(s) for s in _UKR_QUALITY_SUFFIXES)


# Match a token that's wrapped in parens or brackets, possibly with
# adjacent punctuation. Examples:  "(L)" "[X]" "(loaned)" "(L)."
_PAREN_TOKEN_RE = re.compile(r"^[\[(].*[\])][^\w]*$|^[\[(]\w+$|^\w+[\])][^\w]*$")


def _strip_paren_suffix_tokens(words: List[str]) -> List[str]:
    """Drop trailing tokens that are entirely or partially parenthesized.

    These mark the player's instance state (locked '(L)', loaned, soulbound,
    etc) and aren't part of the codex name. Examples that get stripped:
        "Eastern Regalia (L)"        -> "Eastern Regalia"
        "Sword of Foo [X]"           -> "Sword of Foo"
        "Loaned Bow (loaned)"        -> "Loaned Bow"
    """
    while words and _PAREN_TOKEN_RE.match(words[-1]):
        words = words[:-1]
    return list(words)

# Enchantment-style adjective prefixes. List is intentionally generous —
# unknown words still get stripped by the iterative-fallback search below.
ENCHANTMENT_PREFIXES: frozenset = frozenset({
    # User-supplied
    "electric", "natural", "organic", "thunderous", "flaming",
    "snowy", "chilling", "warm", "rocky", "sparking",
    # Common Orna elemental enchantments
    "frosty", "icy", "burning", "fiery", "charred",
    "earthen", "sandy", "mossy", "verdant", "fertile",
    "watery", "misty", "stormy", "stony",
})


def _name_candidates(name: str) -> List[str]:
    """
    Generate a search-priority list of name variants for the codex.

    Inventory item names follow the format `[Quality] [Enchantment] BaseName`.
    Strip only the prefixes we *recognise*; otherwise keep the name intact
    (so base names like "Beguiled Axe X" — where "Beguiled" looks like an
    enchantment but is actually part of the codex name — are searched as-is).

    Strategy order:
      1. Recognised quality+enchantment stripped     (most common case)
      2. Quality recognised but 2nd word unknown:
         try dropping 2 (per user's heuristic) AS WELL    (rare)
      3. Original name                                (final fallback)
    """
    words = name.split()
    if not words:
        return []

    out: List[str] = []
    seen = set()

    def add(candidate: str) -> None:
        candidate = candidate.strip()
        if candidate and candidate not in seen:
            seen.add(candidate)
            out.append(candidate)

    # 0. Strip parenthetical / bracketed suffix tokens like "(L)", "[X]",
    #    "(loaned)" — these annotate the player's instance (locked, loaned,
    #    soulbound, etc) and aren't part of the codex name.
    stripped_words = _strip_paren_suffix_tokens(words)
    if not stripped_words:
        return [name]
    paren_stripped = " ".join(stripped_words)
    paren_was_stripped = paren_stripped != name

    # All subsequent steps operate on the paren-stripped form.
    words = stripped_words
    n = len(words)
    first_lower = words[0].lower()
    second_lower = words[1].lower() if n >= 2 else ""

    # 1. Strip whatever leading prefixes we recognise (quality, then enchantment).
    s = list(words)
    if s and s[0].lower() in QUALITY_PREFIXES:
        s.pop(0)
    if s and s[0].lower() in ENCHANTMENT_PREFIXES:
        s.pop(0)
    if len(s) < n:
        add(" ".join(s))

    # 1b. Strip TRAILING quality token (Ukrainian convention: quality goes
    #     after the name, often in brackets that OCR mangles, e.g.
    #     "Вбрання льосальфарів Легендарне" or "...ІЛегендарне!").
    s = list(words)
    while s and _looks_like_quality(s[-1]):
        s.pop()
    if s and len(s) < n:
        add(" ".join(s))

    # 2. Quality was recognised but the second word wasn't a known enchantment —
    #    it might be an enchantment we don't have on file. Per the user's
    #    heuristic ("if first is quality, drop two"), also try dropping the 2nd
    #    word. This kicks in only after the conservative strip above, so for
    #    items with a known enchantment we don't muddy the search.
    if (
        first_lower in QUALITY_PREFIXES
        and second_lower not in ENCHANTMENT_PREFIXES
        and n >= 3
    ):
        add(" ".join(words[2:]))

    # 3. Paren-stripped form (if not already added): handles names like
    #    "Eastern Regalia (L)" where no other prefix-stripping applies.
    if paren_was_stripped:
        add(paren_stripped)

    # 4. Original name as the final fallback.
    add(name)
    return out


# -----------------------------------------------------------------------------
# Parsing helpers
# -----------------------------------------------------------------------------

def _normalize_label(s: str) -> str:
    """'Adornment Slots:' -> 'adornment slots'."""
    return s.strip().rstrip(":").strip().lower()


_NUM_RE = re.compile(r"-?\d+(?:\.\d+)?")


def _parse_stat_value(raw: str) -> Optional[float]:
    """Parse '130', '+5', '2%', '-10' -> 130, 5, 2, -10. None on failure."""
    s = raw.strip().rstrip("%").strip().lstrip("+")
    try:
        return float(s)
    except ValueError:
        m = _NUM_RE.search(raw)
        if m:
            try:
                return float(m.group(0))
            except ValueError:
                return None
    return None


def _http_get(url: str, params: Optional[Dict[str, str]] = None) -> str:
    resp = requests.get(
        url,
        params=params,
        headers={"User-Agent": USER_AGENT},
        timeout=HTTP_TIMEOUT,
        allow_redirects=True,
    )
    resp.raise_for_status()
    return resp.text


# -----------------------------------------------------------------------------
# Search
# -----------------------------------------------------------------------------

# Skip these query-string params on item links (language switchers etc).
_SKIP_QUERY_TOKENS = ("?lang=",)


def _is_item_detail_href(href: str) -> bool:
    """Does this href point at /codex/items/<slug>/?"""
    if not href:
        return False
    # Strip any query string for the path test
    path = href.split("?", 1)[0]
    # Normalise to leading-slash path
    if path.startswith(CODEX_BASE):
        path = path[len(CODEX_BASE):]
    if not path.startswith("/codex/items/"):
        return False
    rest = path[len("/codex/items/"):].strip("/")
    # rest must be a non-empty single slug
    return bool(rest) and "/" not in rest


@lru_cache(maxsize=512)
def search_first_url(name: str, lang: str = "en") -> Optional[str]:
    """Search the codex; return the absolute URL of the first item hit, or None."""
    params = {"q": name}
    if lang and lang != "en":
        params["lang"] = lang
    html = _http_get(SEARCH_URL, params=params)
    soup = BeautifulSoup(html, "html.parser")

    for a in soup.find_all("a", href=True):
        href = a["href"]
        if any(tok in href for tok in _SKIP_QUERY_TOKENS):
            continue
        if _is_item_detail_href(href):
            return urljoin(CODEX_BASE, href)
    return None


# -----------------------------------------------------------------------------
# Parsing a single item page
# -----------------------------------------------------------------------------

@dataclass
class _PageMeta:
    """Metadata harvested from labels that aren't stats."""
    place: str = ""
    type_str: str = ""
    tier: str = ""
    rarity: str = ""
    is_celestial: bool = False  # rarity == "Celestial"
    is_exotic: bool = False     # rarity == "Exotic" (rare exotic class)
    is_two_handed: bool = False # standalone "Two handed" / "Two-handed" line


# Standalone flag lines on item pages — they have no colon, so they're not stats.
# Match as anchored words to avoid false positives in flavour text.
# Ukrainian: Дворучний (masc) / Дворучна (fem) / Дворучне (neut).
_TWO_HANDED_RE = re.compile(
    r"(?mi)^\s*(?:two[- ]handed|дворучн(?:ий|а|е))\s*$"
)


def _harvest_meta(soup: BeautifulSoup) -> _PageMeta:
    """Pull Place/Type/Tier/Rarity labels and standalone flag lines.

    Handles both English ("Place:" / "Type:" / "Tier:" / "Rarity:") and
    Ukrainian ("Тип:" twice — first=Place, second=Type — / "Ранг:" / "Рідкість:").
    """
    meta = _PageMeta()
    page_text = soup.get_text(separator="\n")

    # English labels
    en_patterns = {
        "place":    r"Place\s*:\s*([^\n]+)",
        "type_str": r"Type\s*:\s*([^\n]+)",
        "tier":     r"Tier\s*:\s*([^\n]+)",
        "rarity":   r"Rarity\s*:\s*([^\n]+)",
    }
    for attr, pat in en_patterns.items():
        m = re.search(pat, page_text, re.IGNORECASE)
        if m:
            setattr(meta, attr, m.group(1).strip())

    # Ukrainian: "Ранг:" -> tier, "Рідкість:" -> rarity
    if not meta.tier:
        m = re.search(r"Ранг\s*:\s*([^\n]+)", page_text)
        if m:
            meta.tier = m.group(1).strip()
    if not meta.rarity:
        m = re.search(r"Рідкість\s*:\s*([^\n]+)", page_text)
        if m:
            meta.rarity = m.group(1).strip()

    # Ukrainian: "Тип:" appears twice on weapon pages -
    #   1st occurrence == English "Place:"
    #   2nd occurrence == English "Type:"
    if not meta.place or not meta.type_str:
        tip_matches = re.findall(r"Тип\s*:\s*([^\n]+)", page_text)
        if tip_matches:
            if not meta.place:
                meta.place = tip_matches[0].strip()
            if not meta.type_str and len(tip_matches) >= 2:
                meta.type_str = tip_matches[1].strip()

    # Rarity-based detection. The Ukrainian codex actually keeps "Celestial"
    # in English in the Rarity field (lucky us).
    rarity_lower = meta.rarity.lower()
    meta.is_celestial = rarity_lower == "celestial"
    meta.is_exotic = rarity_lower == "exotic"

    meta.is_two_handed = bool(_TWO_HANDED_RE.search(page_text))
    return meta


def parse_codex_html(html: str) -> CodexEntry:
    """
    Parse a codex item page's HTML into a CodexEntry.

    - Stats come from <div class="codex-stat"> blocks.
    - Flags are inferred from the Place / Type metadata + page text.
    """
    soup = BeautifulSoup(html, "html.parser")

    # Name from the <h1>
    name_tag = soup.find("h1")
    name = name_tag.get_text(strip=True) if name_tag else ""

    # Stats
    stats: Dict[str, float] = {}
    for div in soup.select("div.codex-stat"):
        text = div.get_text(separator=" ", strip=True)
        if ":" not in text:
            continue
        label, _, value = text.partition(":")
        norm = _normalize_label(label)
        if any(norm.startswith(p) for p in SKIP_LABEL_PREFIXES):
            continue
        key = STAT_LABEL_MAP.get(norm)
        if key is None:
            # Unrecognised stat label - log via name and skip.  Adding more
            # mappings to STAT_LABEL_MAP is the fix.
            continue
        v = _parse_stat_value(value)
        if v is None:
            continue
        # adornment_slots is conceptually an int
        stats[key] = int(v) if key == "adornment_slots" else v

    # Flags from metadata
    meta = _harvest_meta(soup)
    place_lower = meta.place.lower()
    type_lower = meta.type_str.lower()

    # Two-handed: prefer the standalone tag ("Two handed" / "Дворучний"),
    # fall back to a place like "Two-handed weapon" if some item type uses it.
    is_two_handed = (
        meta.is_two_handed
        or "two-handed" in place_lower
        or "two handed" in place_lower
    )
    is_accessory = any(
        kw in place_lower
        for kw in (
            "accessory", "neck", "ring", "earring", "amulet",
            # Ukrainian
            "аксесуар", "шия", "кільце", "сережка", "амулет",
        )
    )
    is_adornment = (
        "adornment" in type_lower or "adornment" in place_lower
        or "прикрас" in type_lower or "прикрас" in place_lower
    )

    # Celestial weapons have Rarity: Celestial AND a weapon-like Place
    # (Place is "Weapon" / "Зброя" for celestials).
    is_weapon_like = (
        "weapon" in place_lower
        or "hand" in place_lower
        or "зброя" in place_lower         # UA: weapon
        or "рука" in place_lower          # UA: hand
        or place_lower in {"right", "left", "off-hand", "off hand"}
    )
    is_celestial_weapon = meta.is_celestial and is_weapon_like

    # Equippables (have a "Place") that aren't accessories/adornments are upgradable
    is_equippable = bool(place_lower) and not is_adornment
    is_upgradable = is_equippable and not is_accessory
    has_scaling_slots = is_upgradable and "adornment_slots" in stats

    if is_celestial_weapon:
        boss_scaling = -1
    elif is_upgradable:
        boss_scaling = 1
    else:
        boss_scaling = 0

    return CodexEntry(
        name=name,
        stats=stats,
        is_adornment=is_adornment,
        is_accessory=is_accessory,
        is_celestial_weapon=is_celestial_weapon,
        is_two_handed=is_two_handed,
        is_upgradable=is_upgradable,
        has_scaling_slots=has_scaling_slots,
        boss_scaling=boss_scaling,
    )


@lru_cache(maxsize=512)
def fetch_codex_entry(url: str, lang: str = "en") -> CodexEntry:
    """Fetch and parse a codex item page in the requested language."""
    params = {"lang": lang} if lang and lang != "en" else None
    html = _http_get(url, params=params)
    return parse_codex_html(html)


# -----------------------------------------------------------------------------
# Material tier/rarity lookup (for guild-proof pricing, see orna_proofs.py)
# -----------------------------------------------------------------------------

_TIER_NUM_RE = re.compile(r"\d+")


@dataclass
class MaterialMeta:
    """Tier + rarity for a crafting material — all orna_proofs needs."""
    name: str
    tier: int
    rarity: str
    source_url: str


@lru_cache(maxsize=256)
def fetch_material_meta(name: str, lang: str = "en") -> Optional[MaterialMeta]:
    """
    Search-and-parse a material's Tier/Rarity (e.g. "Tier: ★4", "Rarity: Rare").

    Unlike fetch_codex_entry, this only needs the raw Tier/Rarity labels, not
    the full stat block, so it's kept separate and lightweight.
    """
    for candidate in _name_candidates(name):
        try:
            url = search_first_url(candidate, lang=lang)
        except requests.RequestException:
            raise
        if url is None:
            continue
        params = {"lang": lang} if lang and lang != "en" else None
        html = _http_get(url, params=params)
        soup = BeautifulSoup(html, "html.parser")
        page_meta = _harvest_meta(soup)
        tier_match = _TIER_NUM_RE.search(page_meta.tier)
        if not tier_match or not page_meta.rarity:
            continue
        name_tag = soup.find("h1")
        item_name = name_tag.get_text(strip=True) if name_tag else candidate
        return MaterialMeta(
            name=item_name,
            tier=int(tier_match.group(0)),
            rarity=page_meta.rarity,
            source_url=url,
        )
    return None


def lookup_by_name(
    name: str,
    lang: str = "en",
) -> Tuple[Optional[CodexEntry], Optional[str]]:
    """
    Search by free-text name, parse the first hit. If the raw name doesn't
    resolve, walk through prefix-stripped fallbacks (handles quality +
    enchantment prefixes that aren't part of the codex item name).

    Returns (entry, source_url). Both are None if nothing matched.
    """
    for candidate in _name_candidates(name):
        try:
            url = search_first_url(candidate, lang=lang)
        except requests.RequestException:
            # Surface this — caller can decide. Don't silently swallow.
            raise
        if url is None:
            continue
        entry = fetch_codex_entry(url, lang=lang)
        return entry, url
    return None, None


def clear_cache() -> None:
    """Useful in tests or if you suspect stale codex data."""
    search_first_url.cache_clear()
    fetch_codex_entry.cache_clear()
    fetch_material_meta.cache_clear()
