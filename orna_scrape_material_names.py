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

Why scrape the *materials list* page rather than search-by-name (like
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
from pathlib import Path
from typing import Dict

import requests
from bs4 import BeautifulSoup

LIST_URL = "https://playorna.com/codex/items/"
OUTPUT_PATH = Path(__file__).with_name("orna_material_names_uk.json")
MAX_PAGES = 10  # safety cap; the materials list is currently 2 pages


def _fetch_listing(lang: str) -> Dict[str, str]:
    """slug -> display name, for every material, walking ?p=N pagination."""
    entries: Dict[str, str] = {}
    page = 1
    while page <= MAX_PAGES:
        params = {"q": "", "t": "", "cl": "", "c": "material", "p": page}
        if lang != "en":
            params["lang"] = lang
        resp = requests.get(LIST_URL, params=params, timeout=10)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        links = soup.select("a.codex-entries-entry")
        if not links:
            break
        for a in links:
            slug = a.get("href", "").strip("/").split("/")[-1]
            divs = a.find_all("div")
            name = divs[1].get_text(strip=True) if len(divs) > 1 else None
            if slug and name:
                entries[slug] = name

        if not soup.select_one(f'a[href*="p={page + 1}&"]'):
            break
        page += 1
    return entries


def main() -> None:
    en = _fetch_listing("en")
    uk = _fetch_listing("uk")
    mapping = {en_name: uk[slug] for slug, en_name in en.items() if slug in uk}

    missing = sorted(set(en) - set(uk))
    if missing:
        print(f"Warning: {len(missing)} slug(s) had no Ukrainian name: {missing}")

    with OUTPUT_PATH.open("w", encoding="utf-8") as f:
        json.dump(mapping, f, ensure_ascii=False, indent=2, sort_keys=True)

    print(f"Wrote {len(mapping)} entries to {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
