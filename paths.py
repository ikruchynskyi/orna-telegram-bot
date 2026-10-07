"""Where every file the bot reads or writes lives. One place, so a module never
builds a path from its own location or the working directory.

    data/            committed corpora (*.txt) and static tables (*.json)
    data/qa_crawl/   the raw r/OrnaRPG crawl - committed, expensive to redo
    data/cache/      refetchable caches and derived indexes (gitignored)
    data/state/      live runtime state: reminders, amities, stats, browser
                     logins (gitignored; back these up, they are not refetchable)
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
QA_CRAWL = DATA / "qa_crawl"
CACHE = DATA / "cache"
STATE = DATA / "state"

for _d in (CACHE, STATE):
    _d.mkdir(parents=True, exist_ok=True)
