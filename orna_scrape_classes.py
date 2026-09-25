"""
orna_scrape_classes.py
======================
Regenerates `orna_classes.json` from aussiescodex's own stats-estimator
data - the per-class stat modifiers, bonus stats and passive effects that
back their UI.

Run (the URL changes on every one of their deploys, see below):

    python3 orna_scrape_classes.py https://www.aussiescodex.com/_next/static/chunks/218-<hash>.js

**This is NOT crawlable on a schedule, which is why the result is
committed.** The data lives inside a Next.js webpack chunk whose filename
carries a content hash (`218-cb72350c16e02252.js`); that hash changes
whenever they rebuild, so yesterday's URL 404s and there is no stable
address to poll. Finding the current one means loading the site and
reading its script tags. Same static-and-committed treatment as
orna_reddit.txt, for a different reason: that one is append-only history,
this one has no fetchable address.

Shape inside the chunk: three `JSON.parse('{...}')` literals, which is why
this needs no JS engine - they are extracted as text and parsed directly.
  1. absolute base stats for the 19 tier-10 specializations
     (Gilgamesh, Heretic, Realmshifter, Beowulf, Grand Summoner, Diety and
     their sub-specs) - hp/mana/attack/defense/magic/resistance/dexterity/
     foresight/crit/view_distance.
  2. bonusStats + passiveEffects for those same 19.
  3. the 40 regular classes, each with `tier`, `statModifiers` (PERCENT
     adjustments to hp/attack/...), `bonusStats` and `passiveEffects`.

Note aussiescodex spells it "Diety" - kept verbatim rather than corrected,
so a future re-run diffs cleanly against their data; orna_classes.py
handles the "deity" spelling at lookup time instead.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx

OUTPUT_PATH = Path(__file__).with_name("orna_classes.json")
HTTP_TIMEOUT = 30.0
HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                         "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36"}
_MARK = "JSON.parse('"


def extract_blobs(source: str) -> list:
    """Every `JSON.parse('...')` literal in the chunk, parsed.

    Scans for the closing quote rather than regexing, because the payload
    contains escaped quotes (`\\'`) that a non-greedy regex would stop on."""
    blobs, i = [], 0
    while True:
        i = source.find(_MARK, i)
        if i < 0:
            break
        start = i + len(_MARK)
        j = start
        while True:
            j = source.find("'", j)
            if j < 0 or source[j - 1] != "\\":
                break
            j += 1
        if j < 0:
            break
        try:
            blobs.append(json.loads(source[start:j].replace("\\'", "'")))
        except ValueError:
            pass                      # not every JSON.parse in a chunk is ours
        i = j + 1
    return blobs


def classify(blobs: list) -> dict:
    """Sort the blobs into {spec_stats, spec_extras, classes} by SHAPE, not
    position - a rebuild could reorder them, and silently mislabelling the
    absolute stat table as percent modifiers would poison every estimate."""
    out = {"spec_stats": {}, "spec_extras": {}, "classes": {}}
    for blob in blobs:
        if not isinstance(blob, dict) or not blob:
            continue
        sample = next(iter(blob.values()))
        if not isinstance(sample, dict):
            continue
        if "tier" in sample and "statModifiers" in sample:
            out["classes"] = blob
        elif "hp" in sample and "view_distance" in sample:
            out["spec_stats"] = blob
        elif "bonusStats" in sample or "passiveEffects" in sample:
            out["spec_extras"] = blob
    return out


def build(url: str) -> dict:
    resp = httpx.get(url, headers=HEADERS, timeout=HTTP_TIMEOUT)
    resp.raise_for_status()
    data = classify(extract_blobs(resp.text))
    missing = [k for k, v in data.items() if not v]
    if missing:
        raise ValueError(f"chunk did not yield {missing} - wrong chunk URL, or their bundle changed shape")
    data["_source"] = url
    return data


def main() -> None:
    if len(sys.argv) < 2:
        sys.exit(
            "usage: python3 orna_scrape_classes.py <chunk url>\n\n"
            "The URL is NOT stable - it carries a content hash that changes on every\n"
            "aussiescodex deploy. To find the current one: open\n"
            "https://www.aussiescodex.com/orna-stats-estimator, view source, and look for\n"
            "the /_next/static/chunks/<n>-<hash>.js script that contains \"statModifiers\"."
        )
    data = build(sys.argv[1])
    OUTPUT_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
    print(f"classes: {len(data['classes'])}, specializations: {len(data['spec_stats'])}")
    print(f"Wrote {OUTPUT_PATH} ({OUTPUT_PATH.stat().st_size} bytes)")


def _demo() -> None:
    """Pins extraction and shape-classification against the real literals,
    with no network. Run `python3 orna_scrape_classes.py --demo`."""
    src = (
        "let s=JSON.parse('{\"None\":{\"hp\":0,\"view_distance\":0},"
        "\"Gilgamesh\":{\"hp\":12509,\"attack\":1304,\"view_distance\":0}}'),"
        "x=JSON.parse('{\"Gilgamesh\":{\"bonusStats\":{\"ward_power\":50},\"passiveEffects\":[]}}'),"
        "y=JSON.parse('{\"Brawler\":{\"tier\":3,\"statModifiers\":{\"hp\":5},\"bonusStats\":{},"
        "\"passiveEffects\":[]},\"Duelist\":{\"tier\":5,\"statModifiers\":{\"dexterity\":25},"
        "\"bonusStats\":{},\"passiveEffects\":[\"Duelist Weapon Power (Dual Wield)\"]}}');"
    )
    blobs = extract_blobs(src)
    assert len(blobs) == 3, [type(b) for b in blobs]
    data = classify(blobs)
    # classified by shape, so order cannot mislabel absolute stats as percents
    assert data["spec_stats"]["Gilgamesh"]["hp"] == 12509
    assert data["spec_extras"]["Gilgamesh"]["bonusStats"]["ward_power"] == 50
    assert data["classes"]["Brawler"]["tier"] == 3
    assert data["classes"]["Duelist"]["passiveEffects"] == ["Duelist Weapon Power (Dual Wield)"]

    # an escaped quote inside a payload must not end the literal early
    tricky = """JSON.parse('{"a":{"tier":1,"statModifiers":{},"name":"it\\'s"}}')"""
    got = extract_blobs(tricky)
    assert got and got[0]["a"]["name"] == "it's", got
    print("orna_scrape_classes: all checks passed")


if __name__ == "__main__":
    if "--demo" in sys.argv:
        _demo()
    else:
        main()
