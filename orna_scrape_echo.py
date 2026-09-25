#!/usr/bin/env python3
"""
orna_scrape_echo.py - build `orna_echo.txt` from playerecho.com/orna's guides.

Why this source earns a place next to the others: it is the only one that
writes down the MECHANICS AND FORMULAS the codex never states and the community
sheets only tabulate - Ward capacity's base formula and absorption rates,
Ascension altar costs, dungeon modes/cooldowns/godforging, the Circle of
Anguish's proof paths, orn/XP multiplier stacking - plus per-EVENT tier gates
and rewards, which `orna_calendar` (live event list) has no content for at all.

Committed and re-run BY HAND, like `orna_reddit.txt` and `orna_guide_*.txt`,
NOT on a TTL like the community sheets. These are published articles: they
change when their author revises them, not weekly, so re-fetching 37 pages on a
timer would spend the budget to learn nothing. Re-run it when the site adds
guides (`python3 orna_scrape_echo.py`), read the diff, and commit.

Enumerated from the site's own SITEMAP, not by scraping the four paginated
index pages: the sitemap is the canonical list, so a pagination change or a
13th post on a page cannot silently drop an article. robots.txt is `Allow: /`
(checked 2026-09-25).

Output format matches `orna_knowledge.txt`'s conventions so one reader shape
covers both: `=== Title (url) ===` per article, `## Heading` per section, and
tables flattened to `" | "`-joined rows. Flattened rather than typed for the
same reason: these are hand-written pages, and a layout tweak should degrade to
messier text, never a crash.
"""
from __future__ import annotations

import re
import sys
import time
import urllib.parse

import requests
from bs4 import BeautifulSoup

SITEMAP_URL = "https://playerecho.com/sitemap-0.xml"
BASE = "https://playerecho.com"
OUT_PATH = "orna_echo.txt"
# Identify the crawler honestly rather than spoofing a browser.
HEADERS = {"User-Agent": "orna-telegram-bot/1.0 (guild helper bot; contact via github ikruchynskyi)"}
DELAY_SECONDS = 1.0
TIMEOUT = 30

# /orna itself and /orna/2../orna/4 are index pages, not articles.
_ARTICLE_RE = re.compile(r"^/orna/(?!\d+$)[a-z0-9-]+$")
_WS_RE = re.compile(r"[ \t ]+")


# "Ã" / "â" where "×" / "→" belong: the signature of UTF-8 bytes decoded as
# latin-1. This site serves `Content-Type: text/html` with NO charset, so
# requests falls back to ISO-8859-1 (its documented default for text/*) while
# the document itself declares <meta charset="utf-8">. That corrupted exactly
# the characters the formulas are made of - "Ward_Total = Ward_Base × (1 + ...)"
# arrived as "Ã (1 + ...)" - so this is checked, not assumed.
_MOJIBAKE_RE = re.compile(r"[ÃÂ][\u0080-\u009f\u00a0-\u00bf]|â\u0080")


def _fetch_text(url: str) -> str:
    """GET `url` and decode it properly. Never read `.text` directly here."""
    resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    resp.raise_for_status()
    resp.encoding = resp.apparent_encoding or "utf-8"
    return resp.text


def article_urls() -> list:
    """Every /orna/<slug> article in the sitemap, in sitemap order."""
    resp = requests.get(SITEMAP_URL, headers=HEADERS, timeout=TIMEOUT)
    resp.raise_for_status()
    out = []
    for loc in re.findall(r"<loc>([^<]+)</loc>", resp.text):
        path = urllib.parse.urlparse(loc.strip()).path.rstrip("/")
        if _ARTICLE_RE.match(path):
            out.append(BASE + path)
    if not out:
        raise RuntimeError("sitemap yielded no /orna/<slug> articles - markup changed?")
    return out


def _clean(text: str) -> str:
    return _WS_RE.sub(" ", text.replace("\r", " ").replace("\n", " ")).strip()


