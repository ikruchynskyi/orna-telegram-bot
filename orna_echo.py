#!/usr/bin/env python3
"""
orna_echo.py - read `orna_echo.txt` (playerecho.com/orna's 37 guides, built by
orna_scrape_echo.py) and search it.

WHY THIS SOURCE, next to four that already exist: it is the only one that states
the MECHANICS AND FORMULAS outright. The codex gives an entry's own numbers and
never a formula; the community sheets tabulate results; the reddit corpus has
devs explaining things in passing. This has `Ward_Base = (HP + MP) / 2`, Ward's
multiplicative per-slot gear scaling, Ascension's altar costs, dungeon
modes/cooldowns/godforging, the Circle of Anguish's proof paths, and per-EVENT
tier gates and rewards - the last being something `orna_calendar` (which knows
only WHICH events are live) has no content for at all.

SEARCHED AT SECTION LEVEL, not line level, and this is the whole reason it is a
separate module from `orna_knowledge`. That corpus is tabular: one row IS the
answer, so returning the matching line is right. These are written guides, where
the answer is a paragraph plus the formula it introduces - handing back only the
line containing "Ward" would strip the formula sitting two lines below it. Same
argument `orna_reddit` and `orna_guides` already make for their own corpora.

Not on a TTL: the file is committed and re-run by hand (see the scraper's
docstring). A missing file disables the corpus cleanly - logged, empty results -
so a checkout made before the first scrape still works, exactly like
`orna_reddit.txt`.

Self-check: `python3 orna_echo.py`.
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass

logger = logging.getLogger(__name__)

CORPUS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "orna_echo.txt")
SITE_URL = "https://playerecho.com/orna"
# A heading hit is worth more than a body hit: "## Ward Capacity: The Base
# Formula" is what the section is ABOUT, while the same word in a paragraph may
# be an aside. Tuned on the real corpus, not guessed - see _demo.
_HEADING_WEIGHT = 3
_TITLE_WEIGHT = 2
_MIN_WORD_LEN = 3
_MAX_BODY_CHARS = 1400

_ARTICLE_RE = re.compile(r"^=== (?P<title>.+?) \((?P<url>[^)]+)\) ===$")
_WORD_RE = re.compile(r"[^\W_]+", re.UNICODE)

_SECTIONS: list | None = None


@dataclass
class Section:
    """One `## Heading` block of one guide (or a guide's intro, heading "")."""
    title: str          # the article's title
    url: str
    heading: str
    body: str

    @property
    def label(self) -> str:
        return f"{self.title} - {self.heading}" if self.heading else self.title


def _parse(text: str) -> list:
    """Split the corpus into per-heading sections. A guide's text before its
    first `##` becomes a section with an empty heading, since the intro is
    often where the summary sentence lives."""
    out: list = []
    title = url = heading = ""
    body: list = []

    def flush():
        joined = "\n".join(body).strip()
        if title and joined:
            out.append(Section(title=title, url=url, heading=heading, body=joined))

    for line in text.splitlines():
        m = _ARTICLE_RE.match(line)
        if m:
            flush()
            title, url = m.group("title"), m.group("url")
            heading, body = "", []
            continue
        if line.startswith("## "):
            flush()
            heading, body = line[3:].strip(), []
            continue
        body.append(line)
    flush()
    return out


def _load() -> list:
    global _SECTIONS
    if _SECTIONS is None:
        try:
            with open(CORPUS_PATH, encoding="utf-8") as fh:
                _SECTIONS = _parse(fh.read())
        except FileNotFoundError:
            # Degrade, never raise: knowledge_search must keep working for the
            # other corpora on a checkout that predates the first scrape.
            logger.warning("orna_echo: %s not found - run orna_scrape_echo.py", CORPUS_PATH)
            _SECTIONS = []
        except Exception:
            logger.warning("orna_echo: could not read %s", CORPUS_PATH, exc_info=True)
            _SECTIONS = []
    return _SECTIONS


def search(query: str, limit: int = 3) -> list:
    """Sections mentioning `query`, best first, scored by how many DISTINCT
    query words appear - heading and title hits weighted above body hits.

    Distinct-word scoring (not whole-query substring) for the reason
    orna_knowledge learned the hard way: nobody phrases a question the way a
    guide phrases its heading, and a query naming several subjects at once
    ("ward absorption turns") matches no single substring anywhere."""
    sections = _load()
    words = {w for w in _WORD_RE.findall(query.lower()) if len(w) >= _MIN_WORD_LEN}
    if not words or not sections:
        return []
    scored = []
    for sec in sections:
        body_words = set(_WORD_RE.findall(sec.body.lower()))
        head_words = set(_WORD_RE.findall(sec.heading.lower()))
        title_words = set(_WORD_RE.findall(sec.title.lower()))
        score = (len(words & body_words)
                 + _HEADING_WEIGHT * len(words & head_words)
                 + _TITLE_WEIGHT * len(words & title_words))
        if score:
            scored.append((score, len(sec.body), sec))
    # Longest body breaks a tie: between two equally-matching sections the
    # fuller one is the better answer to hand a reading model.
    scored.sort(key=lambda t: (-t[0], -t[1]))
    return [sec for _score, _len, sec in scored[:limit]]


def search_text(query: str, limit: int = 3) -> str:
    """`search` rendered as one labelled block for a tool observation."""
    hits = search(query, limit)
    if not hits:
        return ""
    parts = []
    for sec in hits:
        body = sec.body if len(sec.body) <= _MAX_BODY_CHARS else sec.body[:_MAX_BODY_CHARS] + " …"
        parts.append(f"[{sec.label}]\n{body}")
    return "\n\n".join(parts)


def _demo() -> None:
    """Checks the parser on a fixture, then the real corpus if it is present."""
    fixture = (
        "=== Ward Guide: Capacity (https://playerecho.com/orna/ward-guide) ===\n"
        "How Ward works.\n"
        "\n"
        "## Ward Capacity: The Base Formula\n"
        "Base Ward is calculated from your stats:\n"
        "    Ward_Base = (HP + MP) / 2\n"
        "\n"
        "## Gear Scaling\n"
        "Bonuses are multiplicative.\n"
        "\n"
        "=== Fishing Guide (https://playerecho.com/orna/fishing) ===\n"
        "## Lines\n"
        "Use a better line.\n"
    )
    secs = _parse(fixture)
    assert [s.heading for s in secs] == ["", "Ward Capacity: The Base Formula", "Gear Scaling", "Lines"], secs
    assert secs[0].title == "Ward Guide: Capacity" and secs[0].url.endswith("/ward-guide")
    assert secs[3].title == "Fishing Guide"
    assert secs[1].label == "Ward Guide: Capacity - Ward Capacity: The Base Formula"

    global _SECTIONS
    _SECTIONS = secs
    # A heading word must outrank a body-only mention: "capacity" is in the
    # Ward section's heading and nowhere else.
    assert search("ward capacity formula")[0].heading == "Ward Capacity: The Base Formula"
    assert "Ward_Base = (HP + MP) / 2" in search_text("ward capacity formula")
    assert search("fishing line")[0].title == "Fishing Guide"
    assert search("") == [] and search("a") == []          # too-short words cannot match everything
    assert search("nonexistentsubject") == []
    _SECTIONS = None

    if os.path.exists(CORPUS_PATH):
        real = _load()
        assert len(real) > 300, f"only {len(real)} sections parsed from the real corpus"
        articles = {s.url for s in real}
        assert len(articles) >= 30, articles
        # The formula this corpus exists for must be reachable by an obvious
        # question, not just present in the file.
        hit = search_text("how is ward capacity calculated")
        assert "Ward_Base = (HP + MP) / 2" in hit, hit[:400]
        # ...and a mechanic the codex has no field for at all.
        assert search("ascension altar cost"), "ascension costs unreachable"
        assert search("dungeon cooldown"), "dungeon cooldowns unreachable"
        print(f"orna_echo: all checks passed ({len(real)} sections, {len(articles)} guides)")
    else:
        print("orna_echo: fixture checks passed (no corpus file yet)")


if __name__ == "__main__":
    _demo()
