# `/orna` build reasoning - implementation plan

Spec: `docs/superpowers/specs/2026-11-04-orna-build-reasoning-design.md`.
Run every command from `~/orna-telegram-bot` with `set -a && . ./.env && set +a`.

## Task 1 - Build-math facts into the mechanics corpus

Files: `data/orna_mechanics.txt`, `.claude/skills/orna-game-mechanics/references/mechanics.md`.

1. Add the section `=== Build math: caps, stacking, weakness, status, pets ===` after
   "Stat bonuses (they multiply)". One short paragraph per fact in the spec's table, each ending with its source
   and date (developer / players / guide). State the unknowns as unknowns: whether Damage Limit Break raises a
   raid's per-player cap, whether the player's Status Effect Chance changes a pet's skill chance, whether
   Damage Limit Break from several items stacks.
2. Copy the same section into the skill reference.
3. Re-index: `python3 -m knowledge.orna_pinecone mechanics`.
4. Verify: `python3 -m knowledge.orna_pinecone --probe "damage cap limit break raid"` and
   `--probe "weakness exploit sigil blight"` rank the new section in the top 3 of `mechanics`.

## Task 2 - The checklist composer and the `build_check` tool

Files: `bot/telegram_orna.py`.

1. `_BUILD_DIMENSIONS`: a tuple of `(name, rule, query_template)` for CONTEXT, PIPELINE, CAPS, STACKING,
   SOURCE & RELIABILITY, SATURATION, SLOTS & AVAILABILITY, TRADE-OFFS. A template uses `{goal}`.
2. `_compose_build_check(goal, mode, gear, hits_by_dim, extras) -> str`: a pure function. For each dimension:
   the rule line, then at most `_BUILD_HITS` passages (title + `_BUILD_HIT_CHARS` characters), with no passage
   repeated across dimensions; "(no passage found - say unknown)" when empty. Then the extras (crucible
   overview, patch notes). Header: "BUILD CHECKLIST for <goal> (<mode or 'mode not given: ask or state your
   assumption'>) - answer every line; unknown is an answer".
3. `_run_build_check_tool(args, sources)`: runs `_retrieve` for every dimension in parallel
   (`asyncio.gather`), adds `orna_bonuses.overview()` when the goal or gear says crucible/anguish, adds
   `orna_releases.search_strict(goal)`, records the sources, calls the composer.
4. Wire it in: `_ACTIONS`, `_run_tool`, the status label, the short-input summary, and a `_TOOLS_TEXT` entry
   ("- build_check(args={goal, mode, gear}): the mechanics checklist for a build question ...").
5. Tier 0: `_demo()` gets a composer check with a fake `hits_by_dim` - every dimension appears, a duplicate
   passage appears once, an empty dimension says unknown, a missing mode asks for it.

## Task 3 - Tune the dimension queries on real questions

1. Run the tool alone (scratchpad script) for: the Gorynych crucible question, "crucible bonuses for my
   Beowulf in PvP arena", "weakness exploit amity or fire damage amity for heretic", "raise Grand Summoner
   Hydrus raid damage".
2. For each, read the passages per dimension. Change a template where the top passage is off-topic.
   Target: the spec's facts appear under CAPS, STACKING and CONTEXT for the questions they concern.

## Task 4 - Recipe and REVIEW

Files: `bot/telegram_orna.py`.

1. `_OPTIMIZE_RULE`: get the goal and gear (class_guide or the user's items) -> `research(<pet>)` if a pet
   matters -> `build_check(...)` -> ask or state the game mode -> a per-slot pick, then one line per checklist
   dimension that changes the pick. Keep the cap / reliability / size wording as the examples of a dimension.
2. REVIEW prompt: point 9 - "BUILD: if the evidence has a BUILD CHECKLIST, the draft covers each dimension
   that changes the recommendation, or says it is unknown. A point the checklist marks unknown is not stated
   as fact."

## Task 5 - Cases and measurement

Files: `tests/orna_test_suite.py`.

1. Add tier-3 cases `pvp-crucible`, `weakness-vs-element`, `gsh-raid-cap` (spec's success criteria), and add
   `build_check` to `crucible-optimize`'s `tools_all`.
2. Run `TIER=3 N=3 CASE=crucible-optimize,pvp-crucible,weakness-vs-element,gsh-raid-cap`.
3. Run the related cases at N=2 and tier 0. Read every failure's answer before changing anything.

## Task 6 - Ship

1. CLAUDE.md: one line for `build_check` next to the build-questions line.
2. Commit (code, corpus, skill reference, tests, these two docs), push, restart the bot, check the log.
