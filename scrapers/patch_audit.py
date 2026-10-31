"""Go back through Orna's patch notes and correct what the knowledge base says against them.

    python3 -m scrapers.patch_audit            # judge every change not judged before, write corrections + report
    python3 -m scrapers.patch_audit --dry-run  # judge and print, write nothing

Changes come from the official patch notes (orna_releases, whose history file grows with every fetch) and the
patch-note posts on blog.ornarpg.com (2023-2025). For each change, the keyword index finds the corpus passages
about the same thing (never newer than the change, when the passage carries a date), and a free cloud model
(gemma4) says which of them the change makes WRONG and what is true now. Every conflict becomes:
- an entry in data/orna_corrections.txt (knowledge namespace "corrections", which outranks every other
  source - see orna_curated), re-indexed in Pinecone at the end; and
- a line in data/patch_audit_report.md, for a human to decide whether the old passage should be removed for
  good (scrapers/removed_content.py) - nothing is deleted automatically.
Verdicts are cached per change (data/cache/patch_audit.json): a re-run only judges new patch notes.
"""

from __future__ import annotations

import asyncio
import hashlib
import html
import json
import os
import re
import sys

import httpx

import paths
from knowledge import orna_curated, orna_pinecone, orna_textindex
from llm.ollama_client import OLLAMA_CLOUD_HOST, OllamaError, chat_json
from orna import orna_releases

JUDGE_MODEL = os.environ.get("PATCH_AUDIT_MODEL", "gemma4:31b")
CACHE = paths.CACHE / "patch_audit.json"
REPORT = paths.DATA / "patch_audit_report.md"
BLOG_API = "https://blog.ornarpg.com/wp-json/wp/v2/posts"
HITS = 4
_DATE_RE = re.compile(r"\[(20\d\d-\d\d-\d\d)\]|r/OrnaRPG (20\d\d-\d\d)|\((20\d\d-\d\d-\d\d)\)")
# An UNDATED passage (guides, sheets, Ornabook: scraped 2026-10, written over the last year or two) can be
# corrected only by a patch from the last year: a 2023 patch is older than the guide that already reflects it
# (first run: 2023 Hero of Aethric / Replica Titan notes "corrected" 2026 guides).
UNDATED_SINCE = os.environ.get("PATCH_AUDIT_UNDATED_SINCE", "2025-10-01")
_AUDIT_TITLE = re.compile(r"\(patch \d{4}-\d\d-\d\d\)$")


def _blog_changes() -> list:
    """The blog's patch-note posts, one change per bullet."""
    try:
        posts = httpx.get(BLOG_API, params={"search": "patch notes", "per_page": 100, "_fields": "date,title,link,content"},
                          headers={"User-Agent": "Mozilla/5.0"}, timeout=30).json()
    except Exception as e:
        print(f"blog: {e}", file=sys.stderr)
        return []
    out = []
    for p in posts:
        if "patch" not in p["title"]["rendered"].lower():
            continue
        for li in re.findall(r"<li[^>]*>(.*?)</li>", p["content"]["rendered"], re.S):
            text = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", li))).strip()
            if len(text) > 25:
                out.append({"date": p["date"][:10], "source": html.unescape(p["title"]["rendered"]), "url": p["link"], "text": text})
    return out


def changes() -> list:
    out = [{"date": n["date"], "source": f"{n['title']} ({n['kind']})", "url": orna_releases.RELEASES_URL,
            "text": re.sub(r"^(Changes|New features|Fixes|Bug fixes|Balance): ", "", i)}
           for n in orna_releases.all_notes() for i in n["items"]]
    return out + _blog_changes()


def _older(hit: dict, date: str) -> bool:
    """A passage the change can correct: undated, or dated before the change."""
    m = _DATE_RE.search(hit["text"][:300])
    d = next((g for g in m.groups() if g), "") if m else ""
    return d < date if d else date >= UNDATED_SINCE


_PROMPT = ("Orna game patch note, {date}:\n\"{change}\"\n\nKnowledge-base passages written before it:\n{hits}\n\n"
           "Which passages does this patch note make WRONG or out of date? Only a direct conflict counts (a number, "
           "rule, location, availability or mechanic that the patch changed or removed) - not a passage that is "
           "merely about the same topic. Answer as JSON: {{\"conflicts\": [{{\"passage\": <number>, \"old_claim\": "
           "\"<what the passage says, quoted or close>\", \"now_true\": \"<what is true after the patch>\"}}]}} - "
           "an empty list when nothing conflicts.")


