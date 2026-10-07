"""Build `orna_ornabook.txt` from Ornabook (book.cadelabs.ovh), a community-
written mdBook guide to Orna's mechanics: gear bonuses, hybrid builds, status
effect magnitudes, dungeons and key multipliers, raids, kingdoms, Anguish 2.0.

Output is the `=== Title (url) ===` / `## Heading` format that orna_echo's
section reader parses, so the same reader serves both corpora
(orna_echo._load(path=ORNABOOK_PATH)) - no second parser.

Run: python3 -m scrapers.orna_scrape_ornabook          crawl + write the corpus
     python3 -m scrapers.orna_scrape_ornabook --demo   self-check, no network

WHAT IS NOT OBVIOUS ABOUT THIS SITE
  * The most important tables are written in ICONS. The status-effect table's
    column headers are images (`td_tmp.png`, `su_tmp.png`, ...) and its row
    labels are images too (`def.png` `atk.png`), so its text alone is
    "-100% | -80% | -20% | +25%" - numbers with no stated meaning, which a
    reading model would quote confidently and wrongly. _icon_label() turns each
    icon back into the game's own notation: `td_tmp` -> "T. ↓↓↓",
    `atk_tu_tmp` -> "T. Att ↑↑↑". That is the same notation aussiescodex uses
    for status codes, so it lines up with the codex vocabulary.
  * Empty cells are POSITIONAL. 229 rows have one, and orna_scrape_echo's
    _table_rows drops empties - which shifts every later value one column
    left. Kept here as "—".
  * Crawled per CHAPTER page, not via mdBook's one-page print.html: in
    print.html the <h1>s are sections and repeated ids get "-1" suffixes that
    do not exist on the real pages, so every citation built from it would
    point at an anchor that is not there.
  * Some chapters are unfinished on the site itself ("(TODO)" headings with no
    body). Those produce no section - the corpus says nothing rather than an
    empty heading the model might read as "the guide says nothing applies".
  * 16 images are real infographics (exploration-dungeon maps, a dual-wield
    ratio plot). Their content cannot be crawled; each is marked in the text
    as "[image not captured: ...]" so a reader knows something is missing
    rather than assuming the prose is complete.

robots.txt (checked 2026-10-05): Cloudflare's content-signal preamble only,
no Content-Signal values and no Disallow - by its own rule (c), unset signals
neither grant nor restrict. The crawler identifies itself and waits
DELAY_SECONDS between pages. Contributors are credited on the site; every
section here carries its page URL so answers cite the source.
"""

from __future__ import annotations

import paths
import os
import re
import sys
import time
from urllib.parse import urljoin

from bs4 import BeautifulSoup, NavigableString, Tag

# Reused, not copied: the encoding-safe fetch (the site-without-charset bug
# orna_echo hit), whitespace cleaning, and the mojibake guard.
from scrapers.orna_scrape_echo import DELAY_SECONDS, _MOJIBAKE_RE, _clean, _fetch_text

BASE = "https://book.cadelabs.ovh/"
TOC_URL = BASE + "TableOfContents.html"
OUT_PATH = str(paths.DATA / "orna_ornabook.txt")
# Pages with no game content.
_SKIP_PAGES = {"TableOfContents.html", "Contributors.html", "print.html"}
EMPTY_CELL = "—"

# ---------------------------------------------------------------------------
# Icons -> text
# ---------------------------------------------------------------------------

_STAT_NAMES = {
    "atk": "Att", "def": "Def", "res": "Res", "mag": "Mag", "dex": "Dex",
    "crit": "Crit", "all": "All stats", "berserk": "Berserk", "target": "Target",
}
# [stat_]{t|d|s}{u|d}[_tmp]: magnitude (triple/double/single), direction
# (up/down), and whether it is a TEMPORARY (in-battle, "T.") effect.
_ARROW_RE = re.compile(r"^(?:(?P<stat>[a-z]+)_)?(?P<mag>[tds])(?P<dir>[ud])(?P<tmp>_tmp)?$")
_MAGNITUDE = {"t": 3, "d": 2, "s": 1}


def _icon_label(src: str) -> str:
    """An mdbook icon's file name -> what it means, in game notation."""
    stem = re.sub(r"\.(png|svg|gif|webp)$", "", src.rsplit("/", 1)[-1].lower())
    m = _ARROW_RE.match(stem)
    if m and (m.group("stat") is None or m.group("stat") in _STAT_NAMES):
        arrows = ("↑" if m.group("dir") == "u" else "↓") * _MAGNITUDE[m.group("mag")]
        stat = _STAT_NAMES.get(m.group("stat") or "", "")
        prefix = "T. " if m.group("tmp") else ""
        return f"{prefix}{stat + ' ' if stat else ''}{arrows}"
    if stem in _STAT_NAMES:
        return _STAT_NAMES[stem]
    # Anything else (status afflictions, items, places) is named by its file:
    # "poisoned" -> "Poisoned", "theta_charon_riftlock" -> "Theta Charon
    # Riftlock". Approximate, but a readable approximation beats a blank.
    return stem.replace("_", " ").strip().title()


