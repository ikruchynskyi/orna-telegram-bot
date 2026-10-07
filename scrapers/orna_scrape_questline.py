"""Build orna_questline.txt from Konq's Unfelled questline guide (a public Google Doc).

One `## Tier N - Quest M: ...` section per quest, in the `=== Title (url) ===`
format orna_echo's section reader parses (same as orna_ornabook.txt), so it is
searchable by grep and by Pinecone ("questline" namespace) with no new reader.

Run: python3 -m scrapers.orna_scrape_questline && python3 -m knowledge.orna_pinecone questline
"""
import paths
import re

import httpx

DOC_ID = "1z6Efh6Heuo6lF1O5_FnvXpJbT8KVyBnghUTiqCie5UM"
SOURCE_URL = f"https://docs.google.com/document/d/{DOC_ID}/edit"
OUT = paths.DATA / "orna_questline.txt"
TITLE = "Unfelled questline guide by Konq (April 2022)"

_TIER_RE = re.compile(r"^Tier (\d+) Quests\s*$")
_QUEST_RE = re.compile(r"^Quest \d+(:| -) .+")   # 52 and 53 are written "Quest 52 - ..."


def build(text: str) -> str:
    """Doc text -> corpus. Quest headings carry their tier, so a section found
    on its own still says where in the questline it is."""
    out = [f"# {TITLE} - built by orna_scrape_questline.py", "", f"=== {TITLE} ({SOURCE_URL}) ==="]
    tier = ""
    for line in text.lstrip("﻿").splitlines():
        line = line.rstrip()
        m = _TIER_RE.match(line)
        if m:
            tier = f"Tier {m.group(1)} - "
            continue
        if _QUEST_RE.match(line):
            out.append(f"## {tier}{line}")
        elif line in ("Introduction", "Thank You!"):
            out.append(f"## {line}")
        elif line.strip():
            out.append(line)
    return "\n".join(out) + "\n"


def _demo() -> None:
    doc = "﻿Intro line\nTier 1 Quests\n\nQuest 1: Samson - Defeat Rat (2)\nNew: easy.\n"
    body = build(doc)
    assert "## Tier 1 - Quest 1: Samson - Defeat Rat (2)\nNew: easy." in body, body
    from knowledge import orna_echo
    secs = orna_echo._parse(body)
    assert [s.heading for s in secs] == ["", "Tier 1 - Quest 1: Samson - Defeat Rat (2)"], secs


if __name__ == "__main__":
    _demo()
    r = httpx.get(f"https://docs.google.com/document/d/{DOC_ID}/export", params={"format": "txt"},
                  follow_redirects=True, timeout=60)
    r.raise_for_status()
    body = build(r.text)
    n = body.count("\n## Tier ")
    if n < 40:          # the doc has 53 quests; never overwrite a good file with a broken parse
        raise SystemExit(f"only {n} quest sections parsed - not writing {OUT.name}")
    OUT.write_text(body, encoding="utf-8")
    print(f"{OUT.name}: {n} quests, {len(body)} chars")
