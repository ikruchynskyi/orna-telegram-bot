# `/orna` build archetypes - implementation plan

Spec: `docs/superpowers/specs/2026-11-06-orna-build-archetypes-design.md`. Each task ends with its check; do not
start the next task until it passes.

## Task 1 - Scraper
`scrapers/aussie_builds.py`: `fetch()` (list, then each new id's detail, 1.5 s apart, into
`data/cache/aussie_builds/`), `load()` -> list of builds. CLI `fetch`, `--demo`.
Check: 79 builds load; every build has a class and a weapon; the demo passes offline on a saved build.

## Task 2 - Profiles
`knowledge/orna_build_profiles.py`: `profile(build)` -> {families: {family: total}, statuses, skills, class
effects, mode, class line}. Families map codex stat keys (crit, crit_damage, chain_damage_chance, apex_*,
follower_*, summon_*, ward*, buff_duration, weakness/flask, low-HP/mana, hybrid, ...). Adornments are counted
per copy; anguish bonuses and amities are added under the same families.
Check: 5 builds verified by hand against the codex (the crit Dara, the 200% chain Whipper, a GSA summon PvP,
a Beowulf Towerfall, a Gilgamesh Hercules spiked shield).

## Task 3 - Mining
A report (scratch, not shipped): per class line and mode, each build's top families against the line's
average; clusters of builds sharing their top 2-3 families. Read every cluster with its descriptions.
Check: each candidate archetype has 2+ builds or a guide/corpus passage; mechanics claimed for it (caps,
stacking, class effects) are looked up in the corpora and the codex, with the source noted.

## Task 4 - Archetype knowledge
`data/orna_build_archetypes.txt`: a general "how builds are built" section (engine -> scaling -> enablers ->
protection -> caps -> mode) and one section per archetype, in stats and effects, with evidence ids.
Corpus wiring: Pinecone namespace + keyword index + source note; `build_check` ARCHETYPE dimension with the
stat-key queries.
Check: the archetypes come back for paraphrased questions (retrieval benchmark cases); the stat queries return
real items per slot.

## Task 5 - Hold-out and cases
Hold-out: for 3 archetypes, ask the bot to build it for the class, with the source builds' item names excluded
from the evidence, and compare the stat profile of its answer with the source builds'. Tier-3 cases: an
archetype build request, a "new item" request (an item in the codex that no guide or build mentions), and a
mode-specific request.
Check: cases ≥ 2/3; tier 0 green; related cases unchanged.

## Task 6 - Ship
CLAUDE.md, commit, push, restart.