def _decode_icons(root: Tag) -> None:
    """Replace every image in place: an inline icon becomes "[label]", a real
    picture becomes a visible "not captured" marker. In place, so every later
    get_text() - tables included - already sees the text."""
    for img in root.find_all("img"):
        src = img.get("src", "")
        if "mdbook-icon" in (img.get("class") or []):
            img.replace_with(f" [{_icon_label(src)}] ")
        else:
            what = img.get("alt") or src.rsplit("/", 1)[-1]
            img.replace_with(f" [image not captured: {what}] ")


# ---------------------------------------------------------------------------
# LaTeX -> readable math
# ---------------------------------------------------------------------------
# The book writes its formulas in MathJax LaTeX, which arrives as source:
#   \[ \text{multiplier}_{\text{dual-wield}}\ = (1 + 0.65 * B)^{2} \]
# Formulas are the most valuable lines a guide has (orna_echo's first parser
# lost every one of them), so they are turned into plain math rather than
# left as markup a reader has to decode.
_TEX_TEXT = re.compile(r"\\(?:text|mathrm|textbf|mathit|operatorname)\s*\{([^{}]*)\}")
_TEX_FRAC = re.compile(r"\\frac\s*\{([^{}]*)\}\s*\{([^{}]*)\}")
_TEX_SUP = re.compile(r"\^\s*\{([^{}]*)\}")
_TEX_SUB = re.compile(r"_\s*\{([^{}]*)\}")


def _tex(line: str) -> str:
    if "\\" not in line:
        return line
    prev = None
    while prev != line:  # innermost first: a \text inside a \frac's argument
        prev = line
        line = _TEX_TEXT.sub(r"\1", line)
        line = _TEX_SUP.sub(r"^\1", line)
        line = _TEX_SUB.sub(r"_\1", line)
        line = _TEX_FRAC.sub(r"(\1) / (\2)", line)
    line = re.sub(r"\\[\[\]()]", " ", line)                 # \[ \] \( \)
    line = line.replace("\\times", "×").replace("\\cdot", "·").replace("\\\\", " ")
    line = re.sub(r"\\ ", " ", line)                         # "\ " is a TeX space
    line = re.sub(r"\s+_", "_", line)                        # "multiplier _x" -> "multiplier_x"
    return _clean(line)


# ---------------------------------------------------------------------------
# Page -> sections
# ---------------------------------------------------------------------------

def _table_rows(table: Tag) -> list:
    """' | '-joined rows with EVERY cell kept, empty ones as EMPTY_CELL. Not
    orna_scrape_echo's version, which drops empty cells: on this site 229 rows
    have one, and dropping it moves every later number into the wrong column."""
    rows = []
    for tr in table.find_all("tr"):
        cells = [_clean(td.get_text(" ")) or EMPTY_CELL for td in tr.find_all(["th", "td"])]
        if any(c != EMPTY_CELL for c in cells):
            rows.append(" | ".join(cells))
    return rows


def _walk(node: Tag, out: list) -> None:
    """Emit lines for `node`'s block children, in document order."""
    for child in node.children:
        if isinstance(child, NavigableString):
            text = _clean(str(child))
            if text:
                out.append(text)
            continue
        if not isinstance(child, Tag):
            continue
        name = child.name
        if name in ("h1", "h2", "h3", "h4", "h5", "h6"):
            out.append(("H", name, _clean(child.get_text(" ")), child.get("id", "")))
        elif name == "table":
            out.extend(_table_rows(child))
        elif name == "pre":
            # Verbatim, line breaks kept and indented - the convention
            # knowledge_search's prompt describes as "a verbatim formula".
            out.extend("    " + ln for ln in child.get_text().splitlines() if ln.strip())
        elif name in ("ul", "ol"):
            for li in child.find_all("li", recursive=False):
                text = _clean(li.get_text(" "))
                if text:
                    out.append(f"- {text}")
        elif name == "details":
            summary = child.find("summary")
            if summary:
                out.append(_clean(summary.get_text(" ")))
                summary.extract()
            _walk(child, out)
        elif name in ("div", "center", "blockquote", "section", "article", "span", "figure"):
            _walk(child, out)
        elif name in ("script", "style", "nav", "button"):
            continue
        else:
            text = _clean(child.get_text(" "))
            if text:
                out.append(text)


