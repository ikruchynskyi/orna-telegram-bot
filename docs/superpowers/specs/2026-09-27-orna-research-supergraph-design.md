# `/orna` research supergraph tool — design

Date: 2026-09-27
Status: proposed (awaiting review)

## Problem

The `/orna` ReAct loop gathers codex data one entity per tool call. To answer
"what does Fallen King Centaurus drop, and which class benefits from those
items?" the model must `open_entry` the raid (1 network call), read the 6 drop
names, then `open_entry` each of the 6 items (6 more network calls) to get
their stats — ~7 network round-trips and ~7 LLM steps. This blows the step /
wall-clock budget, so the bot **fails to answer a question whose data it
already has on disk**. Verified live: the reported centaurus question.

Two costs the user named:
1. **Price** — every step is a paid LLM call; incremental fetching multiplies
   them.
2. **Quality** — feeding the model data drip-by-drip ("haptic phone effect")
   produces worse analysis and forces the user to rephrase, even though the
   bot is sitting on the data and only needs to grep the right slice.

## Goal

Give the model **broad context in one iteration**: a single tool call returns
the whole relevant subgraph ("supergraph") for a subject — the entity, its
cross-linked relations, each relation's analysis-relevant leaf data, and the
related community knowledge — so it analyzes once and finishes.

## Success criteria

- The centaurus question is answered from **one `research` call + `finish`**
  (was: fail). Measured via the real-model harness, ≥ 4/5 runs.
- The supergraph for an in-dump entity is built with **zero network calls**.
- Fewer LLM steps per analytical request (the whole point).
- A capped/oversized bundle is marked `PARTIAL` — a cap never reads as "that's
  all there is" (the standing observation-honesty rule).

## Key finding that shapes the design (verified against live data)

`codex.json` (the aussies dump, already shipped) holds **every entity by ID**,
including the centaurus raid — records carry **no `name` field** (names live in
`translations.en.json`), which is why a name-grep of `codex.json` misses them.
The cross-link **edges are all present in the dump**: `raids.drops` (122),
`bosses.drops` (271), `monsters.drops` (351), `monsters.skills` (390),
`bosses.skills` (272), `raids.skills` (125), `items.dropped_by` (313),
`items.upgrade_materials` (367), `spells.used_by` (280), `spells.learned_by`
(193), `followers.bestial_bond` (278), `followers.skills` (276),
`classes.skills` (82). Edge targets are `[category, id]` pairs.

Name→ID resolution is also already local: `orna_aussies.display_name(cat, id)`,
`all_codex_names()`, `fuzzy_codex_name(query)`.

**Therefore resolve → edges → leaves is entirely in-memory from data we already
ship.** Network is only a fallback for an entity genuinely not yet in the dump
(a brand-new event entity between dump refreshes).

## Non-goals