def _table_rows(table) -> list:
    """A table flattened to ' | '-joined rows, header row included. Same shape
    orna_knowledge.txt uses, so a reading model gets the column names with the
    numbers instead of having to guess which cell is which."""
    rows = []
    for tr in table.find_all("tr"):
        cells = [_clean(td.get_text(" ")) for td in tr.find_all(["th", "td"])]
        cells = [c for c in cells if c]
        if cells:
            rows.append(" | ".join(cells))
    return rows


def parse_article(html: str, url: str) -> str:
    """One article as a text block. Returns "" if there is no <article> body,
    so a redirect or an error page is skipped rather than written out empty."""
    soup = BeautifulSoup(html, "html.parser")
    article = soup.find("article") or soup.find("main")
    if article is None:
        return ""
    # The <h1> lives in the page's hero <section>, OUTSIDE <article> - looking
    # only inside it silently titled every block with the URL slug.
    h1 = soup.find("h1")
    title = _clean(h1.get_text(" ")) if h1 else url.rsplit("/", 1)[-1]

    lines = [f"=== {title} ({url}) ==="]
    # The hero's one-line standfirst summarises the whole guide, which is
    # exactly what a search wants to match on.
    subtitle = _clean(h1.find_next("p").get_text(" ")) if h1 and h1.find_next("p") else ""
    if subtitle:
        lines.append(subtitle)
    seen_any = bool(subtitle)
    for node in article.find_all(["h1", "h2", "h3", "h4", "p", "li", "table", "blockquote", "pre"]):
        # A <li>/<p> inside a table is already covered by the table's own rows.
        if node.name != "table" and node.find_parent("table") is not None:
            continue
        if node.name == "h1":
            continue                       # already the block title
        if node.name in ("h2", "h3", "h4"):
            heading = _clean(node.get_text(" "))
            if heading:
                lines.append("")
                lines.append(f"## {heading}")
            continue
        if node.name == "table":
            rows = _table_rows(node)
            if rows:
                lines.extend(rows)
                seen_any = True
            continue
        if node.name == "pre":
            # THE FORMULAS LIVE HERE. Every calculation on this site is a
            # <pre><code> block ("Ward_Base = (HP + MP) / 2"), so a parser that
            # only reads <p>/<li> captures the sentence "Ward is calculated from
            # your stats:" and drops the formula it introduces - which is the
            # single most valuable thing this source has. Line breaks are
            # preserved here (unlike prose, which gets collapsed): a multi-line
            # formula squeezed onto one line is unreadable.
            for raw in node.get_text("\n").splitlines():
                code = _WS_RE.sub(" ", raw).rstrip()
                if code.strip():
                    lines.append(f"    {code.strip()}")
            seen_any = True
            continue
        text = _clean(node.get_text(" "))
        if not text:
            continue
        lines.append(f"- {text}" if node.name == "li" else text)
        seen_any = True
    if not seen_any:
        return ""
    return "\n".join(lines).rstrip() + "\n"


def build_text(urls=None, progress=True) -> str:
    """The whole corpus. Extracted as its own function (like
    orna_scrape_knowledge.build_text) so a future TTL-refresh could reuse the
    exact builder the committed file was made with, rather than a second copy
    that can drift from the parser reading it."""
    urls = urls if urls is not None else article_urls()
    blocks, failed = [], []
    for i, url in enumerate(urls, 1):
        try:
            block = parse_article(_fetch_text(url), url)
        except Exception as exc:           # one bad page must not lose the other 36
            failed.append((url, repr(exc)))
            block = ""
        if block:
            blocks.append(block)
        else:
            failed.append((url, "no article body parsed"))
        if progress:
            print(f"  [{i}/{len(urls)}] {'ok  ' if block else 'SKIP'} {url}", file=sys.stderr)
        time.sleep(DELAY_SECONDS)
    if failed and progress:
        print(f"\n{len(failed)} page(s) produced nothing:", file=sys.stderr)
        for url, why in failed:
            print(f"  {url} - {why}", file=sys.stderr)
    if not blocks:
        raise RuntimeError("no articles parsed - refusing to write an empty corpus")
    text = "\n\n".join(blocks)
    hits = _MOJIBAKE_RE.findall(text)
    if hits:
        raise RuntimeError(
            f"refusing to write a mis-decoded corpus: {len(hits)} mojibake sequence(s), "
            f"e.g. {hits[:5]} - check the charset handling in _fetch_text")
    return text


