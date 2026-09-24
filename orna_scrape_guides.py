"""
orna_scrape_guides.py
=======================
One-off / re-run-when-needed script that (re)generates the per-class/
build-topic community guide files (orna_guide_<topic>.txt) - long-form
written strategy guides for specific classes (Summoner, Realmshifter/
Thief, Deity, Gilgamesh, Beowulf, Heretic), the Swash build (usable by
any class, not a class itself), and Towers of Olympia mechanics.

Deliberately SEPARATE from orna_scrape_knowledge.py/orna_knowledge.txt,
not another section added to that file: orna_knowledge.txt is a single
corpus fuzzy-searched as one blob (short community-wiki FACTS - tier/HP/
resistance rows). These guides are long-form REASONING (why a build
works, tradeoffs between two setups) meant to be handed to the model
whole when a question is clearly about one specific class/topic, not
grepped for a fragment - see orna_guides.py, which reads these files by
topic key rather than searching across all of them at once.

Two source kinds:
- Google Sheets (Gilgamesh, Beowulf, Swash, Heretic, Towers, Summoner) -
  fetched per-tab via the public CSV export endpoint
  (docs.google.com/spreadsheets/d/<id>/export?format=csv&gid=<gid>), same
  as orna_scrape_knowledge.py. A guide's own tab list was discovered by
  fetching its /htmlview page and reading the embedded
  `{name: "...", pageUrl: "...gid=..."}` entries it renders its own tab
  bar from - not guessed. A few tabs are excluded per guide (noted inline
  below) for the same reasons orna_scrape_knowledge.py already excludes
  some Ornapedia tabs: icon-sourcing reference sheets, version-history/
  changelogs, and visibly WIP/template scratch pages, none of which
  would ever usefully answer a player's question.
- Google Docs (Realmshifter/Thief, Deity) - fetched whole via
  docs.google.com/document/d/<id>/export?format=txt. Kept as prose (NOT
  flattened into " | " rows - that convention is for spreadsheet cells
  only), since these are written guides meant to be read as paragraphs.

All sources are public "anyone with the link can view" docs/sheets - no
auth needed.

Run this again if the user points at updated/additional guides, or a
live report shows one has gone stale:

    AI/venv/bin/python3 orna_scrape_guides.py
"""
from __future__ import annotations

import csv
import io
from pathlib import Path

import requests

HTTP_TIMEOUT = 20.0

