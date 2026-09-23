"""
orna_scrape_knowledge.py
=========================
One-off / re-run-when-needed script that (re)generates orna_knowledge.txt:
a curated community-knowledge reference for things playorna.com's own codex
simply doesn't track at all - most notably per-monster/boss elemental
damage resistances/immunities (live report: "/orna як вбити Лицар Сіріус"
found nothing useful because the codex has no immunity data for bosses at
all, not even an empty field - the answer only existed in a Reddit thread
found via web_search, AND in this community spreadsheet's "Monster Data"
tab in exact numeric form, verified directly: Knight Sirus's row has
Arcane=1.5, every other element=0, i.e. immune to everything except
arcane, matching the Reddit strategy post word for word).

Source: a set of Google Sheets the user identified as containing this kind
of "hidden info" (community-maintained, not affiliated with Orna's
developers - "Ornapedia" at https://tinyurl.com/ornapedia, a gear-boost
table and combat-mechanics notes from a second sheet, and an orn/xp/gold/
luck bonus calculator). Fetched via the public CSV export endpoint
(docs.google.com/spreadsheets/d/<id>/export?format=csv&gid=<gid>) - no
auth needed, these are public "anyone with the link can view" sheets.

Why one combined text file with generic substring search
(orna_knowledge.py), not per-table typed parsing (unlike, say,
orna_proofs.py's structured formula): these sheets are human-maintained
wiki pages, not clean data tables - merged header cells, explanatory
prose mixed in with rows, inconsistent column counts row to row, and
they'll keep being restructured by their maintainers over time. Hand-
building a typed schema per table (16 of them) would be a lot of brittle
code for content that's inherently semi-structured; a cleaned, flattened
text blob that the LLM reads directly (same "tool retrieves, model
interprets" pattern already used for events()/web_search()) is far less
code and doesn't break the next time a maintainer adds a column.

Why some tabs are excluded: "Home"/"Changelog & FAQ" (Ornapedia) and
"Changelog" (bonus calculator) are meta/version-history pages, not game
data. "Icons" is just an icon-sourcing reference sheet. "eventLvTest" is
visibly WIP scratch data (leading space in its own tab name). None of
these would ever usefully answer a player's question.

Run this again if the user points at updated/additional sheets, or if a
live report shows this data has gone stale:

    AI/venv/bin/python3 orna_scrape_knowledge.py
"""
from __future__ import annotations

import csv
import io
from pathlib import Path

import requests

OUTPUT_PATH = Path(__file__).with_name("orna_knowledge.txt")
HTTP_TIMEOUT = 20.0