- **No GraphQL query language.** The model names a subject; it does not author a
  query string (that would add a malformed-query failure mode on a local model
  for flexibility this problem doesn't need). Decided with the user.
- **Do not remove** `open_entry` / `search_codex` / `query` — they still serve
  browsing and single lookups. `research` is added alongside as the default for
  *analytical* questions.
- **No depth > 1** in phase 1 (entity → its direct relations' leaves). Deeper
  traversal (a drop's own droppers) is not needed for the target questions.
- `knowledge_search` stays as its own tool; `research` reuses its corpus
  aggregation rather than replacing it.

## Architecture

One new loop tool, **`research`**, unifying two supergraph halves in one
observation. Data/graph logic lives in `orna_aussies.py` (all the data +
resolution helpers are there); the tool wrapper, rendering, and knowledge merge
live in `telegram_orna.py`.

### Component 1 — codex supergraph builder (`orna_aussies.py`, new)

`build_supergraph(names: list[str], expand: list[str] | None = None,
per_relation_cap: int = 12) -> dict` (exact signature finalized in the plan):

1. **Resolve** each name → `(category, id)`:
   - build a reverse index `name.lower() → [(category, id)]` from
     `display_name` over all records (cached module-level, like `_ALL_NAMES`);
   - exact match first, then `fuzzy_codex_name` for typos/transliteration;
   - **collision / ambiguity** (a name in >1 category): return a short
     `ambiguous: [...]` disambiguation list in the result rather than guessing
     (the tool renders it as "did you mean"). A category preference orders it
     (bosses/raids/monsters for the common "beat/drops" ask), but never
     silently drops alternatives.
2. **Fetch** the primary record from `_codex()["main"][category][id]`
   (in-memory). If absent, fall back to a single playorna `open_entry`-style
   fetch for that one entity's edges (rare; logged), then continue joining
   leaves from the dump.
3. **Expand** its edge fields one level. Default edge set is category-aware
   (a raid/boss/monster → `drops` + `skills`; an item → `dropped_by` +
   `upgrade_materials` + `ability`; a follower → `bestial_bond` + `skills`; a
   spell → `learned_by`/`used_by`). `expand` overrides the default when given.
4. **Join leaves**: each edge target `[cat, id]` → a compact leaf record
   `{name (via display_name), category, useable_by, place/item_type, tier,
   rarity, key numeric stats, notable resolved effects}`. Effects/immunities
   codes resolved via the existing `resolve_codes`/`display_name` path.
5. **Cap** each relation to `per_relation_cap`; when truncated, record
   `partial: true` + the true total for that relation.

Returns a structured dict (not text) so it is unit-testable and the renderer is
separate. Pure/in-memory (wrapped in `asyncio.to_thread` at the call site, per
the event-loop rule).

### Component 2 — knowledge supergraph (reuse, broadened)

For the resolved subject name(s), gather the community context in the **same**
call: the existing corpora aggregation (`orna_knowledge` incl. Monster-Data
elemental immunities, `orna_reddit`, `orna_qa`, `orna_echo`, `orna_bonuses`,
`orna_classes`, `orna_mechanics`). Factor the aggregation currently inside
`_run_knowledge_tool` into a shared helper both it and `research` call (no
duplication). Broadening = drive it by the resolved entity name and raise the
per-corpus caps modestly. Best-effort: a corpus with nothing contributes an
empty section, never an error.

### Component 3 — the loop tool (`telegram_orna.py`, new `_run_research_tool`)

- Add `"research"` to `_ACTIONS` (so it is also in `_STEP_TOOLS`).
- Wrapper: resolve+build (to_thread) → render **one** structured text
  observation (codex supergraph section + knowledge section) → return it.
- **No per-entity cards** posted to chat (like `open_entry` `post=False`), so
  the loop's browsing doesn't spam the answer. Record touched entities on
  `session.viewed_entries` so `finish()` can offer buttons on demand.
- Cite sources via `_add_source` (aussies; playorna only if the fallback
  fetched; knowledge sources as `_run_knowledge_tool` already does).
- Multi-entity: the observation bundles all subjects (the "even if the user
  asks about multiple monsters" case).

### Prompt changes

- Add a `research` bullet: use it for analytical/comparative questions ("what
  drops X and which class benefits", "how do I beat Y", "compare what these
  bosses drop") — call it **once**, analyze, `finish`; do not chain
  `open_entry`.
- Note it satisfies `_STRATEGY_RULE` (its knowledge half carries the Monster
  Data immunities), so a "how to beat X" question needs `research`, not a
  separate `knowledge_search` + N `open_entry`.
- `_ACTIONS` stays the single source for the prompt enum and `_STEP_TOOLS`
  (they cannot drift).

## Data flow

```
user question
  -> model picks action=research, action_input="Fallen King Centaurus"
  -> _run_research_tool
       -> to_thread(build_supergraph)            [codex.json + translations, no net]
            resolve name->(raids, fallen-king-centaurus)
            record + drops(6) + skills(6), each drop joined to its item leaf
       -> to_thread(gather_knowledge)            [Monster Data / reddit / qa / ...]
       -> render ONE observation (supergraph + knowledge, capped+honest)
  -> model analyses everything in ONE step -> finish (with source + entry buttons)
```

## Error handling / edge cases

- **Entity not in dump** → single playorna fallback fetch for its edges, then
  join leaves from the dump; if still nothing, honest "not found" observation
  (never a fabricated answer).
- **Ambiguous name** → disambiguation list, not a guess.
- **Oversized relation** → capped + `PARTIAL` with the true count.
- **Subject is a spell/class/building** (no drops) → expands its own relevant
  edges (`learned_by`/`used_by`/`skills`); empty relations are omitted.
- **Knowledge miss** → empty knowledge section; codex half still stands.

## Testing (TDD)

- **Tier 0 (`orna_aussies._demo` / new checks)**: `build_supergraph("Fallen
  King Centaurus")` → category `raids`, ≥ 6 drops each carrying `useable_by`
  and ≥ 1 numeric stat, ≥ 6 skills, built with no network (from `_codex()`
  only). A multi-entity call bundles both. An oversized relation sets
  `partial`. Ambiguity returns alternatives.
- **Tier 0 wiring (`orna_test_suite.py`)**: `research` is in `_ACTIONS` /
  `_STEP_TOOLS`; `_run_research_tool` returns an observation containing the
  drops with stats; reuse the observation-honesty helper (a capped list shows
  `PARTIAL`).
- **Real-model (harness, N≥5)**: the exact centaurus question → one `research`
  call + `finish`, answer names drops and reasons class-fit; a
  "how to beat <immune boss>" question uses `research` and cites immunities.
- Run `python3 orna_test_suite.py` (Tier 0 must stay 100%) at the end.

## Rollout / phasing

- **Phase 1** (this spec): `research` tool, codex supergraph from `codex.json`,
  knowledge half via the shared aggregator, prompt guidance, tests.
- **Phase 2** (only if a real request needs it): depth > 1, richer field
  selection via JSON args. Not built now (YAGNI).

## Files touched

- `orna_aussies.py` — `build_supergraph` + reverse name index + `_demo`
  additions.
- `telegram_orna.py` — `_run_research_tool`, `_ACTIONS`/`_STEP_TOOLS`, shared
  knowledge aggregator extracted from `_run_knowledge_tool`, prompt bullet +
  `_STRATEGY_RULE` note.
- `orna_test_suite.py` — Tier-0 checks above.
- `CLAUDE.md` — a `research` subsection under the `/orna` docs (conventions
  require documenting why, not just what).
```
