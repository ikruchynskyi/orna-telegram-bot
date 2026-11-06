"""Player-published builds from aussiescodex's Build Library (https://www.aussiescodex.com/orna-builds), the raw
material for the build ARCHETYPES (data/orna_build_archetypes.txt): what mechanics community builds combine.

The page renders in the browser over two public JSON endpoints (verified 2026-11-06):
  /api/builds/public   every published build: id, name, class, specialization, level, AL, PvP flag, equipment ids,
                       description, votes
  /api/builds/<id>     one build: each slot's item, quality, upgrade level, element, anguish bonus, adornments;
                       amities when set. No pet or skill field.

    python3 -m scrapers.aussie_builds fetch     # the list, then the details of builds not cached yet
    python3 -m scrapers.aussie_builds --demo
"""

from __future__ import annotations

import json
import sys
import time

import paths

SITE = "https://www.aussiescodex.com"
CACHE = paths.CACHE / "aussie_builds"
PAUSE = 1.5     # seconds between detail requests


def fetch() -> int:
    """New builds fetched. The list is refetched every time (votes change); a detail only once."""
    import httpx
    from orna.orna_bonuses import HEADERS
    CACHE.mkdir(parents=True, exist_ok=True)
    lst = httpx.get(f"{SITE}/api/builds/public", headers=HEADERS, timeout=30).raise_for_status().json()
    (CACHE / "public.json").write_text(json.dumps(lst), encoding="utf-8")
    n = 0
    for b in lst.get("builds") or []:
        path = CACHE / f"{b['id']}.json"
        if path.exists():
            continue
        d = httpx.get(f"{SITE}/api/builds/{b['id']}", headers=HEADERS, timeout=30).raise_for_status().json()
        d["description"], d["votes"] = b.get("description"), b.get("votes")
        path.write_text(json.dumps(d), encoding="utf-8")
        n += 1
        time.sleep(PAUSE)
    return n


def load() -> list:
    """Every cached build, with its current vote count from the list."""
    try:
        votes = {b["id"]: b.get("votes") for b in json.loads((CACHE / "public.json").read_text())["builds"]}
    except (OSError, ValueError, KeyError):
        votes = {}
    out = []
    for p in sorted(CACHE.glob("*.json")):
        if p.name == "public.json":
            continue
        b = json.loads(p.read_text(encoding="utf-8"))
        b["votes"] = votes.get(b["id"], b.get("votes"))
        out.append(b)
    return out


def url(build_id: str) -> str:
    """A build opens in the Gear Simulator (the page's own "Copy build link")."""
    return f"{SITE}/orna-gear-simulator/{build_id}"


def _demo() -> None:
    builds = load()
    if builds:     # needs a fetch first
        assert all(b.get("selectedClass") and (b.get("equipment") or {}).get("weapon") for b in builds
                   if (b.get("equipment") or {}).get("weapon")), "a build without class"
        assert sum(1 for b in builds if (b.get("equipment") or {}).get("weapon")) >= len(builds) - 2
    print(f"aussie_builds: {len(builds)} builds cached - ok")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "fetch":
        print(fetch(), "new builds")
    elif cmd == "--demo":
        _demo()
    else:
        print(__doc__)