def parse_chapter(html: str, page_url: str, chapter: str) -> str:
    """One chapter page -> corpus text. Each <h1> starts an article (cited at
    its own anchor), deeper headings become `## ` sections inside it."""
    main = BeautifulSoup(html, "html.parser").find("main")
    if main is None:
        raise ValueError(f"{page_url}: no <main> - not an mdBook page any more?")
    _decode_icons(main)
    items: list = []
    _walk(main, items)

    lines, started, parent = [], False, ""
    for it in items:
        if isinstance(it, tuple):
            _, level, text, anchor = it
            text = _tex(text)
            if level == "h1":
                title = chapter if text.lower() == chapter.lower() else f"{chapter} - {text}"
                url = f"{page_url}#{anchor}" if anchor else page_url
                lines.append(f"\n=== {title} ({url}) ===")
                started, parent = True, ""
                continue
            if not started:
                lines.append(f"\n=== {chapter} ({page_url}) ===")
                started = True
            if level == "h2":
                parent = text
                lines.append(f"## {text}")
            else:
                # A sub-heading names its PARENT too: "Example" and "Generic
                # case" are meaningless alone, and the reader weights heading
                # words 3x - so the subject has to be IN the heading.
                lines.append(f"## {parent} > {text}" if parent else f"## {text}")
        else:
            if not started:
                lines.append(f"\n=== {chapter} ({page_url}) ===")
                started = True
            # Verbatim <pre> lines (indented) are code and formulas exactly as
            # written - never rewritten.
            lines.append(it if it.startswith("    ") else _tex(it))
    return "\n".join(lines).strip() + "\n"


def chapter_pages(toc_html: str) -> list:
    """(chapter title, absolute page url) for every content page in the table
    of contents, in book order - read from the site rather than hardcoded, so
    a new chapter is picked up by the next crawl."""
    soup = BeautifulSoup(toc_html, "html.parser")
    seen, out = set(), []
    for a in soup.find_all("a", href=True):
        url = urljoin(TOC_URL, a["href"])
        if "#" in url or not url.startswith(BASE) or not url.endswith(".html"):
            continue
        page = url[len(BASE):]
        if page in _SKIP_PAGES or page in seen:
            continue
        seen.add(page)
        title = re.sub(r"^\s*[\d.]+\s*", "", a.get_text(" ", strip=True)).strip()
        if title:
            out.append((title, url))
    return out


def validate(text: str, chapters: list) -> None:
    """Refuse to write a corpus that is silently wrong - the repo-wide rule
    that an empty or partial parse is never committed as if it were complete."""
    hits = _MOJIBAKE_RE.findall(text)
    if hits:
        raise ValueError(f"mojibake in output ({hits[:3]}) - decoding went wrong, not writing")
    articles = re.findall(r"^=== .+ \((https?://[^)]+)\) ===$", text, re.M)
    if len(articles) < 15:
        raise ValueError(f"only {len(articles)} articles parsed - the site changed shape?")
    pages_seen = {u.split("#")[0] for u in articles}
    missing = [t for t, u in chapters if u not in pages_seen]
    if missing:
        raise ValueError(f"chapters produced no content: {missing}")
    # The icon decoding is the whole value of the status tables; if it stopped
    # working the corpus would still "look" fine - so check for its output.
    if "T. ↓↓↓" not in text or "[Def]" not in text:
        raise ValueError("decoded status-effect icons missing - the icon scheme changed?")


def build_text(progress: bool = True) -> str:
    chapters = chapter_pages(_fetch_text(TOC_URL))
    parts = []
    for i, (title, url) in enumerate(chapters):
        if i:
            time.sleep(DELAY_SECONDS)
        if progress:
            print(f"  {title:<20} {url}", file=sys.stderr)
        parts.append(parse_chapter(_fetch_text(url), url, title))
    text = (f"# Ornabook - community Orna mechanics guide ({BASE})\n"
            f"# Built by orna_scrape_ornabook.py. Credits: {BASE}Contributors.html\n\n"
            + "\n".join(parts))
    validate(text, chapters)
    return text