def _demo() -> None:
    """Parser check against a saved-shape fixture - no network, so it pins the
    extraction rules rather than the site's current wording."""
    html = """<article>
      <h1>Ward Guide</h1>
      <p>Intro para.</p>
      <h2 id="x">Ward Capacity: The Base Formula</h2>
      <p>Base Ward is calculated from your stats:</p>
      <pre class="astro-code"><code><span class="line"><span>Ward_Base = (HP + MP) / 2</span></span></code></pre>
      <ul><li>First bullet</li><li>Second bullet</li></ul>
      <table><tr><th>Tier</th><th>Ward</th></tr><tr><td>10</td><td>500</td></tr>
        <tr><td>9</td><td>400</td></tr></table>
      <h3>Common Mistakes</h3>
      <p>Trailing  whitespace   collapsed.</p>
    </article>"""
    out = parse_article(html, "https://example.com/orna/ward-guide")
    assert out.startswith("=== Ward Guide (https://example.com/orna/ward-guide) ===")
    assert "## Ward Capacity: The Base Formula" in out
    assert "## Common Mistakes" in out                      # h3 is a section too
    assert "- First bullet" in out and "- Second bullet" in out
    assert "Tier | Ward" in out and "10 | 500" in out        # header row kept with the data
    assert "Trailing whitespace collapsed." in out           # runs of spaces squeezed
    # the formula itself, not just the sentence introducing it
    assert "Ward_Base = (HP + MP) / 2" in out, "a <pre> formula must survive parsing"
    assert "<" not in out and ">" not in out                 # no markup leaks through
    # a page with no <article>/<main> body yields nothing rather than a stub
    assert parse_article("<div><h1>Oops</h1></div>", "u") == ""
    # a body with headings but no prose is still empty - an index page, not a guide
    assert parse_article("<article><h1>T</h1><h2>Only a heading</h2></article>", "u") == ""
    # h1 outside <article> (the real page shape) still titles the block
    hero = '<section><h1>Real Title</h1><p>Standfirst.</p></section><article><p>Body.</p></article>'
    assert parse_article(hero, "u").startswith("=== Real Title (u) ===")
    assert "Standfirst." in parse_article(hero, "u")
    # the mojibake guard must fire on mis-decoded text and stay quiet on good text
    assert _MOJIBAKE_RE.search("Ward_Total = Ward_Base Ã\u0097 (1 + Bonus)")
    assert _MOJIBAKE_RE.search("1: Starter class â\u0080\u0093 early")
    assert not _MOJIBAKE_RE.search("Ward_Total = Ward_Base \u00d7 (1 + Bonus) \u2192 done")
    assert _ARTICLE_RE.match("/orna/ward-guide") and not _ARTICLE_RE.match("/orna/4")
    assert not _ARTICLE_RE.match("/orna")
    print("orna_scrape_echo: all parser checks passed")


if __name__ == "__main__":
    if "--check" in sys.argv:
        _demo()
        sys.exit(0)
    _demo()
    urls = article_urls()
    print(f"{len(urls)} article(s) from the sitemap", file=sys.stderr)
    text = build_text(urls)
    with open(OUT_PATH, "w", encoding="utf-8") as fh:
        fh.write(text)
    sections = text.count("\n=== ") + text.startswith("=== ")
    print(f"wrote {OUT_PATH}: {sections} articles, {len(text):,} chars", file=sys.stderr)
