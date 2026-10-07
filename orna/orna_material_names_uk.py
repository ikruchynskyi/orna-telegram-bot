"""
orna_material_names_uk.py
==========================
Static English <-> Ukrainian material name table, scraped from the codex's
materials list and matched by item slug (see orna_scrape_material_names.py
for how/why, and to regenerate orna_material_names_uk.json if new materials
are ever added to the game).

Used by telegram_offerings.py as the primary, deterministic lookup for
OCR'd Ukrainian material names, ahead of the LLM fallback.
"""
from __future__ import annotations

import paths
import json
from typing import Dict

_DATA_PATH = paths.DATA / "orna_material_names_uk.json"

with _DATA_PATH.open(encoding="utf-8") as _f:
    EN_TO_UK: Dict[str, str] = json.load(_f)

UK_TO_EN: Dict[str, str] = {uk.lower(): en for en, uk in EN_TO_UK.items()}

# Every item (potions, keys, tokens, gear) - for rendering cards, never for
# fuzzy matching (see orna_scrape_material_names.ITEMS_OUTPUT_PATH).
_ITEMS_PATH = paths.DATA / "orna_item_names_uk.json"
ITEM_EN_TO_UK: Dict[str, str] = (json.loads(_ITEMS_PATH.read_text(encoding="utf-8"))
                                 if _ITEMS_PATH.exists() else dict(EN_TO_UK))
