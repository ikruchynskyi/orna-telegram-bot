"""Monthly knowledge refresh: re-scrape every source, re-index what changed, check, report.

    python3 -m scrapers.refresh            # everything (an hour or more; the Discord threads step much longer)
    python3 -m scrapers.refresh --demo     # self-check, no network

Run by launchd on the 1st of each month (deploy/com.username.ornarefresh.plist). The corpora in data/ were
scraped once (2026-10-06) and nothing refreshed them, while the wikis and the game kept changing.

1. Re-scrape the corpora (sheets, playerecho, Ornabook, questline, class guides, item names, Ukrainian terms).
   A corpus that comes back more than MAX_SHRINK smaller is put back as it was and not indexed: that is a
   site outage or a changed page layout, not a month of edits.
2. Re-index each changed corpus in Pinecone (the keyword index rebuilds itself from the files).
3. The steps that keep their own delta: the patch-note audit, the Discord harvest + discussion threads, the
   Reddit delta (it needs a Reddit login - see reddit_delta; it fails until there is one, and the rest goes on).
4. The deterministic checks + the retrieval benchmark (tier 0, Pinecone on).
5. A report: data/cache/logs/refresh-<date>.md - per step ok/failed and time, and `git diff --stat` of data/.
   Nothing is committed: read the diff and commit it (the corpora are reviewed, like the scrapers say).
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import paths

ROOT = Path(__file__).resolve().parent.parent
MAX_SHRINK = 0.3
# (module and args, corpus files it writes, Pinecone namespace to re-index when they change)
SCRAPES = [
    (["scrapers.orna_scrape_knowledge"], ["orna_knowledge.txt"], "knowledge"),
    (["scrapers.orna_scrape_echo"], ["orna_echo.txt"], "echo"),
    (["scrapers.orna_scrape_ornabook"], ["orna_ornabook.txt"], "ornabook"),
    (["scrapers.orna_scrape_questline"], ["orna_questline.txt"], "questline"),
    (["scrapers.orna_scrape_guides"], [], None),                 # class_guide reads them from disk
    (["scrapers.orna_scrape_material_names"], ["orna_material_names_uk.json", "orna_item_names_uk.json"], None),
    (["scrapers.orna_scrape_terms_uk"], ["orna_terms_uk.json"], None),
]
SELF_INDEXING = [
    ["scrapers.patch_audit"],
    ["knowledge.orna_discord_search", "harvest"],
    ["knowledge.orna_pinecone", "discord"],
    ["scrapers.discord_threads"],
    ["scrapers.reddit_delta"],
]
TIMEOUT = {"scrapers.discord_threads": 24 * 3600}


def _run(args: list, log, env=None) -> tuple:
    """-> (ok, seconds). Output goes to the log file."""
    t0 = time.time()
    log.write(f"\n$ python3 -m {' '.join(args)}\n")
    log.flush()
    try:
        r = subprocess.run([sys.executable, "-m", *args], cwd=ROOT, stdout=log, stderr=subprocess.STDOUT,
                           timeout=TIMEOUT.get(args[0], 3 * 3600), env=env)
        ok = r.returncode == 0
    except subprocess.TimeoutExpired:
        ok = False
    return ok, time.time() - t0


def shrank(before: int, after: int) -> bool:
    return before > 0 and after < before * (1 - MAX_SHRINK)


def _restore(name: str) -> None:
    subprocess.run(["git", "checkout", "--", f"data/{name}"], cwd=ROOT, check=False)


def main() -> None:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
    logs = paths.CACHE / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    day = time.strftime("%Y-%m-%d")
    rows = []
    with open(logs / f"refresh-{day}.log", "a", encoding="utf-8") as log:
        for args, files, ns in SCRAPES:
            sizes = {f: (paths.DATA / f).stat().st_size if (paths.DATA / f).exists() else 0 for f in files}
            mtimes = {f: (paths.DATA / f).stat().st_mtime if (paths.DATA / f).exists() else 0 for f in files}
            ok, secs = _run(args, log)
            note = ""
            changed = [f for f in files if (paths.DATA / f).exists() and (paths.DATA / f).stat().st_mtime != mtimes[f]]
            for f in changed:
                if shrank(sizes[f], (paths.DATA / f).stat().st_size):
                    _restore(f)
                    note += f" {f} shrank >{MAX_SHRINK:.0%} - put back;"
                    ok = False
            if ok and ns and changed:
                ok2, s2 = _run(["knowledge.orna_pinecone", ns], log)
                secs += s2
                note += " re-indexed" if ok2 else " RE-INDEX FAILED"
            rows.append((" ".join(args), ok, secs, note.strip() or ("changed" if changed else "no change")))
        for args in SELF_INDEXING:
            ok, secs = _run(args, log)
            rows.append((" ".join(args), ok, secs, ""))
        ok, secs = _run(["tests.orna_test_suite"], log, env={**os.environ, "TIER": "0", "PINECONE": "1"})
        rows.append(("checks (tier 0 + retrieval benchmark)", ok, secs, ""))
    diff = subprocess.run(["git", "diff", "--stat", "--", "data/"], cwd=ROOT, capture_output=True, text=True).stdout
    report = (f"# Knowledge refresh {day}\n\n| step | result | time | note |\n|---|---|---|---|\n"
              + "".join(f"| {name} | {'ok' if ok else '**FAILED**'} | {secs / 60:.0f} min | {note} |\n"
                        for name, ok, secs, note in rows)
              + f"\nFull output: refresh-{day}.log\n\n## Changed corpora (not committed)\n\n```\n{diff or 'none'}\n```\n")
    (logs / f"refresh-{day}.md").write_text(report, encoding="utf-8")
    print(report)


def _demo() -> None:
    assert shrank(1000, 600) and not shrank(1000, 800) and not shrank(0, 10) and not shrank(1000, 1500)
    assert all((ROOT / (m[0].replace(".", "/") + ".py")).exists() for m, _f, _n in SCRAPES)
    assert all((ROOT / (m[0].replace(".", "/") + ".py")).exists() for m in SELF_INDEXING)
    from knowledge import orna_pinecone
    assert {n for _m, _f, n in SCRAPES if n} <= set(orna_pinecone.NAMESPACES)
    print("refresh: all checks passed")


if __name__ == "__main__":
    _demo() if "--demo" in sys.argv else main()