def _demo() -> None:
    # Icon decoding: the scheme, the stat-specific form, and a non-arrow icon.
    assert _icon_label("/img/icons/statuses/td_tmp.png") == "T. ↓↓↓"
    assert _icon_label("/img/icons/statuses/su_tmp.png") == "T. ↑"
    assert _icon_label("/img/icons/statuses/dd.png") == "↓↓"
    assert _icon_label("/img/icons/statuses/atk_tu_tmp.png") == "T. Att ↑↑↑"
    assert _icon_label("Guilds//img/icons/statuses/mag_sd.png") == "Mag ↓"
    assert _icon_label("/img/icons/statuses/def.png") == "Def"
    assert _icon_label("/img/icons/poisoned.png") == "Poisoned"
    assert _icon_label("/img/icons/theta_charon_riftlock.png") == "Theta Charon Riftlock"
    # An item whose name merely ENDS like an arrow code must not be read as one.
    assert _icon_label("/img/icons/kara.png") == "Kara"

    page = """<html><body><main>
      <h1 id="stats-altering"><a href="#x">Stats altering status effects</a></h1>
      <p>How much each tier changes a stat.</p>
      <table>
        <tr><th>Stat(s) altered</th><th><img class="mdbook-icon" src="/img/icons/statuses/td_tmp.png"></th>
            <th><img class="mdbook-icon" src="/img/icons/statuses/su_tmp.png"></th></tr>
        <tr><td><img class="mdbook-icon" src="/img/icons/statuses/def.png"><img class="mdbook-icon"
            src="/img/icons/statuses/atk.png"></td><td>-100%</td><td>+25%</td></tr>
        <tr><td><img class="mdbook-icon" src="/img/icons/statuses/dex.png"></td><td></td><td>+20%</td></tr>
      </table>
      <h2 id="todo">Calls (TODO)</h2>
      <h2 id="formula">Formula</h2>
      <pre><code>Damage = Atk * 2
- Def / 2</code></pre>
      <p><img src="/img/plot.svg" alt="dual wield ratio plot"></p>
      <h1 id="second">Second section</h1><ul><li>one</li><li>two</li></ul>
    </main></body></html>"""
    text = parse_chapter(page, BASE + "StatusEffects.html", "Status Effects")

    # the table means something: header and row labels survived as text
    assert "Stat(s) altered | [T. ↓↓↓] | [T. ↑]" in text, text
    assert "[Def] [Att] | -100% | +25%" in text, text
    # an empty cell keeps its column - "+20%" is still under T.↑, not under T.↓↓↓
    assert "[Dex] | — | +20%" in text, text
    # citations point at the real anchor on the real page
    assert "=== Status Effects - Stats altering status effects " \
           "(https://book.cadelabs.ovh/StatusEffects.html#stats-altering) ===" in text, text
    assert "(https://book.cadelabs.ovh/StatusEffects.html#second) ===" in text
    # formulas verbatim and indented; an uncapturable image is visibly marked
    assert "    Damage = Atk * 2\n    - Def / 2" in text, text
    assert "[image not captured: dual wield ratio plot]" in text
    assert "- one\n- two" in text

    # ...and orna_echo's reader parses it, with the empty TODO heading dropped
    from knowledge import orna_echo
    secs = orna_echo._parse(text)
    heads = [s.heading for s in secs]
    assert "Calls (TODO)" not in heads, heads
    assert "Formula" in heads
    assert secs[0].url.endswith("#stats-altering")

    # the guard refuses a corpus that lost its icons
    try:
        validate("=== a (https://book.cadelabs.ovh/A.html) ===\nx\n" * 20,
                 [("A", "https://book.cadelabs.ovh/A.html")])
    except ValueError as e:
        assert "icons" in str(e)
    else:
        raise AssertionError("a corpus without decoded icons must be refused")
    # LaTeX becomes readable math - the real formula from the Gear chapter.
    raw = (r"\[ \text{dual-wield-ratio} = \frac{\text{multiplier} _{\text{dual-wield}}}"
           r"{\text{multiplier} _{\text{single-wield}}} = \frac{(1 + 0.65 * B)^{2}}{1 + B} \]")
    got = _tex(raw)
    assert got == ("dual-wield-ratio = (multiplier_dual-wield) / (multiplier_single-wield) = "
                   "((1 + 0.65 * B)^2) / (1 + B)"), got
    assert _tex(r"\[ \text{multiplier}_{\text{single-wield}}\ = (1 + B) = 1.575 \]") == \
        "multiplier_single-wield = (1 + B) = 1.575"
    assert _tex("no markup here") == "no markup here"

    # A sub-heading keeps its parent's subject.
    nested = parse_chapter(
        '<main><h1 id="g">Gear bonus</h1><h2 id="d">Dual-wielding</h2><p>x</p>'
        '<h3 id="e">Example</h3><p>y</p><h1 id="h">Hybrid</h1><h3 id="s">Summary</h3><p>z</p></main>',
        BASE + "Gear.html", "Gear")
    assert "## Dual-wielding > Example" in nested, nested
    assert "## Summary" in nested and "Dual-wielding > Summary" not in nested, \
        "a new <h1> resets the parent heading"
    print("orna_scrape_ornabook: all checks passed")


if __name__ == "__main__":
    if "--demo" in sys.argv:
        _demo()
    else:
        corpus = build_text()
        tmp = OUT_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(corpus)
        os.replace(tmp, OUT_PATH)
        n = len(re.findall(r"^=== ", corpus, re.M))
        print(f"wrote {OUT_PATH}: {n} articles, {len(corpus):,} chars")
