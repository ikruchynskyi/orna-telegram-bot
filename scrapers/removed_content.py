"""Game content that is gone from Orna but still sits on the wikis and in old posts the scrapers read.

Exploration Dungeons were removed on 2026-07-29 (server 1.331, app 3.26.1, playorna.com/releases):
Goblin Fortresses and Mystic Caves are Battle Dungeons again and the Spelunking Guild is Battle Dungeon
focused. The Ornabook still documents them (an Exploration floor table: tier 11 = 12 floors) and the
2023 v1.4 notes say the guild "rewards players for interacting with Exploration Dungeons" - which is how
/orna told a player to grind the guild in 12-floor tier 11 runs (Battle Dungeons have 25).

scrub() is applied by the corpus scrapers (ornabook, reddit, echo) so a re-scrape can't bring it back.
"Area Exploration" (the Traveler's Guild tab) is a different, current feature and is left alone.
Run `python3 -m scrapers.removed_content` to scrub the committed corpora in place and check it.
"""

from __future__ import annotations

import re

import paths

REMOVED_NOTE = ("Note: Exploration Dungeons were removed from Orna on 2026-07-29 (server 1.331, app 3.26.1). "
                "Goblin Fortresses and Mystic Caves are Battle Dungeons again; there is no mining or Deepshards "
                "in dungeons; the Spelunking Guild is Battle Dungeon focused.")

# A sentence (or table row) about the removed dungeons is dropped; a list that merely includes them is
# rewritten first so the rest of its sentence survives.
_REWRITES = [("Hard dungeons , Exploration Dungeons or Monuments", "Hard dungeons or Monuments"),
             ("both Battle and Exploration Dungeons", "Battle Dungeons"),
             ("a Battle Dungeon or an Exploration Dungeon", "a Battle Dungeon"),
             # the guild-shop list: since the rework the guild trades in Proofs of Spelunking (playorna.com codex)
             ("Spelunking: 250 [Deepshard] Refined Deepshards ", "")]
# a sentence that only continues a dropped one ("They have low key cost...") goes with it
_FOLLOW_ON = re.compile(r"^(They|This|These|It|Their|Its)\b")
# (Deepshard itself is an item, still in quests - "Heinzelmann and the Deepgolems" - so it isn't matched)
_GONE = re.compile(r"exploration (\(deep\) )?dungeons?|\"exploration\" mode|deep kiln|aside from the deepshards", re.I)
_SENTENCE = re.compile(r"(?<=[.!?])\s+")
# Wrong, not removed: playerecho's "Approx. floors" column (★4 12, ★7 18, ★10 25) disagrees with the Ornabook's
# floor chart (10, 13, 22), which orna_mechanics carries. The column goes; the level ranges and themed
# dungeons in the same rows stay.
_WRONG_COLUMN = "Approx. floors"


def scrub(text: str) -> str:
    out: list[str] = []
    skip_section = skip_sub = False
    drop_col = None  # index of _WRONG_COLUMN in the table being read
    for line in text.split("\n"):
        if line == REMOVED_NOTE:
            continue  # added again below its heading
        low = line.lower()
        if line.startswith("=== "):
            skip_section, skip_sub = "exploration dungeon" in low, False
            if not skip_section:
                out.append(line)
                if low.startswith("=== dungeons ("):
                    out.append(REMOVED_NOTE)
            continue
        if line.startswith("## "):
            skip_sub = low.strip() == "## exploration dungeons"
            if low.startswith("## exploration dungeons >"):  # Goblin Fortress / Mystic Cave: Battle Dungeons again
                line = "## Battle Dungeons >" + line[len("## Exploration Dungeons >"):]
        if skip_section or skip_sub:
            continue
        cells = line.split(" | ")
        if _WRONG_COLUMN in cells:
            drop_col = cells.index(_WRONG_COLUMN)
        elif drop_col is not None and len(cells) <= drop_col:
            drop_col = None  # the table ended
        if drop_col is not None:
            line = " | ".join(c for k, c in enumerate(cells) if k != drop_col)
        for old, new in _REWRITES:
            line = line.replace(old, new)
        if _GONE.search(line):
            kept, dropped = [], False
            for s in _SENTENCE.split(line):
                dropped = bool(_GONE.search(s)) or (dropped and bool(_FOLLOW_ON.match(s)))
                if not dropped:
                    kept.append(s)
            if not kept:
                continue
            line = " ".join(kept)
        out.append(line)
    return "\n".join(out)


def _demo() -> None:
    book = ("=== Dungeons (https://x/Dungeons.html) ===\nThere exists 2 kinds of dungeons: Battle Dungeons and Exploration Dungeons.\n"
            "=== Dungeons - Exploration Dungeons (https://x#e) ===\nMining is...\n"
            "=== Dungeons - Themed dungeons (https://x#t) ===\n## Exploration Dungeons\nThey are attractive due to Deepshards.\n"
            "## Exploration Dungeons > Goblin Fortresses [Fort]\nGoblin Fortresses are available starting Tier 3.\n"
            "Mining is done at the Deep Kiln.\n"
            "Any combination except for Endless/Boss is valid. Additionally, Exploration Dungeons cannot be run Horde or Endless.\n"
            "Exploration Dungeon | 5 | Yes\nExploration Dungeons are lucrative. They have low key cost. Hard ones cost more.\nThe only way is to run Hard dungeons , Exploration Dungeons or Monuments, all of which guarantee it.\n"
            "Area Exploration is now found as a tab in the Traveler's Guild menu")
    got = scrub(book)
    rest = got.replace(REMOVED_NOTE, "")
    assert REMOVED_NOTE in got and "Mining is" not in rest and "Deep Kiln" not in rest and "Exploration Dungeon" not in rest, got
    table = scrub("Tier | Level | Approx. floors | Themed dungeon\n★4 | 75-99 | 12 | Great Desert Tomb\n★5 is next.")
    assert table == "Tier | Level | Themed dungeon\n★4 | 75-99 | Great Desert Tomb\n★5 is next.", table
    assert scrub(got) == got   # idempotent: the note isn't scrubbed away or doubled on the next run
    assert "## Battle Dungeons > Goblin Fortresses [Fort]\nGoblin Fortresses are available starting Tier 3." in got, got
    assert "Any combination except for Endless/Boss is valid." in got
    assert "run Hard dungeons or Monuments, all of which guarantee it." in got
    assert "Area Exploration is now found" in got   # the current feature stays
    assert "\nHard ones cost more." in got and "low key cost" not in got, got
    print("removed_content: all checks passed")


if __name__ == "__main__":
    _demo()
    for name in ("orna_ornabook.txt", "orna_reddit.txt", "orna_echo.txt"):
        p = paths.DATA / name
        before = p.read_text(encoding="utf-8")
        after = scrub(before)
        if after != before:
            p.write_text(after, encoding="utf-8")
        print(f"{name}: {before.count(chr(10)) - after.count(chr(10))} lines fewer")