# (section title, spreadsheet id, gid, source note) - source note is kept
# in the output header for traceability back to whoever maintains each sheet.
_TABLES = [
    ("Gear XP/Orn/Gold Boosts",
     "1B6F3hYAMZ7t7zeEz7ON7OQFB9zIpGJgQBDgFplhmnhE", "288522693",
     "community calculator, contact Major#1005 on Discord for corrections"),
    ("Combat Mechanics Notes (faction/party/defend/gauntlet/berserk)",
     "1B6F3hYAMZ7t7zeEz7ON7OQFB9zIpGJgQBDgFplhmnhE", "1206792075",
     "community notes, contact eazyx (research team) for corrections"),
    ("Badges", "1H1Fa-J8DgF-5rkupEbOW90cREhmr6xukn_jGstjEfmo", "1785642856", "Ornapedia"),
    ("Boost Items", "1H1Fa-J8DgF-5rkupEbOW90cREhmr6xukn_jGstjEfmo", "1790492424", "Ornapedia"),
    ("Buildings", "1H1Fa-J8DgF-5rkupEbOW90cREhmr6xukn_jGstjEfmo", "778229993", "Ornapedia"),
    ("End of Gauntlet Items", "1H1Fa-J8DgF-5rkupEbOW90cREhmr6xukn_jGstjEfmo", "1570023287", "Ornapedia"),
    ("Monster Data (tier/HP/elemental resistances - includes bosses/raids)",
     "1H1Fa-J8DgF-5rkupEbOW90cREhmr6xukn_jGstjEfmo", "1654896911", "Ornapedia"),
    ("Pets / Followers", "1H1Fa-J8DgF-5rkupEbOW90cREhmr6xukn_jGstjEfmo", "111413653", "Ornapedia"),
    ("Proofs for Materials (community cross-reference)",
     "1H1Fa-J8DgF-5rkupEbOW90cREhmr6xukn_jGstjEfmo", "1458092352", "Ornapedia"),
    ("Raid Rewards", "1H1Fa-J8DgF-5rkupEbOW90cREhmr6xukn_jGstjEfmo", "609072912", "Ornapedia"),
    ("Skills / Spells", "1H1Fa-J8DgF-5rkupEbOW90cREhmr6xukn_jGstjEfmo", "165761277", "Ornapedia"),
    ("Titles", "1H1Fa-J8DgF-5rkupEbOW90cREhmr6xukn_jGstjEfmo", "929997798", "Ornapedia"),
    ("View Distance", "1H1Fa-J8DgF-5rkupEbOW90cREhmr6xukn_jGstjEfmo", "1409156030", "Ornapedia"),
    ("XP / Leveling", "1H1Fa-J8DgF-5rkupEbOW90cREhmr6xukn_jGstjEfmo", "284554719", "Ornapedia"),
    ("Orn/XP/Gold/Luck Bonus Calculator Reference (example totals + per-slot multipliers)",
     "15ErGi6_9XnRiuG6v9AHsYML93pzK3Nsrp7kVVEcVzws", "0", "community calculator"),
    ("Gear Item Boost Values (per-item XP/Orn/Gold/Luck %, before quality scaling)",
     "15ErGi6_9XnRiuG6v9AHsYML93pzK3Nsrp7kVVEcVzws", "1495370665", "community calculator, GearInf tab"),
]


def _fetch_csv_rows(spreadsheet_id: str, gid: str) -> list[list[str]]:
    url = f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/export"
    resp = requests.get(url, params={"format": "csv", "gid": gid}, timeout=HTTP_TIMEOUT)
    resp.raise_for_status()
    return list(csv.reader(io.StringIO(resp.text)))


def _clean_rows(rows: list[list[str]]) -> list[str]:
    """Flatten each row's non-empty cells into one ' | '-joined line, drop
    fully-empty rows. Deliberately NOT trying to infer a fixed column
    schema (see module docstring) - a flattened line the LLM can read
    alongside whatever header line precedes it is enough for it to
    interpret correctly (verified directly against Monster Data's row for
    Knight Sirus)."""
    lines = []
    for row in rows:
        cells = [c.strip() for c in row if c.strip()]
        if cells:
            lines.append(" | ".join(cells))
    return lines


def main() -> None:
    sections = []
    for title, spreadsheet_id, gid, source in _TABLES:
        rows = _fetch_csv_rows(spreadsheet_id, gid)
        lines = _clean_rows(rows)
        sections.append(f"=== {title} ({source}) ===\n" + "\n".join(lines))
        print(f"{title}: {len(lines)} lines")

    header = (
        "# orna_knowledge.txt - community-maintained reference data NOT covered by "
        "playorna.com's own codex (immunities/resistances, gear boost %, combat "
        "mechanics notes, ...). Generated by orna_scrape_knowledge.py - see that "
        "file's docstring for sources and why this is a flattened text blob rather "
        "than typed tables. This is fan-maintained, not official - treat it as "
        "well-informed community knowledge, similar trust level to a good wiki, "
        "not the same authority as the live codex data.\n"
    )
    OUTPUT_PATH.write_text(header + "\n\n".join(sections) + "\n", encoding="utf-8")
    print(f"Wrote {len(sections)} sections to {OUTPUT_PATH} ({OUTPUT_PATH.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