_SOURCES = [
    {
        "topic": "summoner",
        "title": "Ultimate Circle of Grand Summoners RESUMMONED",
        "filename": "orna_guide_summoner.txt",
        "kind": "sheet",
        "spreadsheet_id": "1JDNee0miAjx_pGWp6MxrEq_e7NnmJsbS11x60brE4sw",
        "source_url": "https://docs.google.com/spreadsheets/u/0/d/1JDNee0miAjx_pGWp6MxrEq_e7NnmJsbS11x60brE4sw/htmlview",
        # all 9 tabs discovered via htmlview are real guide content - none excluded.
        "tabs": [
            ("Introduction", "1899425289"),
            ("FAQ", "1091779678"),
            ("Abilities & Stats", "1988660942"),
            ("Classline Skills", "0"),
            ("Noteworthy Skills", "369549762"),
            ("Classline Gear", "219905603"),
            ("Noteworthy Gear", "1982876381"),
            ("Builds v.2", "1111676202"),
            ("Endless", "388248342"),
        ],
    },
    {
        "topic": "thief",
        "title": "Realmshifter raiding (a guide by Cosmo)",
        "filename": "orna_guide_thief.txt",
        "kind": "doc",
        "doc_id": "1tsmnXoyVvlNSgBeFwFWpNT09R9bYGtGO-UcmNK_UjTY",
        "source_url": "https://docs.google.com/document/u/0/d/1tsmnXoyVvlNSgBeFwFWpNT09R9bYGtGO-UcmNK_UjTY/mobilebasic",
    },
    {
        "topic": "deity",
        "title": "Getting started with Deity (a guide by Cosmo)",
        "filename": "orna_guide_deity.txt",
        "kind": "doc",
        "doc_id": "1Iv2iUoNOfQhPY0OFeBO-b6bMAvwnTGkYFGC9n9nKQqI",
        "source_url": "https://docs.google.com/document/u/0/d/1Iv2iUoNOfQhPY0OFeBO-b6bMAvwnTGkYFGC9n9nKQqI/mobilebasic",
    },
    {
        "topic": "gilgamesh",
        "title": "All Gilga Ward Pieces",
        "filename": "orna_guide_gilgamesh.txt",
        "kind": "sheet",
        "spreadsheet_id": "1KFpi0ulS5FnfFEW34h1v4G93m6ObgxXHXX6zTkM4sWk",
        "source_url": "https://docs.google.com/spreadsheets/d/1KFpi0ulS5FnfFEW34h1v4G93m6ObgxXHXX6zTkM4sWk/edit?gid=0",
        "tabs": [
            ("Helm", "413780019"),
            ("Chest", "1746725425"),
            ("Legs", "1261501130"),
            ("Offhand", "1537037876"),
            ("Accessories", "1462184566"),
        ],
    },
    {
        "topic": "beowulf",
        "title": "Beowulf - The Ultimate Guide",
        "filename": "orna_guide_beowulf.txt",
        "kind": "sheet",
        "spreadsheet_id": "1jOGIgXc_Igh71Dm6d1QLaFj3TlM3nM-xQO3kUt5vsTo",
        "source_url": "https://docs.google.com/spreadsheets/d/1jOGIgXc_Igh71Dm6d1QLaFj3TlM3nM-xQO3kUt5vsTo/edit?gid=1723210392",
        # excluded: "Final-Icon-Page" (gid=1250065797) - icon-sourcing reference sheet,
        # same exclusion reason as orna_scrape_knowledge.py's "Icons" tab.
        "tabs": [
            ("Main Notes on Beo", "1723210392"),
            ("Tower Purchases", "972258910"),
            ("Follower Tierlist v3.0", "1612779048"),
            ("Event Gear v3.0", "266926470"),
            ("Follower Act Rates", "735029241"),
            ("Base Beo Builds", "829657438"),
            ("BeoA Builds", "1760156437"),
            ("BeoH Builds", "1897727627"),
            ("Endless", "494098142"),
        ],
    },
    {
        "topic": "swash",
        "title": "Swash/Blade Gear List (T10/T11)",
        "filename": "orna_guide_swash.txt",
        "kind": "sheet",
        "spreadsheet_id": "1rL-CupUTM58bWCy9zsf8auKj7cnm3-zcssGF58JgFxk",
        "source_url": "https://docs.google.com/spreadsheets/d/1rL-CupUTM58bWCy9zsf8auKj7cnm3-zcssGF58JgFxk/edit?gid=0",
        "tabs": [
            ("Head Slot", "0"),
            ("Body Slot", "1633161369"),
            ("Legs Slot", "1203890731"),
            ("Offhand Slot", "1514517296"),
        ],
    },
    {
        "topic": "heretic",
        "title": "Heretic - One Turn Orna",
        "filename": "orna_guide_heretic.txt",
        "kind": "sheet",
        "spreadsheet_id": "1unU5LcivK_lO-y5xB8U6rWd2JxbuS99zdKEYNQLp7Us",
        "source_url": "https://docs.google.com/spreadsheets/d/1unU5LcivK_lO-y5xB8U6rWd2JxbuS99zdKEYNQLp7Us/edit?gid=1378658536",
        # excluded: "WIP - Your Inventory" (a per-reader inventory-tracking template, not
        # guide content), "Example Build Page (DUPLICATE THIS)" (explicitly a template
        # for the reader to copy), "Icon-Oclast" (icon-sourcing reference sheet).
        "tabs": [
            ("Towers Purchases", "1378658536"),
            ("Early T10", "46578419"),
            ("View Distance", "882110285"),
            ("Dungeon", "714577244"),
            ("Towers", "913566591"),
            ("Raids", "818288178"),
            ("BoF Guild", "442063739"),
            ("Flask Calculators", "1887270385"),
        ],
    },
    {
        "topic": "towers",
        "title": "Towers of Olympia Breakdown",
        "filename": "orna_guide_towers.txt",
        "kind": "sheet",
        "spreadsheet_id": "1KQrKTDzA_eEmbMoU7WfyRgA3WrJuoCUmmvD65cCdIg0",
        "source_url": "https://docs.google.com/spreadsheets/d/1KQrKTDzA_eEmbMoU7WfyRgA3WrJuoCUmmvD65cCdIg0/edit?gid=1896770848",
        # excluded: "Changelog" (version history, not data), "WIP - Tower Enemies"
        # (visibly WIP scratch, same exclusion reason as orna_scrape_knowledge.py's
        # "eventLvTest"). NOTE: this sheet is REWARDS/mechanics reference content, not
        # the wild-tower height algorithm - that's orna_towers.py, ported from
        # OrnaCodex's own tower.ts, and needs no spreadsheet data at all.
        "tabs": [
            ("Wild Towers", "1896770848"),
            ("Building Towers", "1303179130"),
            ("Augments", "2049829913"),
            ("Classes", "14454573"),
            ("Weaponry", "924867268"),
        ],
    },
]


