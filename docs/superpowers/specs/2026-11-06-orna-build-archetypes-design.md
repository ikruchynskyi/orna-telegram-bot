# `/orna` build archetypes: learn the mechanics behind community builds — design

Date: 2026-11-06
Status: approved by the owner's request ("don't just copy builds - understand which mechanics they use, so the
bot can build new ones when new items and pets are added")

## Problem

The bot answers build questions by finding a guide build and repeating its items. When a class has no guide
build for the asked mode (2026-11-05: Beowulf Auriga PvP), or when a new item or pet arrives, it has nothing to
reason from, and it borrows items from builds for other modes or invents them.

## Source (verified 2026-11-06)

aussiescodex's Build Library (`/orna-builds`) is a client-side page over two public JSON endpoints:
`/api/builds/public` (79 builds: id, name, class, specialization, level, AL, PvP flag, equipment ids,
description, votes) and `/api/builds/<id>` (every slot's item, quality, upgrade level, element, anguish bonus,
each adornment; amities when set). There is no pet or skill field; a description sometimes names them.
49 PvE / 30 PvP; 77 at level 250; AL 0-173; 44 have votes; 15 a description.

Every item and adornment id resolves in the codex dump (`orna_aussies._codex()`), whose records carry the stat
keys a mechanic needs (crit, crit_damage, chain_damage_chance, apex_rate/apex_power, follower_stats/act,
buff_duration, ward, ...), the statuses an item gives or causes, and the skills it grants (`+spell`).

## Goal

A curated description of the MECHANICS that community builds combine - each archetype's engine (what deals
the damage or wins the fight), the stats it stacks and why, its caps and the class effects it relies on -
written in terms of stats and effects, not item names, and wired into the build tools so a future question
("build X with the new item Y") is answered by placing Y in the archetype it feeds.

## Design

1. **Scraper** `scrapers/aussie_builds.py`: list + details into `data/cache/aussie_builds/` (paced, cached by id;
   new builds only on a rerun).
2. **Profiles** `knowledge/orna_build_profiles.py`: a build -> its totals per stat family over all slots,
   adornments, anguish bonuses and amities, plus the statuses and skills its items give, plus the class's own
   effects (`orna_classes`). Pure functions over the codex; a self-check on hand-verified builds.
3. **Archetypes** (mined, then written by hand): group the profiles by their dominant stat families per class
   line and mode; an archetype needs 2+ builds or a guide/corpus passage that confirms it. Each is written up
   in `data/orna_build_archetypes.txt`: engine, stat priorities, enabling mechanics and caps, class effects it
   needs, a slot recipe in stats, PvP/PvE notes, and the evidence (build ids, guide lines). Unknowns stay
   unknown.
4. **Bot wiring**: the archetypes file is a corpus (Pinecone + keyword index), and `build_check` gets an
   ARCHETYPE dimension: the archetypes matching the goal/class, with "find current items for it" as concrete
   `query` conditions on the archetype's stat keys - so a new item with those stats is found the day the codex
   has it.
5. **Validation per step** (see the plan): profiles checked by hand on 5 builds; archetypes checked against the
   guides and corpora; a hold-out check - remove one build's items from the evidence and see if the archetype +
   stat queries rebuild an equivalent loadout; then tier-3 cases.

## Non-goals

- No copying of builds into the corpus as item lists (the evidence lists build ids, not loadouts).
- No damage simulator; numbers come from the codex and `calculate()`.
- No pet data from this source (it has none); pets come from the codex followers and the act-rate estimate.

## Risks

- Player builds vary in quality: weight by votes, and an archetype needs agreement from 2+ builds or a guide.
- Stat keys differ in scale (flat attack vs % crit): profiles count percentage families separately and compare
  a build against the class line's average, not raw sums.
