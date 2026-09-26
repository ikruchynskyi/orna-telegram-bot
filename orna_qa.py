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

Self-check: `python3 orna_qa.py`.
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

CORPUS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "orna_qa.txt")
# A hit in the question is what makes a thread relevant; the same word inside one
# answer may be an aside. Tuned on the real corpus - see _demo.
_TITLE_WEIGHT = 3
_QUESTION_WEIGHT = 2
_MIN_WORD_LEN = 3
_MAX_BLOCK_CHARS = 1100
# A thread must match the QUESTION side (title or Q: line), not merely share a
# word with one of its answers. Without this, a long answer that happens to
# contain "immunity" surfaced a thread about a completely different subject -
# and an irrelevant Q&A block is worse than none, because the model may trust
# it. The floor then requires more than one incidental word overall.
# TWO question-side hits, not one. With a small corpus a single shared word is
# almost always incidental ("how do I get the summoner class" matched "How'd
# someone get the wealth shrine" on how+get), and precision matters far more
# than recall here: a thread that is not about the question is worse than no
# thread at all, because the model may build an answer on it. Recall improves as
# the corpus grows - see the note in orna_scrape_qa about resuming the crawl.
_MIN_QUESTION_HITS = 2
_MIN_SCORE = 6
# Words appearing in more than this share of question texts carry no signal
# ("how", "get", "orna", "guide"). DERIVED from the corpus rather than a
# hardcoded stopword list, the same "discover, don't hardcode" approach
# orna_aussies uses for its field vocabularies - so it keeps working as the
# corpus grows and needs no English word list. Without it, "how do I get the
# summoner class" matched a thread titled "How'd someone get the wealth shrine"
# on `how`+`get` alone.
# 0.10 chosen by measurement on the real corpus, not guessed: it drops every
# interrogative ("how", "get", "what", "any", "this", "you", "the") while
# keeping all of summoner/vritra/amities/orns/followers/anguished/adornment/beo.
# 0.15 left "how" and "get" in, which is exactly what produced the bad matches.
_MAX_DOC_FREQ = 0.10

_HEADER_RE = re.compile(r"^=== (?P<title>.+?) \(r/OrnaRPG (?P<when>[\d-]+), (?P<score>\d+)up, (?P<url>[^)]+)\) ===$")
_WORD_RE = re.compile(r"[^\W_]+", re.UNICODE)

_THREADS: list | None = None
_COMMON_WORDS: frozenset | None = None


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


def _common_words() -> frozenset:
    """Question-side words too frequent to discriminate, from the corpus itself."""
    global _COMMON_WORDS
    if _COMMON_WORDS is None:
        threads = _load()
        if not threads:
            return frozenset()
        counts: dict = {}
        for th in threads:
            for w in set(_WORD_RE.findall(f"{th.title} {th.question}".lower())):
                if len(w) >= _MIN_WORD_LEN:
                    counts[w] = counts.get(w, 0) + 1
        cutoff = max(2, int(len(threads) * _MAX_DOC_FREQ))
        _COMMON_WORDS = frozenset(w for w, n in counts.items() if n > cutoff)
    return _COMMON_WORDS


def search(query: str, limit: int = 3) -> list:
    """Threads whose QUESTION best matches `query`, best first.

    Distinct-word overlap rather than whole-query substring, for the reason
    orna_knowledge learned the hard way: nobody phrases a question the way
    another person phrased theirs, and a multi-subject query matches no single
    substring anywhere."""
    threads = _load()
    words = {w for w in _WORD_RE.findall((query or "").lower()) if len(w) >= _MIN_WORD_LEN}
    if not words or not threads:
        return []
    # Drop the words every question uses. If that empties the query the ask was
    # entirely generic, and no thread is a real match.
    discriminating = words - _common_words()
    words = discriminating or set()
    if not words:
        return []
    scored = []
    for th in threads:
        title_w = set(_WORD_RE.findall(th.title.lower()))
        q_w = set(_WORD_RE.findall(th.question.lower()))
        a_w = set(_WORD_RE.findall(" ".join(th.answers).lower()))
        question_hits = len(words & (title_w | q_w))
        score = (_TITLE_WEIGHT * len(words & title_w)
                 + _QUESTION_WEIGHT * len(words & q_w)
                 + len(words & a_w))
        if question_hits >= _MIN_QUESTION_HITS and score >= _MIN_SCORE:
            scored.append((score, th.score, th))
    # Upvotes break a tie: between two equally-matching threads the one the
    # community engaged with more is the better one to hand back.
    scored.sort(key=lambda t: (-t[0], -t[1]))
    return [th for _s, _u, th in scored[:limit]]


def search_text(query: str, limit: int = 3) -> str:
    """`search` rendered as one labelled block for a tool observation."""
    hits = search(query, limit)
    if not hits:
        return ""
    parts = []
    for th in hits:
        body = th.text
        if len(body) > _MAX_BLOCK_CHARS:
            body = body[:_MAX_BLOCK_CHARS] + " …"
        parts.append(body)
    return "\n\n".join(parts)


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

    global _THREADS, _COMMON_WORDS
    _THREADS = threads
    _COMMON_WORDS = None
    # a question-side match must win over an answer-side one
    assert search("multiple followers")[0].url == "https://x/a"
    assert search("vritra charm immunity")[0].url == "https://x/b"
    # the vote counts and the dev flag must survive into the observation text
    text = search_text("multiple followers")
    assert "22up" in text and "6up DEV" in text and "24up" in text
    assert search("") == [] and search("of a") == []          # short words cannot match everything
    assert search("nonexistentsubject") == []
    # A word that appears ONLY inside an answer must not surface the thread: an
    # irrelevant Q&A block is worse than none. "summons" is in an answer of the
    # followers thread and in neither question.
    assert search("summons") == [], "an answer-only match must not qualify"
    assert search("multiple followers"), "a title match still qualifies"
    # ...and one incidental question word is not enough on its own
    assert search("immunity") == [], "a single weak hit is below the score floor"
    assert search("followers") == [], "one question word alone is not enough"
    _THREADS = None
    _COMMON_WORDS = None

    if os.path.exists(CORPUS_PATH):
        # On the real corpus, precision over recall: a query whose only overlap
        # with a thread is generic wording must return NOTHING. Verified against
        # the live corpus rather than asserted in the abstract.
        for generic in ("how do i get", "what is the best", "anyone know this"):
            assert search(generic) == [], f"{generic!r} matched something"
        assert _common_words(), "no common words derived - is the corpus empty?"
        real = _load()
        assert real, "corpus file exists but parsed to nothing"
        assert all(t.answers for t in real), "a thread with no answer must not be kept"
        print(f"orna_qa: all checks passed ({len(real)} threads, "
              f"{sum(len(t.answers) for t in real)} answers)")
    else:
        print("orna_qa: fixture checks passed (no corpus file yet)")


if __name__ == "__main__":
    _demo()
