"""Semantic search over the community text corpora, in Pinecone.

Replaces the word-overlap scorers in orna_knowledge / orna_mechanics /
orna_echo (+ Ornabook) / orna_qa / orna_reddit for knowledge_search: those
miss a question phrased differently from the text that answers it. The
corpora themselves stay where they are - this module CHUNKS them with each
module's own parser (so a chunk is a section / thread / dev comment, and its
citation is the one that module already gives) and uploads them.

One index with integrated embedding (Pinecone embeds both the records and the
query, so there is no embedding code here); one NAMESPACE per corpus, because
knowledge_search labels each corpus's block with its provenance and the model
weighs them differently.

Off when PINECONE_API_KEY is unset; telegram_orna then falls back to the grep
scorers, as it also does when a search call fails.

Rebuild after a corpus changes (re-scrape): `python3 orna_pinecone.py [ns ...]`.
/update_codex re-indexes the "knowledge" namespace itself, since that is the
one corpus it refetches.
"""
import json
import logging
import os
import sys
import time
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

API_VERSION = "2025-04"
INDEX = os.environ.get("PINECONE_INDEX", "orna-knowledge")
EMBED_MODEL = "llama-text-embed-v2"
# Below this cosine score a hit is noise - a vector search always returns
# top_k, even for "xyzzy plugh", and the model must see an honest empty.
# Measured 2026-10-06: junk queries top out at 0.16, real hits run 0.25-0.57.
# A calibration knob: tune with `python3 orna_pinecone.py --probe "<q>"`.
MIN_SCORE = float(os.environ.get("PINECONE_MIN_SCORE", "0.25"))
CHUNK_CHARS = 2000      # well inside the model's 2048-token input
BATCH = 96              # upsert_records' per-request record limit
# ...and a char budget per request: one full batch of dense table rows is
# near the plan's 250k embedding tokens/MINUTE, so it could never go through.
BATCH_CHARS = 50_000
TIMEOUT = 15.0

NAMESPACES = ("knowledge", "mechanics", "echo", "ornabook", "qa", "reddit", "discord", "questline")

_host: Optional[str] = None


def enabled() -> bool:
    return bool(os.environ.get("PINECONE_API_KEY"))


def _headers(ctype: str = "application/json") -> dict:
    return {"Api-Key": os.environ["PINECONE_API_KEY"], "X-Pinecone-Api-Version": API_VERSION,
            "Content-Type": ctype, "Accept": "application/json"}


def _index_host() -> str:
    global _host
    if _host is None:
        r = httpx.get(f"https://api.pinecone.io/indexes/{INDEX}", headers=_headers(), timeout=TIMEOUT)
        r.raise_for_status()
        _host = r.json()["host"]
    return _host


def search(namespace: str, query: str, top_k: int = 6) -> list:
    """Hits above MIN_SCORE, best first: [{"text","title","url","score"}].
    Raises on any HTTP/network failure - the caller decides the fallback."""
    r = httpx.post(f"https://{_index_host()}/records/namespaces/{namespace}/search",
                   headers=_headers(), timeout=TIMEOUT,
                   json={"query": {"inputs": {"text": query}, "top_k": top_k},
                         "fields": ["text", "title", "url"]})
    if r.status_code == 404:        # namespace never indexed: nothing there, not an outage
        return []
    r.raise_for_status()
    return [{**h["fields"], "_id": h["_id"], "ns": namespace, "score": h["_score"]} for h in r.json()["result"]["hits"]
            if h["_score"] >= MIN_SCORE]


# --- chunking: one record per section/thread/comment, split only if long ---

def _split(head: str, lines: list, max_chars: int = CHUNK_CHARS) -> list:
    """`head` + as many whole lines as fit, repeated - so a table chunk keeps
    its title and column header, and nothing is cut mid-row. A single line
    longer than the budget becomes its own (truncated-by-embedder) chunk."""
    chunks, cur, size = [], [], len(head)
    for line in lines:
        if cur and size + len(line) + 1 > max_chars:
            chunks.append("\n".join([head] + cur))
            cur, size = [], len(head)
        cur.append(line)
        size += len(line) + 1
    if cur:
        chunks.append("\n".join([head] + cur))
    return chunks


def _units(namespace: str) -> list:
    """(head, body_lines, title, url) per natural unit of one corpus."""
    if namespace == "knowledge":
        import orna_knowledge
        return [(f"[{s.title}]" + (f"\n{s.header}" if s.header else ""),
                 [l for l in s.lines if l != s.header], s.title, orna_knowledge.source_url(s.title) or "")
                for s in orna_knowledge._load()]
    if namespace == "mechanics":
        import orna_mechanics
        return [(f"=== {t} ===", b.split("\n"), orna_mechanics.SOURCE_TITLE, orna_mechanics.SOURCE_URL)
                for t, b in orna_mechanics._load()]
    if namespace in ("echo", "ornabook", "questline"):
        import orna_echo
        path = orna_echo.CORPUS_PATH if namespace == "echo" else \
            os.path.join(os.path.dirname(os.path.abspath(__file__)), f"orna_{namespace}.txt")
        return [(f"[{s.label}]", s.body.split("\n"), s.label[:60], s.url) for s in orna_echo._load(path)]
    if namespace == "qa":
        import orna_qa
        return [(t.text.split("\n", 1)[0], t.text.split("\n")[1:], f"r/OrnaRPG: {t.title}"[:60], t.url)
                for t in orna_qa._load()]
    if namespace == "reddit":
        import orna_reddit
        return [(e.head, e.body.split("\n"), e.head[:60], e.url) for e in orna_reddit._load()]
    if namespace == "discord":
        import orna_discord_search
        return orna_discord_search.units()
    raise ValueError(f"unknown namespace {namespace!r}")


