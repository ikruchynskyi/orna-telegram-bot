"""
orna_scrape_material_names.py
==============================
One-off / re-run-when-needed script that (re)generates orna_material_names_uk.json:
an English -> Ukrainian material name table.

Why this exists instead of just asking the LLM to translate: gpt-oss:20b is
not reliable enough to be a single source of truth for this — the same input
("Титан") returns the right answer on some calls and an empty match on
others (verified: 3 repeated calls, 1 miss). A static table scraped once
from the codex itself is deterministic.

Why scrape the *materials list* rather than search-by-name (like
orna_codex.search_first_url does for the assess flow): searching a short
common word like "Soul" can match the wrong item first (verified: it
resolved to an unrelated item's page instead of the material). The list
page at /codex/items/?c=material enumerates every material with its slug
directly, so cross-referencing the English and Ukrainian listings by slug
is unambiguous.

Run this again if new materials are added to the game (or orna_sheets'
Material Forecast tab lists a name this script's output doesn't cover):

    AI/venv/bin/python3 orna_scrape_material_names.py
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Dict

from orna_codex import fetch_codex_json

OUTPUT_PATH = Path(__file__).with_name("orna_material_names_uk.json")
# Every item (2,773 in 2026-10): potions, keys, tokens, gear - what tool cards
# show next to materials. Kept SEPARATE from the materials table on purpose:
# telegram_offerings and the /orna input glossary fuzzy-match against that one,
# and 2,700 more names would turn near-misses into wrong matches.
ITEMS_OUTPUT_PATH = Path(__file__).with_name("orna_item_names_uk.json")
MAX_PAGES = 200  # runaway guard; the full list is ~70 pages


def _fetch_listing(lang: str, item_type: str = "") -> Dict[str, str]:
    """slug -> display name from the codex item list's bootstrap JSON (the
    page used to be server-rendered HTML; 2026-10 it is JSON only, and the old
    HTML parser silently found 0 entries). Slug pairs EN with UK exactly."""
    entries: Dict[str, str] = {}
    page, pages = 1, 1
    while page <= min(pages, MAX_PAGES):
        params = (("p", str(page)),) + ((("c", item_type),) if item_type else ())
        data = fetch_codex_json("/codex/items/", lang=lang, extra_params=params)
        pages = int(data.get("pages") or 1)
        for r in data.get("results") or []:
            slug = (r.get("url") or "").strip("/").split("/")[-1]
            if slug and r.get("name"):
                entries[slug] = r["name"].strip()
        page += 1
        time.sleep(0.3)
    return entries


def _write(path: Path, en: Dict[str, str], uk: Dict[str, str]) -> None:
    mapping = {en_name: uk[slug] for slug, en_name in en.items() if slug in uk}
    if len(mapping) < 0.9 * len(en) or not mapping:
        raise SystemExit(f"refusing to write {path.name}: only {len(mapping)} of {len(en)} names paired")
    with path.open("w", encoding="utf-8") as f:
        json.dump(mapping, f, ensure_ascii=False, indent=2, sort_keys=True)
    print(f"Wrote {len(mapping)} entries to {path}")


def main() -> None:
    _write(OUTPUT_PATH, _fetch_listing("en", "material"), _fetch_listing("uk", "material"))
    _write(ITEMS_OUTPUT_PATH, _fetch_listing("en"), _fetch_listing("uk"))


if __name__ == "__main__":
    main()