def _fetch_csv_rows(spreadsheet_id: str, gid: str) -> list[list[str]]:
    url = f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/export"
    resp = requests.get(url, params={"format": "csv", "gid": gid}, timeout=HTTP_TIMEOUT)
    resp.raise_for_status()
    return list(csv.reader(io.StringIO(resp.text)))


def _clean_rows(rows: list[list[str]]) -> list[str]:
    """Same flattening convention as orna_scrape_knowledge.py._clean_rows -
    one ' | '-joined line per non-empty row, no fixed column schema."""
    lines = []
    for row in rows:
        cells = [c.strip() for c in row if c.strip()]
        if cells:
            lines.append(" | ".join(cells))
    return lines


def _fetch_doc_text(doc_id: str) -> str:
    url = f"https://docs.google.com/document/d/{doc_id}/export"
    resp = requests.get(url, params={"format": "txt"}, timeout=HTTP_TIMEOUT)
    resp.raise_for_status()
    return resp.text


def _clean_doc_text(text: str) -> str:
    """Strip the leading BOM Docs' export adds, collapse runs of 3+ blank
    lines to 1 - otherwise preserve the doc's own paragraph/heading
    structure as-is (prose, not flattened rows - see module docstring)."""
    text = text.lstrip("﻿")
    lines = [line.rstrip() for line in text.split("\n")]
    out: list[str] = []
    blank_run = 0
    for line in lines:
        if line == "":
            blank_run += 1
            if blank_run > 2:
                continue
        else:
            blank_run = 0
        out.append(line)
    return "\n".join(out).strip()


def _build_sheet_body(source: dict) -> str:
    sections = []
    for tab_name, gid in source["tabs"]:
        rows = _fetch_csv_rows(source["spreadsheet_id"], gid)
        lines = _clean_rows(rows)
        sections.append(f"--- {tab_name} ---\n" + "\n".join(lines))
        print(f"    [{tab_name}] {len(lines)} lines")
    return "\n\n".join(sections)


def _build_doc_body(source: dict) -> str:
    text = _fetch_doc_text(source["doc_id"])
    cleaned = _clean_doc_text(text)
    print(f"    {len(cleaned.splitlines())} lines")
    return cleaned


def main() -> None:
    for source in _SOURCES:
        print(f"=== {source['title']} ({source['topic']}) ===")
        if source["kind"] == "sheet":
            body = _build_sheet_body(source)
        else:
            body = _build_doc_body(source)

        header = (
            f"# {source['filename']} - community {'build/class' if source['topic'] != 'towers' else 'game-system'} "
            f"guide, NOT official Orna developer content - treat as well-informed community "
            f"knowledge, similar trust level to a good wiki, not the same authority as the live "
            f"codex data. Title: {source['title']!r}. Source: {source['source_url']} . Generated "
            f"by orna_scrape_guides.py.\n"
        )
        out_path = Path(__file__).with_name(source["filename"])
        out_path.write_text(header + "\n" + body + "\n", encoding="utf-8")
        print(f"Wrote {out_path.name} ({out_path.stat().st_size} bytes)\n")


if __name__ == "__main__":
    main()
