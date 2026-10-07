"""
orna_sheets.py
===============
Google Sheets access for the "Material Forecast" tab: which guild sells
which material on which date. Shared by the slash-command handlers in
telegram_bot.py and the natural-language flow in telegram_resources.py.
"""
from __future__ import annotations

import datetime
import os
from typing import List

import httpx

SHEETS_API_KEY = os.environ.get("SHEETS_API_KEY")
if not SHEETS_API_KEY:
    raise RuntimeError("SHEETS_API_KEY environment variable is not set")

# Overridable via env so a deployer can point this at their own copy of the
# spreadsheet — the default is the maintainer's. Whatever sheet you use must
# keep the same layout: L6:W68, row 6 a header, column L = material name,
# column M = Anguish (outdated mechanic, skipped), columns N-W = the 10
# active guilds in GUILD_NAMES order below.
SPREADSHEET_ID = os.environ.get("SPREADSHEET_ID", "1gWTEeQnFlNePLTOLCbrzyMWljJjR01L84z2tpeaOAi8")
SHEET_RANGE = os.environ.get("SHEET_RANGE", "Material Forecast!L6:W68")
SHEETS_URL = (
    f"https://sheets.googleapis.com/v4/spreadsheets/{SPREADSHEET_ID}"
    f"/values/{SHEET_RANGE}"
)

# Columns N-W in order (column M / Anguish is skipped as outdated mechanic).
# Also the order of GUILD_PROOFS in orna_proofs.py — keep both in sync.
GUILD_NAMES = ['Agony', 'Despair', 'Melancholy', 'Torment',
               'Coral', 'Deepshards', 'Remembrance', 'Sparring', 'Trials', 'Towers']


def get_today_month_day() -> str:
    today = datetime.date.today()
    return f"{today:%B} {today.day}"


async def fetch_sheet_data() -> List[List[str]]:
    """Fetch material rows from Google Sheets.

    Returns rows 7-68, each as [material_name, date_agony, date_despair, ..., date_towers].
    Column M (Anguish / outdated mechanic) is stripped; guild dates map to GUILD_NAMES by index.
    """
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.get(
            SHEETS_URL, params={"key": SHEETS_API_KEY}
        )
        response.raise_for_status()
        data = response.json()
    rows = data.get("values", [])
    # rows[0] is the header (row 6) — skip it.
    # For each data row: col 0 = material name (L), col 1 = Anguish/outdated (M, skip), cols 2+ = guilds (N-W).
    return [[r[0]] + r[2:] for r in rows[1:] if r]