async def judge(change: dict, hits: list) -> list:
    key = os.environ.get("OLLAMA_API_KEY")
    listing = "\n\n".join(f"[{i + 1}] ({h['ns']}: {h['title'][:60]})\n{h['text'][:1500]}" for i, h in enumerate(hits))
    try:
        out = await chat_json(OLLAMA_CLOUD_HOST, JUDGE_MODEL, [{"role": "user", "content": _PROMPT.format(
            date=change["date"], change=change["text"], hits=listing)}], {"Authorization": f"Bearer {key}"} if key else {})
    except OllamaError as e:
        print(f"  judge failed: {e}", file=sys.stderr)
        return None
    good = []
    for c in out.get("conflicts") or []:
        try:
            h = hits[int(c.get("passage")) - 1]
        except (TypeError, ValueError, IndexError):
            continue
        if str(c.get("now_true") or "").strip():
            good.append({"ns": h["ns"], "title": h["title"], "url": h["url"], "old": str(c.get("old_claim") or "")[:400],
                         "now": str(c["now_true"])[:400]})
    return good


async def main(dry: bool) -> None:
    cache = json.loads(CACHE.read_text()) if CACHE.exists() else {}
    # human review: [change text, passage title] pairs that are NOT a conflict - never written again
    rejected = {tuple(x) for x in cache.get("_rejected", [])}
    found = []
    todo = changes()
    print(f"{len(todo)} changes to check", file=sys.stderr)
    for ch in todo:
        k = hashlib.sha1(f"{ch['date']}|{ch['text']}".encode()).hexdigest()[:16]
        if k not in cache:
            hits = [h for h in await asyncio.to_thread(orna_textindex.search, ch["text"], HITS * 2)
                    if h["ns"] not in ("corrections", "verified") and _older(h, ch["date"])][:HITS]
            verdict = await judge(ch, hits) if hits else []
            if verdict is None:
                continue                       # not cached: tried again next run
            cache[k] = verdict
        for c in cache[k]:
            if (ch["text"], c["title"]) in rejected:
                continue
            found.append((ch, c))
            print(f"  {ch['date']} {ch['text'][:70]!r}\n      {c['ns']}: {c['title'][:50]} - was: {c['old'][:80]} | now: {c['now'][:80]}")
    if not dry:
        CACHE.write_text(json.dumps(cache, ensure_ascii=False, indent=1))
    print(f"{len(found)} conflicts", file=sys.stderr)
    if dry or not found:
        return
    # this run's findings replace the previous run's (entries titled "... (patch <date>)"); /correct entries stay
    orna_curated.save("corrections", [e for e in orna_curated.load("corrections") if not _AUDIT_TITLE.search(e[0])])
    by_change = {}
    for ch, c in found:
        by_change.setdefault((ch["date"], ch["text"], ch["source"], ch["url"]), []).append(c)
    for (date, text, source, url), cs in by_change.items():
        seen, lines = set(), []
        for c in cs:
            if (c["title"], c["old"]) not in seen:
                seen.add((c["title"], c["old"]))
                lines.append(f"Out of date - {c['title']} ({c['ns']}) said: {c['old']}\nNow: {c['now']}")
        body = f"Official patch note, {date} ({source}): {text}\n" + "\n".join(lines)
        orna_curated.add("corrections", f"{text[:80]} (patch {date})", body, url)
    REPORT.write_text("# Patch audit: knowledge the patch notes contradict\n\nEach was added to "
                      "data/orna_corrections.txt. Remove a passage for good with scrapers/removed_content.py.\n\n"
                      + "\n".join(f"- **{ch['date']}** {ch['text']}  \n  {c['ns']} · {c['title']}: _{c['old']}_ → {c['now']}"
                                  for ch, c in found) + "\n", encoding="utf-8")
    if orna_pinecone.enabled():
        print("corrections:", orna_pinecone.index_corpus("corrections"), "records indexed", file=sys.stderr)


if __name__ == "__main__":
    asyncio.run(main("--dry-run" in sys.argv))
