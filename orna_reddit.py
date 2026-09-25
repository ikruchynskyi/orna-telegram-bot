"""
orna_reddit.py
==============
Reader for `orna_reddit.txt` - Orna's developers writing on Reddit (see
orna_scrape_reddit.py for how it's built and why it isn't re-crawled).

Searched at ENTRY level, not line level, which is the whole reason this
isn't just another section inside orna_knowledge.txt. That corpus is
tabular: one row IS the complete answer ("Shrine of Luck | ... | 2"), so
returning the matching line is right. A developer explaining why orn
bonus multiplies instead of adds is a paragraph, and handing back the one
line containing "multiplicative" would strip the reasoning around it -
the same argument orna_guides.py makes for keeping long-form guides whole.

Missing file is not an error: the corpus is committed, but a checkout
without it (or before the first scrape) should degrade to "no reddit
results" rather than breaking knowledge_search for everyone.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

DATA_PATH = Path(__file__).with_name("orna_reddit.txt")
_entries: Optional[list] = None

# "[2023-11-14] u/OrnaOdie in r/OrnaRPG - re: How does orn bonus stack?"
_HEAD_RE = re.compile(r"^\[(\d{4}-\d{2}-\d{2})\]\s+u/(\S+)")


class Entry:
    __slots__ = ("head", "url", "body", "section")

    def __init__(self, head: str, url: str, body: str, section: str):
        self.head, self.url, self.body, self.section = head, url, body, section

    @property
    def text(self) -> str:
        return f"{self.head}\n{self.url}\n{self.body}" if self.url else f"{self.head}\n{self.body}"


def _load() -> list:
    """Every entry in the corpus, newest first within each section."""
    global _entries
    if _entries is not None:
        return _entries
    if not DATA_PATH.exists():
        logger.info("orna_reddit: %s not present - reddit corpus disabled", DATA_PATH.name)
        _entries = []
        return _entries

    entries = []
    text = DATA_PATH.read_text(encoding="utf-8")
    for chunk in text.split("\n=== ")[1:]:
        title_line, _, rest = chunk.partition("\n")
        section = title_line[:-4] if title_line.endswith(" ===") else title_line
        # Entries are separated by a blank line; the scraper collapses runs of
        # blank lines INSIDE an entry to exactly one, so a split on a blank
        # line alone would cut paragraphs apart. Anchor on the header instead.
        for block in rest.split("\n\n"):
            block = block.strip("\n")
            if not block:
                continue
            lines = block.split("\n")
            if not _HEAD_RE.match(lines[0]):
                # a continuation paragraph of the previous entry
                if entries:
                    entries[-1].body += "\n\n" + block
                continue
            head = lines[0]
            url = lines[1] if len(lines) > 1 and lines[1].startswith("http") else ""
            body = "\n".join(lines[2:] if url else lines[1:]).strip()
            entries.append(Entry(head, url, body, section))
    _entries = entries
    return entries


def search(query: str, limit: int = 4) -> list:
    """Entries mentioning `query`, best first. Scored by how many distinct
    query words appear, so a multi-word ask still ranks the entry that
    covers most of it - the same shape orna_knowledge._search_words uses,
    for the same reason (nobody phrases a question the way a comment is
    written)."""
    entries = _load()
    q = query.strip().lower()
    if not q or not entries:
        return []
    words = {w for w in re.findall(r"[^\W_]+", q) if len(w) > 2}
    scored = []
    for e in entries:
        hay = f"{e.head}\n{e.body}".lower()
        if q in hay:
            scored.append((100, e))          # exact phrase always wins
        elif words:
            hits = sum(1 for w in words if w in hay)
            if hits >= max(2, len(words) // 2):
                scored.append((hits, e))
    scored.sort(key=lambda t: -t[0])
    return [e for _s, e in scored[:limit]]


def format_entries(entries: list, max_chars: int = 2000) -> str:
    """Entries as text for the model, truncated as a whole rather than per
    entry so one long comment can't crowd the rest out silently."""
    out, used = [], 0
    for e in entries:
        block = e.text
        if used + len(block) > max_chars:
            block = block[: max(0, max_chars - used)] + " …"
            if block.strip():
                out.append(block)
            break
        out.append(block)
        used += len(block)
    return "\n\n".join(out)


def _demo() -> None:
    """Parses a fixture in the exact shape orna_scrape_reddit writes, so the
    reader and writer can't drift. Run `python3 orna_reddit.py`."""
    global _entries, DATA_PATH
    sample = (
        "# header comment\n"
        "\n=== u/OrnaOdie comments (reddit, developer commentary) ===\n"
        "[2023-11-14] u/OrnaOdie in r/OrnaRPG - re: How does orn bonus stack?\n"
        "https://www.reddit.com/r/OrnaRPG/comments/abc/x/\n"
        "Orn bonus from gear is multiplicative, not additive.\n"
        "\n"
        "So two 10% pieces give 1.21, not 1.2.\n"
        "\n"
        "[2022-01-02] u/OrnaOdie in r/OrnaRPG - re: Ward question\n"
        "https://www.reddit.com/r/OrnaRPG/comments/def/y/\n"
        "Ward absorbs magic damage before HP does.\n"
    )
    import tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as fh:
        fh.write(sample)
        tmp = Path(fh.name)
    orig, DATA_PATH, _entries = DATA_PATH, tmp, None
    try:
        entries = _load()
        assert len(entries) == 2, [e.head for e in entries]
        # the continuation paragraph must stay attached to its entry, not
        # become a third one
        assert "1.21" in entries[0].body, entries[0].body
        assert entries[0].url.endswith("/abc/x/")
        assert entries[1].head.startswith("[2022-01-02]")

        hits = search("orn bonus multiplicative")
        assert hits and "multiplicative" in hits[0].body, [h.head for h in hits]
        assert search("ward magic damage"), "multi-word ask must match the ward entry"
        assert search("completely unrelated zzzz") == []

        text = format_entries(hits)
        assert "reddit.com" in text and "1.21" in text, text
        # truncation is whole-corpus, and marked
        assert format_entries(hits, max_chars=40).endswith("…")
    finally:
        DATA_PATH, _entries = orig, None
        tmp.unlink(missing_ok=True)

    # a missing corpus disables cleanly instead of raising
    DATA_PATH, _entries = Path("/nonexistent/orna_reddit.txt"), None
    assert search("anything") == []
    DATA_PATH, _entries = orig, None
    print("orna_reddit: all checks passed")


if __name__ == "__main__":
    _demo()
