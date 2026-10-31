"""Two small knowledge corpora the bot writes itself, both searched by knowledge_search (Pinecone namespaces
"corrections" and "verified", and the keyword index):

- data/orna_corrections.txt - where an official patch note (scrapers/patch_audit.py) or an admin (/correct)
  says the guides are out of date: what changed, when, and what is true now. Outranks every other source.
- data/orna_verified.txt - /orna answers a reviewer approved after players voted on them (/verify): a worked
  answer to a question that is likely to be asked again.

Format, one entry per section (the same "=== title ===" shape the other corpora use):
    === <title> ===
    url: <source url or empty>
    <body>
An entry's key is a hash of its title, so the same title replaces its entry and one entry can be upserted
into Pinecone without re-indexing the rest.
"""

from __future__ import annotations

import hashlib
import os

import paths

FILES = {"corrections": paths.DATA / "orna_corrections.txt", "verified": paths.DATA / "orna_verified.txt"}
HEADER = {"corrections": "# Corrections: what official patch notes (or an admin) say the guides get wrong. Written by\n"
                         "# scrapers/patch_audit.py and /correct - edit by hand freely, keep the section format.\n",
          "verified": "# Verified /orna answers: approved by an admin (/verify) after players voted on them.\n"}


def key(title: str) -> str:
    return hashlib.sha1(title.strip().lower().encode("utf-8")).hexdigest()[:12]


def load(name: str) -> list:
    """-> [(title, body, url)]; [] when the file doesn't exist yet."""
    path = FILES[name]
    if not path.exists():
        return []
    out = []
    for chunk in path.read_text(encoding="utf-8").split("\n=== ")[1:]:
        title_line, _, rest = chunk.partition("\n")
        title = title_line[:-4] if title_line.endswith(" ===") else title_line
        url = ""
        if rest.startswith("url:"):
            url_line, _, rest = rest.partition("\n")
            url = url_line[4:].strip()
        out.append((title.strip(), rest.strip(), url))
    return out


def add(name: str, title: str, body: str, url: str = "") -> str:
    """Add or replace (same title) one entry; atomic write. -> its key."""
    entries = [e for e in load(name) if key(e[0]) != key(title)] + [(title.strip(), body.strip(), url.strip())]
    text = HEADER[name] + "".join(f"\n=== {t} ===\nurl: {u}\n{b}\n" for t, b, u in entries)
    path = FILES[name]
    tmp = path.with_suffix(".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
    return key(title)


def units(name: str) -> list:
    """orna_pinecone's (head, body_lines, title, url, key) per entry - keyed, so one can be upserted alone."""
    label = "Correction" if name == "corrections" else "Verified answer"
    return [(f"[{label}: {t}]", b.split("\n"), t[:60], u, key(t)) for t, b, u in load(name)]


def _demo() -> None:
    import tempfile
    from pathlib import Path
    real = dict(FILES)
    with tempfile.TemporaryDirectory() as d:
        FILES["corrections"] = Path(d) / "c.txt"
        assert load("corrections") == []
        k = add("corrections", "Spelunking Guild", "Battle Dungeon focused since 2026-07-29.", "https://playorna.com/releases/")
        add("corrections", "Spelunking Guild", "Replaced.", "u")   # same title: replaces
        add("corrections", "Other", "Body\nline 2")
        assert load("corrections") == [("Spelunking Guild", "Replaced.", "u"), ("Other", "Body\nline 2", "")]
        assert units("corrections")[0][4] == k and units("corrections")[1][1] == ["Body", "line 2"]
    FILES.update(real)
    print("orna_curated: all checks passed")


if __name__ == "__main__":
    _demo()