def records(namespace: str) -> list:
    out = []
    for i, (head, lines, title, url) in enumerate(_units(namespace)):
        for j, text in enumerate(_split(head, lines)):
            out.append({"_id": f"{namespace}-{i}-{j}", "text": text, "title": title, "url": url})
    return out


# --- indexing ---

def ensure_index() -> None:
    r = httpx.get(f"https://api.pinecone.io/indexes/{INDEX}", headers=_headers(), timeout=TIMEOUT)
    if r.status_code == 404:
        httpx.post("https://api.pinecone.io/indexes/create-for-model", headers=_headers(), timeout=TIMEOUT,
                   json={"name": INDEX, "cloud": "aws", "region": "us-east-1",
                         "embed": {"model": EMBED_MODEL, "metric": "cosine",
                                   "field_map": {"text": "text"}}}).raise_for_status()
    else:
        r.raise_for_status()
    while not httpx.get(f"https://api.pinecone.io/indexes/{INDEX}", headers=_headers(),
                        timeout=TIMEOUT).json().get("status", {}).get("ready"):
        time.sleep(2)


def index_corpus(namespace: str) -> int:
    """Replace one namespace with the corpus as it is on disk now. Refuses an
    empty parse rather than wiping a good namespace (same rule as the caches)."""
    recs = records(namespace)
    if not recs:
        raise RuntimeError(f"{namespace}: corpus parsed to 0 records - not replacing the index")
    host = _index_host()
    r = httpx.delete(f"https://{host}/namespaces/{namespace}", headers=_headers(), timeout=TIMEOUT)
    if r.status_code not in (200, 202, 404):
        r.raise_for_status()
    batches, cur = [], []
    for rec in recs:
        if cur and (len(cur) == BATCH or sum(len(c["text"]) for c in cur) + len(rec["text"]) > BATCH_CHARS):
            batches.append(cur)
            cur = []
        cur.append(rec)
    batches.append(cur)
    for start, batch in enumerate(batches):
        body = "\n".join(json.dumps(rec, ensure_ascii=False) for rec in batch)
        r = None
        for _attempt in range(10):
            try:
                r = httpx.post(f"https://{host}/records/namespaces/{namespace}/upsert",
                               headers=_headers("application/x-ndjson"), content=body.encode(), timeout=60)
            except httpx.TransportError as e:       # timeout / connection drop
                logger.info("%s: batch %d failed (%s), retrying", namespace, start, e)
                time.sleep(10)
                continue
            # 429 = the plan's embedding tokens-per-MINUTE quota (250k on
            # starter; the whole corpus is ~1M) - wait out the window.
            if r.status_code == 429:
                logger.info("%s: embedding quota hit at batch %d, waiting 60s", namespace, start)
                time.sleep(60)
            elif r.status_code >= 500:
                logger.info("%s: batch %d got %d, retrying", namespace, start, r.status_code)
                time.sleep(10)
            else:
                break
        if r is None:
            raise RuntimeError(f"{namespace}: batch {start} never reached Pinecone")
        r.raise_for_status()
    # Writes are eventually consistent: wait for the count to settle, then
    # refuse to report success on a namespace that is short of records.
    count = 0
    for _ in range(30):
        stats = httpx.post(f"https://{host}/describe_index_stats", headers=_headers(), json={}, timeout=TIMEOUT)
        stats.raise_for_status()
        count = stats.json().get("namespaces", {}).get(namespace, {}).get("vectorCount", 0)
        if count == len(recs):
            return count
        time.sleep(5)
    raise RuntimeError(f"{namespace}: Pinecone holds {count} records, expected {len(recs)}")


def _demo() -> None:
    chunks = _split("[T]\nh", ["a" * 10, "b" * 10, "c" * 10], max_chars=30)
    assert chunks == ["[T]\nh\n" + "a" * 10 + "\n" + "b" * 10, "[T]\nh\n" + "c" * 10], chunks
    assert all(c.startswith("[T]\nh") for c in chunks)          # header repeats on every chunk
    for ns in NAMESPACES:                                        # every corpus parses to records
        recs = records(ns)
        assert recs or ns == "discord", ns       # discord is a gitignored harvest, absent on a fresh checkout
        assert len({r["_id"] for r in recs}) == len(recs), f"{ns}: duplicate ids"
    print("orna_pinecone: _demo ok")


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()
    args = sys.argv[1:]
    if args[:1] == ["--demo"]:
        _demo()
    elif args[:1] == ["--probe"]:
        q = " ".join(args[1:])
        MIN_SCORE = -1.0    # show raw scores, to pick the cutoff
        for ns in NAMESPACES:
            for h in search(ns, q, 3):
                print(f"{h['score']:.3f} {ns:9} {h['text'][:90]!r}")
    else:
        logging.basicConfig(level=logging.INFO)
        ensure_index()
        for ns in args or NAMESPACES:
            print(ns, index_corpus(ns), "records")
