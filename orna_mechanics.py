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

import difflib
import re
from pathlib import Path
from typing import Optional

DATA_PATH = Path(__file__).with_name("orna_mechanics.txt")
# One curated source for the whole file, cited when any section is returned.
SOURCE_TITLE = "Orna game mechanics (community-verified 2026)"
SOURCE_URL = "https://www.ornalegends.com/home/the-ultimate-ornarpg-beginner-basics-guide"

# A few whole sections is plenty of context for one observation; the corpus is
# only ~7KB total, so this mostly guards against a very broad query dragging in
# half the file.
_MAX_CHARS = 4000
_WORD_RE = re.compile(r"[a-z][a-z'-]{2,}")
# Words that appear in nearly every section carry no topic signal. Without
# dropping them, every section scores >= 1 on a query like "how does X work"
# and a genuine no-match returns noise instead of "" (which the caller reads as
# "nothing here, try web_search").
_STOPWORDS = {
    "the", "and", "for", "are", "with", "you", "your", "how", "what", "does",
    "did", "can", "when", "where", "why", "who", "this", "that", "much",
    "many", "orna", "game", "work", "works", "which", "into", "from", "has",
    "have", "its", "it's", "get", "gets", "all", "any", "per", "was", "were",
}

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


def list_sections() -> list:
    return [t for t, _ in _load()]


def _content_words(text: str) -> set:
    return {w for w in _WORD_RE.findall(text.lower()) if len(w) >= 3 and w not in _STOPWORDS}


def _vocabulary() -> set:
    global _vocab
    if _vocab is None:
        v: set = set()
        for t, b in _load():
            v |= _content_words(t + " " + b)
        _vocab = v
    return _vocab


def _score(qwords: set, title: str, body: str) -> int:
    """Distinct query words present. A title hit counts triple - a section
    NAMED for the topic ("Elements and factions") is the one a "factions"
    query wants, over one that merely mentions the word in passing."""
    tl, bl = title.lower(), body.lower()
    return sum((3 if w in tl else 0) + (1 if w in bl else 0) for w in qwords)


def _correct(qwords: set) -> set:
    """Fuzzy-correct off-by-a-bit words against the corpus vocabulary - the
    same transliteration-drift fix orna_knowledge/orna_aussies use, so a
    slightly misspelled or transliterated term still lands."""
    vocab = _vocabulary()
    out = set()
    for w in qwords:
        if w in vocab:
            out.add(w)
            continue
        close = difflib.get_close_matches(w, vocab, n=1, cutoff=0.8)
        out.add(close[0] if close else w)
    return out


def _ranked(qwords: set, secs: list) -> list:
    scored = [(_score(qwords, t, b), t, b) for t, b in secs]
    scored = [row for row in scored if row[0] > 0]
    scored.sort(key=lambda row: -row[0])
    return scored


def search(query: str, section: str = "", limit: int = 3) -> str:
    """Return the whole matching mechanics section(s) as source text for the
    model to answer from - "=== Title ===\\n<prose>", ranked by query-word
    overlap, best first, capped at ~4000 chars / `limit` sections. `section`
    narrows to sections whose title contains it (case-insensitive). On an
    exact-word miss, retries once with each query word fuzzy-corrected against
    the corpus vocabulary. "" when nothing relevant matched (caller then falls
    through to the other corpora / web_search)."""
    qwords = _content_words(query)
    if not qwords:
        return ""
    secs = [(t, b) for t, b in _load() if not section or section.lower() in t.lower()]
    scored = _ranked(qwords, secs) or _ranked(_correct(qwords), secs)
    if not scored:
        return ""
    out, total = [], 0
    for _, t, b in scored[:limit]:
        block = f"=== {t} ===\n{b}"
        if out and total + len(block) > _MAX_CHARS:
            break
        out.append(block)
        total += len(block)
    return "\n\n".join(out)


def source_url(_section_title: str = "") -> str:
    """The one curated source behind every section - takes a title arg only to
    match orna_knowledge.source_url's shape at the call site."""
    return SOURCE_URL


def _demo() -> None:
    """Pins the facts the bot must be able to cite (factions, ascension,
    forging/adornment slots, quality, towers) and the search behaviour that
    keeps a real miss honest. Run: `python3 orna_mechanics.py`."""
    # The four factions, each with the correct effect, in one section.
    fac = search("factions element damage")
    for name in ("Earthen Legion", "Stormforce", "Knights of Inferno", "Frozenguard"):
        assert name in fac, (name, fac[:200])
    assert "+25%" in fac and "-20%" in fac, fac[:200]

    # Ascension is the +1%/level endgame axis, not an 11th tier.
    asc = search("ascension level")
    assert "+1%" in asc and "AL 100" in asc, asc[:200]
    assert "no tier 11" in search("tiers").lower() or "10 class tiers" in search("tiers").lower()

    # Forge levels + adornment-slot rule (b816bba's live-verified maxima).
    forge = search("masterforge demonforge godforge")
    assert "11" in forge and "12" in forge and "13" in forge, forge[:200]
    adorn = search("adornment slots")
    assert "godforged" in adorn.lower() and "16" in adorn, adorn[:200]

    # Quality tiers and towers each resolve to their own section.
    assert "Legendary" in search("item quality legendary ornate")
    towers = search("wild towers of olympia")
    assert any(t in towers for t in ("Selene", "Eos", "Prometheus")), towers[:200]

    # A real miss (no content word overlaps the corpus, fuzzy can't rescue it)
    # returns "" so the caller reports "not here" instead of noise. Uses
    # nonsense words on purpose: an ordinary English word like "mechanic" DOES
    # appear in the corpus, so it would (correctly) match.
    assert search("xyzzy plugh frobnicate") == "", search("xyzzy plugh frobnicate")
    # A section filter narrows to that section.
    assert "Kingdoms" in search("members", section="kingdom"), search("members", section="kingdom")[:80]

    # Fuzzy correction: a slightly-off spelling still lands.
    assert "Ascension" in search("ascention"), "fuzzy correction should recover 'ascention'"

    assert source_url() == SOURCE_URL
    print("orna_mechanics: all checks passed")


if __name__ == "__main__":
    _demo()
