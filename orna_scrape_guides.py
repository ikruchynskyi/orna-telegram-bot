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
from typing import Optional

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
        # "Your (Ideal) Inventory" (gid 1489719792) was WRONGLY excluded in an earlier
        # pass as "a per-reader inventory-tracking template, not guide content" - direct
        # inspection (prompted by a live report: "/orna what are ideal items for heretic"
        # answered with fabricated/misattributed items instead of this tab's real S/A/B/C
        # rated list) showed it's real, substantial, populated content - the single most
        # directly relevant tab for exactly that question - not a template at all. Still
        # correctly excluded: "Example Build Page (DUPLICATE THIS)" (explicitly a template
        # for the reader to copy) and "Icon-Oclast" (icon-sourcing reference sheet).
        "tabs": [
            ("Towers Purchases", "1378658536"),
            ("Early T10", "46578419"),
            ("View Distance", "882110285"),
            ("Dungeon", "714577244"),
            ("Towers", "913566591"),
            ("Raids", "818288178"),
            ("BoF Guild", "442063739"),
            ("Flask Calculators", "1887270385"),
            ("Your Ideal Inventory", "1489719792"),
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
    one ' | '-joined line per non-empty row, no fixed column schema.
    Only correct for a DENSE table (one entity per row, e.g. Gilgamesh/
    Swash's gear-list sheets) - see _find_build_row/_transpose_build_table
    for the wide "one entity per column-block" layout this silently
    scrambles."""
    lines = []
    for row in rows:
        cells = [c.strip() for c in row if c.strip()]
        if cells:
            lines.append(" | ".join(cells))
    return lines


_LABEL_COL = 1  # column B - every guide sheet seen so far puts row labels here


def _find_build_row(rows: list[list[str]]) -> Optional[int]:
    """Some guide tabs lay builds out as PARALLEL COLUMN BLOCKS (one
    build per block of columns, not one row per build) instead of a
    dense per-row table - marked by a row whose cells literally read
    "Build Name" (the row label) followed by each build's own name at
    that build's start column. Scanning the first 15 rows is enough -
    every sheet seen has this header within the first 6."""
    for i, row in enumerate(rows[:15]):
        for cell in row:
            if cell.strip().lower() == "build name":
                return i
    return None


def _transpose_build_table(rows: list[list[str]], build_row_idx: int) -> str:
    """Reconstructs one "=== Build Name ===" section per build from a
    column-block-laid-out table, instead of _clean_rows' naive "strip
    empty cells, join what's left" flattening - which silently loses
    column identity the moment ANY row has a gap (nearly every row
    here). Live-verified data loss this caused: Heretic's Raids tab,
    "Omniflask Weakness" build - its own data spans TWO columns (S:
    Priorities/Notes/Spells, T: the actual Class/Spec/Weapon/Offhand/
    Headpiece/Armor/Legwear/Accessories/Amity/Pet gear list) while the
    row label lives in column B; the old flattening happened to keep
    column S's values (Priorities/Notes survived - directly under the
    label in the exact row they occupied) but dropped T's ENTIRE gear
    list outright, since most of those rows had label column B empty
    (a build's 2nd weapon/offhand option) or had OTHER builds' cells
    at different relative offsets, so the positional join scrambled or
    dropped them. Confirmed both Heretic (6 of 8 tabs) and Beowulf (4
    of 9 tabs) use this exact layout for their build-comparison sheets;
    Gilgamesh and Swash's gear-list sheets are dense one-item-per-row
    tables and never hit this function at all (_find_build_row returns
    None for them).

    A build's own column range is [its start column, the NEXT build's
    start column) - taken from the "Build Name" row itself, which names
    every build at its own start column. A row with an empty label
    (a build's second gear option, e.g. two possible Weapons) is
    attached to the PREVIOUS non-empty label, not dropped - matches how
    the sheet visually merges that label cell downward instead of
    repeating it."""
    header = rows[build_row_idx]
    build_starts = [(j, cell.strip()) for j, cell in enumerate(header) if j != _LABEL_COL and cell.strip()]
    if not build_starts:
        return ""
    ncols = max(len(r) for r in rows[build_row_idx:])

    build_lines: list[list[str]] = [[] for _ in build_starts]
    last_label = ""
    for row in rows[build_row_idx:]:
        label = row[_LABEL_COL].strip() if len(row) > _LABEL_COL else ""
        if label:
            last_label = label
        use_label = label or last_label
        for bi, (start_col, _name) in enumerate(build_starts):
            end_col = build_starts[bi + 1][0] if bi + 1 < len(build_starts) else ncols
            values = [row[c].strip() for c in range(start_col, min(end_col, len(row))) if row[c].strip()]
            if values and use_label:
                build_lines[bi].append(f"{use_label}: {', '.join(values)}")

    sections = [
        # A build name can itself carry an embedded newline (a sheet
        # author's own multi-line cell, e.g. "Boss Horde Dungeons\n(No
        # Exotic Items)") - collapse it to one line so the "=== ... ==="
        # section delimiter this reuses orna_knowledge.txt's own
        # convention for stays a single, greppable line.
        f"=== {' '.join(name.split())} ===\n" + "\n".join(lines)
        for (_, name), lines in zip(build_starts, build_lines) if lines
    ]
    return "\n\n".join(sections)


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
        build_row = _find_build_row(rows)
        if build_row is not None:
            # Rows before the build table itself (an intro paragraph,
            # etc.) still get the normal dense flattening.
            intro = _clean_rows(rows[:build_row])
            body = _transpose_build_table(rows, build_row)
            text = "\n".join(intro + ([body] if body else []))
            kind = "build-table"
        else:
            text = "\n".join(_clean_rows(rows))
            kind = "dense"
        sections.append(f"--- {tab_name} ---\n{text}")
        print(f"    [{tab_name}] {len(text.splitlines())} lines ({kind})")
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
