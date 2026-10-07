"""One local full-text index (SQLite FTS5, BM25) over every knowledge_search corpus.

knowledge_search's retrieval when Pinecone is off or failing - one engine
replacing the five per-corpus word scorers (orna_knowledge / mechanics / echo /
qa / reddit) it used to fall back to. Measured 2026-10-06 on the suite's
retrieval benchmark: 8/13, the same as those five (Pinecone: 12/13); it misses
the paraphrased questions, as any keyword search does.

Also tried, and NOT adopted: merging it into Pinecone's ranking (hybrid,
reciprocal-rank fusion) to catch exact words dense vectors miss. It changed
nothing measured (8/8 exact-name questions either way, 12/13 benchmark) and
grew results ~1k chars. The miss that motivated it ("my pet keeps dying") ranks
the Followers section 18th here too.

The rows are orna_pinecone.records(ns) - the same chunks and ids as Pinecone.
Gitignored file, rebuilt automatically (well under a second) when any source is
newer than it; `python3 orna_textindex.py build` forces it. Read connections
are mode=ro, as for codex.sqlite3.
"""
from __future__ import annotations

import logging
import os
import re
import sqlite3
import sys
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

ROOT = Path(__file__).parent
DB_PATH = ROOT / ".textindex.sqlite3"
_lock = threading.Lock()

# Words that match everything and rank nothing. BM25 already discounts common
# words, but a question's filler ("how do I ...") would still pull in every row.
_STOP = set("""a an the and or of to in on for with at by from is are was were be been do does did how what
which who why when where can could should would will i my me you your we it its this that these those there
their they get got use any some all about into out up more most much many orna rpg game""".split())
_WORD_RE = re.compile(r"[^\W_]+", re.UNICODE)


def _sources() -> list:
    """Every file the index is built from - its mtimes decide staleness."""
    out = list(ROOT.glob("orna_*.txt")) + [ROOT / "orna_classes.json"]
    for d in (".knowledge_cache", ".discord_cache"):
        out += list((ROOT / d).glob("*")) if (ROOT / d).is_dir() else []
    return [p for p in out if p.exists()]


def _stale() -> bool:
    if not DB_PATH.exists():
        return True
    built = DB_PATH.stat().st_mtime
    return any(p.stat().st_mtime > built for p in _sources())


def build() -> int:
    """Rebuild from every namespace's records. Atomic (temp + os.replace) and
    refuses an empty result, like every cache here."""
    import orna_pinecone
    rows = []
    for ns in orna_pinecone.NAMESPACES:
        try:
            rows += [(r["_id"], ns, r["title"], r["url"], r["text"]) for r in orna_pinecone.records(ns)]
        except Exception:
            logger.warning("textindex: %s could not be read, left out", ns, exc_info=True)
    if not rows:
        raise RuntimeError("textindex: no records at all - not replacing the index")
    tmp = DB_PATH.with_suffix(".tmp")
    tmp.unlink(missing_ok=True)
    con = sqlite3.connect(tmp)
    con.execute("CREATE VIRTUAL TABLE chunks USING fts5(id UNINDEXED, ns UNINDEXED, title, url UNINDEXED, "
                "text, tokenize='porter unicode61 remove_diacritics 2')")
    con.executemany("INSERT INTO chunks VALUES (?,?,?,?,?)", rows)
    con.commit()
    con.close()
    os.replace(tmp, DB_PATH)
    return len(rows)


def _ensure() -> None:
    with _lock:
        if _stale():
            n = build()
            logger.info("textindex: rebuilt, %d chunks", n)


def match_query(query: str) -> str:
    """The query's content words as an FTS5 OR-query, each quoted so FTS
    syntax in user text ("AND", "-", quotes) is never interpreted."""
    words = [w for w in _WORD_RE.findall(query.lower()) if len(w) > 1 and w not in _STOP]
    return " OR ".join(f'"{w}"' for w in dict.fromkeys(words))


def search(query: str, limit: int = 20) -> list:
    """Best BM25 matches, best first: [{"_id","ns","title","url","text","rank"}].
    Title matches weigh 3x body matches."""
    q = match_query(query)
    if not q:
        return []
    _ensure()
    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    try:
        rows = con.execute("SELECT id, ns, title, url, text, bm25(chunks, 3.0, 1.0) AS r FROM chunks "
                           "WHERE chunks MATCH ? ORDER BY r LIMIT ?", (q, limit)).fetchall()
    finally:
        con.close()
    return [{"_id": i, "ns": ns, "title": t, "url": u, "text": x, "rank": r} for i, ns, t, u, x, r in rows]


def _demo() -> None:
    assert match_query("How do I keep my pet alive?") == '"keep" OR "pet" OR "alive"'
    assert match_query('ward AND "capacity" -x') == '"ward" OR "capacity"'
    assert match_query("how do I") == ""
    hits = search("pet keeps dying keep it alive")
    assert hits and any("Follower" in h["text"] or "pet" in h["text"].lower() for h in hits[:5]), hits[:2]
    assert search("xyzzy plugh frobnicate") == []
    print("orna_textindex: _demo ok")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    if sys.argv[1:] == ["build"]:
        print(build(), "chunks")
    else:
        _demo()
