"""
orna_mechanics.py - runtime reader for orna_mechanics.txt, a curated,
community-verified (2026) reference of Orna's core game mechanics (factions,
ascension, item quality/forging, adornments, wild towers, flasks, kingdoms,
followers, ...) that the codex states nowhere as prose.

Sibling of orna_knowledge.py, but deliberately much simpler: that corpus is
scraped table ROWS (self-contained records, so a line is the unit and it needs
fetch/cache/rebuild machinery). This is a hand-written STATIC file of PROSE
paragraphs, so:
  * the retrieval unit is the whole matching SECTION, not a line - half a
    sentence out of context is useless (same reason orna_guides hands over a
    whole guide, not a grepped fragment); and
  * there is no cache/rebuild - it changes only when someone edits the file.

"tool retrieves, model interprets" - the same split as orna_knowledge /
orna_guides / web_search. Read by knowledge_search
(telegram_orna._run_knowledge_tool), which aggregates this alongside the
sheets/reddit/class corpora.
"""
from __future__ import annotations

import paths
from typing import Optional

DATA_PATH = paths.DATA / "orna_mechanics.txt"
# One curated source for the whole file, cited when any section is returned.
SOURCE_TITLE = "Orna game mechanics (community-verified 2026)"
SOURCE_URL = "https://www.ornalegends.com/home/the-ultimate-ornarpg-beginner-basics-guide"

# A few whole sections is plenty of context for one observation; the corpus is
# only ~7KB total, so this mostly guards against a very broad query dragging in
# half the file.
# Words that appear in nearly every section carry no topic signal. Without
# dropping them, every section scores >= 1 on a query like "how does X work"
# and a genuine no-match returns noise instead of "" (which the caller reads as
# "nothing here, try web_search").

_sections: Optional[list] = None   # list[(title, body)]
_vocab: Optional[set] = None


def _load() -> list:
    global _sections
    if _sections is None:
        text = DATA_PATH.read_text(encoding="utf-8")
        out = []
        for chunk in text.split("\n=== ")[1:]:  # [0] is the leading comment header
            title_line, _, body = chunk.partition("\n")
            title = title_line[:-4] if title_line.endswith(" ===") else title_line
            out.append((title.strip(), body.strip()))
        _sections = out
    return _sections


def _demo() -> None:
    """Pins the facts the bot must be able to cite (factions, ascension,
    forging/adornment slots, quality, towers) in the parsed sections. Finding
    them is orna_pinecone / orna_textindex's job (see the suite's retrieval
    benchmark). Run: `python3 -m knowledge.orna_mechanics`."""
    secs = dict(_load())
    fac = secs["Elements and factions"]
    for name in ("Earthen Legion", "Stormforce", "Knights of Inferno", "Frozenguard"):
        assert name in fac, (name, fac[:200])
    assert "+25%" in fac and "-20%" in fac, fac[:200]
    asc = secs["Ascension Level (AL)"]                 # +1%/level endgame axis, not an 11th tier
    assert "+1%" in asc and "AL 100" in asc, asc[:200]
    tiers = secs["Tiers and character level"].lower()
    assert "no tier 11" in tiers or "10 class tiers" in tiers
    forge = secs["Upgrading and forging"]               # b816bba's live-verified maxima
    assert "11" in forge and "12" in forge and "13" in forge, forge[:200]
    adorn = secs["Adornments (jewels)"]
    assert "godforged" in adorn.lower() and "16" in adorn, adorn[:200]
    assert "Legendary" in secs["Item quality"]
    assert any(t in secs["Wild Towers of Olympia"] for t in ("Selene", "Eos", "Prometheus"))
    print("orna_mechanics: all checks passed")


if __name__ == "__main__":
    _demo()
