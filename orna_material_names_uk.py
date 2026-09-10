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

import json
from pathlib import Path
from typing import Dict

_DATA_PATH = Path(__file__).with_name("orna_material_names_uk.json")

with _DATA_PATH.open(encoding="utf-8") as _f:
    EN_TO_UK: Dict[str, str] = json.load(_f)

UK_TO_EN: Dict[str, str] = {uk.lower(): en for en, uk in EN_TO_UK.items()}
