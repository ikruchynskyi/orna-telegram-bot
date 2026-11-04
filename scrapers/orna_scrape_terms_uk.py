"""The game's own Ukrainian for its general terms (Tier, Rarity, Followers, stat names...), for the translator.

    python3 -m scrapers.orna_scrape_terms_uk          # rebuild data/orna_terms_uk.json
    python3 -m scrapers.orna_scrape_terms_uk --demo   # self-check, no network

Names of items, monsters and classes are pinned in English (identifiers), but ordinary game words were left
to the translation model, which drifts: "Тир" one day, "тір" the next - and the game itself says "Ранг"
(codex, 2026-11-03). So the terms come from the codex: the same pages are fetched in English and Ukrainian,
their short text labels paired by position (the page structure is the same in both), and a pair is kept
only when it appears on at least MIN_PAGES pages - a UI label repeats, an entity's own name mostly does not;
a term that IS an entity's name in the codex (a monster and its follower share a page label) is dropped too,
since names stay in English anyway.
"""

from __future__ import annotations

import html
import json
import re
import sys
from collections import Counter

import paths

OUTPUT = paths.DATA / "orna_terms_uk.json"
MIN_PAGES = 2
# a few of each kind of codex page: gear with stats, monsters, bosses, followers, spells, classes, dungeons
QUERIES = ("sword", "staff", "armor", "Titan Mnemosyne", "Bear", "Wolf", "Thief", "Mage", "dungeon", "Ward",
           "Fire", "Crucible", "raid", "tower", "kingdom")
PER_QUERY = 3
# site chrome, not game terms
_CHROME = {"Skip to content", "Play free", "Decline", "Accept", "Privacy Policy", "Search", "Join our Discord ↗",
           "Home", "Terms of Use", "Credits & Attributions", "Merch", "Event Calendar", "Release Notes", "Updates",
           "The Codex", "Codex", "Events", "Made in Canada 🍁",
           # labels that read wrong as a general term: the imperative "Удосконалюй матеріали", "Mana cost" ->
           # "Мана", "Resistance" -> the dative "Магічній опір", "Event" -> the plural "Події"
           "Upgrade materials", "Mana cost", "Resistance", "Event"}


def _labels(page_html: str) -> list:
    t = re.sub(r"<script.*?</script>|<style.*?</style>", " ", page_html, flags=re.S)
    return [s for s in (" ".join(html.unescape(x).split()) for x in re.split(r"<[^>]+>", t)) if s]


def pairs(en: list, uk: list) -> list:
    """Label pairs of one page; [] when the two versions don't line up (then position means nothing)."""
    if len(en) != len(uk):
        return []
    return [(a, b) for a, b in zip(en, uk)
            if a != b and a not in _CHROME and len(a) <= 30 and not re.search(r"\d|/", a) and re.search(r"[A-Za-z]", a)]


def build() -> dict:
    from orna.orna_codex import CODEX_BASE, _http_get, codex_search
    seen, n = Counter(), 0
    urls = list(dict.fromkeys(r["url"] for q in QUERIES for r in codex_search(q).get("results", [])[:PER_QUERY]))
    for url in urls:
        try:
            en = _labels(_http_get(CODEX_BASE + url))
            uk = _labels(_http_get(CODEX_BASE + url, params={"lang": "uk"}))
        except Exception as e:
            print(f"  {url}: {e}", file=sys.stderr)
            continue
        got = set(pairs(en, uk))
        n += bool(got)
        seen.update(got)
        print(f"  {url}: {len(got)} labels", file=sys.stderr)
    # one English term, one Ukrainian: the most frequent pairing wins
    out = {}
    for (a, b), c in seen.most_common():
        if c >= MIN_PAGES and a not in out:
            out[a] = b
    for a in list(out):
        if any(r.get("name", "").lower() == a.lower() for r in codex_search(a).get("results", [])):
            del out[a]
    print(f"{len(out)} terms from {n} of {len(urls)} pages", file=sys.stderr)
    return dict(sorted(out.items()))


def _demo() -> None:
    en = ["Skip to content", "Titan Crucible", "Tier", "10", "Rarity", "Legendary", "Dropped by"]
    uk = ["Пропустити до контенту", "Тигель Титанів", "Ранг", "10", "Якість", "Легендарне", "Випадає з"]
    assert pairs(en, uk) == [("Titan Crucible", "Тигель Титанів"), ("Tier", "Ранг"), ("Rarity", "Якість"),
                             ("Legendary", "Легендарне"), ("Dropped by", "Випадає з")]
    assert pairs(en, uk[:-1]) == []
    assert _labels("<div>Tier</div><script>x</script><b> Rarity &amp; more </b>") == ["Tier", "Rarity & more"]
    print("orna_scrape_terms_uk: all checks passed")


if __name__ == "__main__":
    if "--demo" in sys.argv:
        _demo()
    else:
        terms = build()
        if not terms:
            raise SystemExit("no terms found - keeping the old file")
        OUTPUT.write_text(json.dumps(terms, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        print(json.dumps(terms, ensure_ascii=False, indent=1))
