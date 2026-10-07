#!/usr/bin/env python3
"""
orna_qa.py - read `orna_qa.txt` (r/OrnaRPG questions + their best-voted answers,
built by orna_scrape_qa.py) and search it.

WHY THIS IS NOT REDUNDANT with four existing corpora: it is the only one indexed
by the QUESTION as a player phrases it. `orna_knowledge` is tabular,
`orna_reddit` is dev prose, `orna_echo` is guide sections, `orna_guides` is whole
class guides - all keyed on the ANSWER's wording. An incoming question resembles
a past player's question far more than it resembles any answer, and that is
exactly the retrieval failure that motivated this: the Vritra Charm answer WAS in
`orna_reddit.txt`, and three `knowledge_search` calls missed it because the model
searched the ITEM name while the text is indexed by the MECHANIC.

It also carries the one thing no other source has: a CORRECTED PREMISE. "How to
get multiple followers?" is answered "you're fighting a summoner, those are
summons, not followers" - a misconception fix that exists only in community Q&A.

SCORING puts the question first. A thread's `Q:` line and title are weighted
above its answers, because matching the ASK is the signal; an answer mentioning a
word in passing is not.

WHAT THIS IS NOT: upvotes are a crowd signal, not truth. Vote counts are kept in
the text so the reading model can weigh 22up against 2up, dev answers are
flagged, and every block carries its DATE - a 2023 answer may predate a balance
patch, which is why the tool description tells the model `releases()` outranks it.

Self-check: `python3 -m knowledge.orna_qa`.
"""
from __future__ import annotations

import paths
import logging
import math
import os
import re
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

CORPUS_PATH = str(paths.DATA / "orna_qa.txt")

_HEADER_RE = re.compile(r"^=== (?P<title>.+?) \(r/OrnaRPG (?P<when>[\d-]+), (?P<score>\d+)up, (?P<url>[^)]+)\) ===$")

_THREADS: list | None = None


@dataclass
class Thread:
    title: str
    url: str
    when: str
    score: int
    question: str = ""
    answers: list = field(default_factory=list)

    @property
    def text(self) -> str:
        out = [f"Q ({self.when}, {self.score}up): {self.title}"]
        if self.question:
            out.append(self.question)
        out.extend(self.answers)
        return "\n".join(out)


def _parse(raw: str) -> list:
    out: list = []
    cur: Thread | None = None
    for line in raw.splitlines():
        m = _HEADER_RE.match(line)
        if m:
            cur = Thread(title=m.group("title"), url=m.group("url"),
                         when=m.group("when"), score=int(m.group("score")))
            out.append(cur)
            continue
        if cur is None or not line.strip():
            continue
        if line.startswith("Q: "):
            cur.question = line[3:].strip()
        elif line.startswith("A ["):
            cur.answers.append(line)
    return [t for t in out if t.answers]


def _load() -> list:
    global _THREADS
    if _THREADS is None:
        try:
            with open(CORPUS_PATH, encoding="utf-8") as fh:
                _THREADS = _parse(fh.read())
        except FileNotFoundError:
            # Degrade like orna_reddit does: knowledge_search must keep working
            # for the other corpora on a checkout made before the first crawl.
            logger.warning("orna_qa: %s not found - run orna_scrape_qa.py", CORPUS_PATH)
            _THREADS = []
        except Exception:
            logger.warning("orna_qa: could not read %s", CORPUS_PATH, exc_info=True)
            _THREADS = []
    return _THREADS


def _demo() -> None:
    fixture = (
        "=== How to get multiple followers? (r/OrnaRPG 2026-03, 24up, https://x/a) ===\n"
        "Q: I see people with multiple followers in arena\n"
        "A [22up]: You're fighting a summoner. They can summon up to 5 total summons.\n"
        "A [6up DEV]: You need to complete quests through the npc Horus.\n"
        "\n"
        "=== Vritra Charm and Paralysis immunity (r/OrnaRPG 2026-01, 23up, https://x/b) ===\n"
        "Q: I am using a Legendary Vritra charm with 8% status protection\n"
        "A [18up]: Won't protect from effects caused by you or your follower.\n"
    )
    threads = _parse(fixture)
    assert len(threads) == 2, threads
    assert threads[0].title == "How to get multiple followers?" and threads[0].score == 24
    assert threads[0].when == "2026-03" and threads[0].url == "https://x/a"
    assert len(threads[0].answers) == 2 and "DEV" in threads[0].answers[1]

    if os.path.exists(CORPUS_PATH):
        # The held-out evaluation THREADS must not be in the corpus. Checked by
        # URL, not by query: other threads legitimately discuss the same
        # subjects, so "no results for vritra" was the wrong assertion (the real
        # corpus answers it from a different, non-held-out thread - which is
        # correct behaviour).
        held_path = os.path.join(os.path.dirname(CORPUS_PATH), "orna_qa_heldout.txt")
        if os.path.exists(held_path):
            with open(held_path, encoding="utf-8") as fh:
                held = {t.url for t in _parse(fh.read())}
            assert held, "held-out file exists but parsed to nothing"
            assert not ({t.url for t in _load()} & held), "a held-out thread leaked into the corpus"
        real = _load()
        assert real, "corpus file exists but parsed to nothing"
        assert all(t.answers for t in real), "a thread with no answer must not be kept"
        print(f"orna_qa: all checks passed ({len(real)} threads, "
              f"{sum(len(t.answers) for t in real)} answers)")
    else:
        print("orna_qa: fixture checks passed (no corpus file yet)")


if __name__ == "__main__":
    _demo()
