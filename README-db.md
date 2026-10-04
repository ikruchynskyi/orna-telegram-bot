# Which database for the codex — MySQL, MongoDB, or SQLite

Asked: *"Plan what DB is to use MySQL or MongoDB. We don't need strong
consistency so here Mongo is better but we have more reads — so I guess here
SQL DB is better."*

**Answer: SQLite** (`orna_codex_db.py`). Not as a compromise — on the two
criteria named in the question it wins both, and the third requirement
(full-text search on any field) it does better than either server engine.

## Measured facts this rests on

| | value |
|---|---|
| codex.json | 2.4 MB, 5,080 records across 9 categories |
| writers | **zero** — it is a derived cache of aussiescodex's public dump |
| write frequency | once a week (TTL) or on `/update_codex` |
| readers | **one process** (the bot) |
| in-memory parse of the whole dump | **14 ms** |
| full 5,080-record filter scan, in Python | **5.4 ms** |
| SQLite build of the full schema + FTS index | **0.33 s** |
| SQLite `GROUP BY` over all records | **0.34 ms** |
| resulting DB file | 13.0 MB |

## The axes that actually decide it

**Consistency is not an axis here.** Mongo's eventual-consistency tradeoff
buys nothing when there are no concurrent writers and no replicas — there is
nothing to be inconsistent about. The question's premise ("we don't need
strong consistency so Mongo is better") would hold for a write-heavy
multi-node dataset; this is a read-only 2.4 MB file.

**Reads dominate — which argues against a server, not for MySQL.** Every read
is already in-process. A localhost MySQL/Mongo round-trip is ~0.3–1 ms of
pure overhead *per query*; SQLite's is a function call. For a workload that is
100% reads of a small dataset, embedding the engine is strictly faster than
talking to one.

**Full-text search on any field was the deciding feature.**
- SQLite **FTS5**: unlimited virtual tables, per-column matching
  (`name: sword`), prefix (`blind*`), BM25 ranking. Used here over
  name/description/body.
- MongoDB: **one text index per collection**, maximum. For "search by any
  field" that is a hard architectural ceiling.
- MySQL: `FULLTEXT` works, but needs InnoDB, per-column index declarations,
  and has a stopword/min-token-length default that silently drops short terms.

**Irregular schema.** 37 top-level fields (most absent on most records) and
**157 distinct stat keys**. A column per stat is absurd and a rigid relational
schema needs a migration every time the game adds a field — which it does.
Handled with a long/narrow `stats(rid, field, value, raw)` table plus the raw
record in a `json` column for `json_extract`. Mongo would store this natively,
which is its one real advantage — but `stats` as a table is *better* for the
actual queries (sort/filter/aggregate on any stat with one index), and it is
the shape that survives a new stat with no code change either way.

**Operational cost, which is where the server engines really lose.** This bot
runs under `launchd` with a minimal environment, and CLAUDE.md documents
several outages that were really just "a binary wasn't found" or "a stale copy
was running". A server engine adds: a daemon to keep alive, startup ordering
(the bot must wait for it), a port, credentials in `.env` (this repo has
already leaked a `.env` once), and a second container in Docker with a volume
and a healthcheck. SQLite adds **one file**.

**Licensing**: MongoDB is SSPL, not open source. Minor here, but it is not nothing.

## Summary

| | MySQL | MongoDB | **SQLite** |
|---|---|---|---|
| install | brew + daemon + grants | brew + daemon | **already in the Python stdlib** |
| aggregation | SQL `GROUP BY` | aggregation pipeline | **SQL `GROUP BY`** |
| full-text, any field | `FULLTEXT`, per-column | **1 text index per collection** | **FTS5, per-column + ranked** |
| irregular schema | JSON column | native | JSON1 + EAV table |
| read latency | TCP round-trip | TCP round-trip | **in-process** |
| extra containers | 1 | 1 | **0** |
| secrets to manage | yes | yes | **none** |

## What was migrated, and how it was made safe

`orna_aussies.query_records`/`count_records` now run on SQL - they delegate to
`orna_codex_db.query`/`.count`, with the old in-memory scan kept as a fallback
if the DB file is missing or unreadable.

An earlier version of this document argued the opposite: that
`_eval_condition` should stay in memory because every branch in it exists
because of a documented live bug, so a hand port would re-litigate all of
them. That was wrong about which way the oracle cuts. The in-memory evaluator
being present is precisely what makes the port **verifiable**: run both over
all 5,080 records across the whole condition vocabulary and require identical
results.

The port splits in two, and only one half is new code:

- **Vocabulary resolution is reused verbatim** - `_resolve_stat_field`,
  `_resolve_attr_field`, `resolve_codes`/`_parse_buff_query` (the `"T Mag 3"`
  -> `t__mag_uuu` shorthand), `_USEABLE_BY_ALIASES`. That is where most of the
  subtlety lives, and none of it was re-derived.
- **Only the comparison semantics became SQL** - which is exactly what the
  differential test covers.

### The differential found four real bugs in the port

156 mismatching conditions out of 4,220 comparisons on the first run. Every one
would have shipped looking plausible:

| Bug | Symptom |
|---|---|
| Top-level `hp` mirrored into `stats` "so ORDER BY works across categories" | silently redefined `{stat hp > 100}` - that kind reads only `record["stats"]`, so a boss's 3,000,000 must not match |
| attr -> `stats[original_field]` fallback dropped | `{attr hp = 10}`: 20 items vs **0** |
| Label branch taken *before* that fallback | `{attr element = fire}`: 91 vs **0** |
| Character-array `element` shape dismissed as unreachable | `{attr element = fire}`: 91 vs **63** |

The third is the instructive one: `_resolve_attr_field("element")`
fuzzy-resolves to **`"events"`** - a genuinely wrong resolution - and the
in-memory path is only correct *because* the stats fallback rescues it. Two
bugs cancelling. The port had to reproduce that exact order of operations.

The fourth had been written off in this repo's own notes as a defensive branch
that never fires. `element` is stored as `['f','i','r','e']` on **288 items**
(and as `["fire"]` on 354 spells). Both shapes are now normalised at build time
in `_stat_raw`, so no query path needs to know.

A trimmed differential is **pinned in `orna_codex_db._demo`** - one case per
condition kind plus those four regressions, each declaring whether it expects
hits, since a case matching nothing in both paths agrees vacuously and would
keep "passing" after the data shifted. Run `python3 orna_codex_db.py`.

### One deliberate deviation

A condition on a field that resolves to nothing matches **every** record in
memory - the bool-flag branch reads absent-as-False, so "items where
`bogus_field` = false" returns all 5,080. The SQL layer fails closed instead.
Unreachable either way: `unresolvable_condition_fields` refuses such a query
upstream, which is asserted in `_demo` so it stays that way.

### One behaviour change

Grouping a count by a list field (`tags`/`events`) now returns per-value counts
(`found_in_chests: 382`) where the in-memory version returned one key per list
*combination* (`"['found_in_shops', 'found_in_chests']"`). The SQL shape is the
useful one, but this is a change in output, not a parity fix.

## If you still want MySQL

The decision is isolated to `orna_codex_db.py`: `_SCHEMA`, `build()`'s
`executemany` calls, `connect()`, and the condition builders. Swapping engines
means that file, a driver in `requirements.txt`, a service in
`docker-compose.yml`, and rewriting `search` from FTS5 to `FULLTEXT`. The
differential test transfers unchanged and would verify the new engine the same
way. No abstraction layer was built for one implementation, on purpose.
