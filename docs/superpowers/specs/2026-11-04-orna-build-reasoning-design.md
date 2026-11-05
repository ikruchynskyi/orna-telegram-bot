# `/orna` build reasoning: a mechanics checklist for build questions — design

Date: 2026-11-04
Status: approved by the owner's request ("cover all mechanics and consider everything related to the matter")

## Problem

A build question ("which crucible bonuses for my Heretic weakness build with Gorynych?") needs the bot to
weigh many mechanics at once. Today `_OPTIMIZE_RULE` names three checks (cap, reliability, size), and the
model looks things up with narrow queries ("anguish flask power"). So the facts that decide the answer
never reach it, even though the corpora hold them. Verified 2026-11-04 by searching the corpora:

| Mechanic | Fact in the corpora | Where |
|---|---|---|
| Per-hit cap | damage is capped at 9,999,999 per hit; Damage Limit Break and some amities raise it | `orna_reddit.txt` (developer, 2024-07-17) |
| Raid cap | a raid caps damage at its max HP and per player; events set their own % (Apollyon 50→40%) | `threads_knowledge.txt` (players) |
| Class cap | Ara has 5x the damage cap | `orna_guide_heretic.txt` |
| Multi-hit | many-hit skills (2000 Seals/Blades) bypass the per-hit cap | `threads_knowledge.txt` |
| Weakness | one blight and one sigil both apply (blight +50%, sigil +20%); weakness exploit applies to both, so it beats +element damage | `threads_knowledge.txt` (players, 2026-06) |
| Stacking | anguish proof/anguished-gear chance add; the other anguish bonuses multiply; a 5% bonus does not cancel a 5% malus | `threads_knowledge.txt` |
| PvP | anguish (incl. crucible) bonuses do not work in PvP | `threads_knowledge.txt` (2026-08) |
| Status | the chance to apply a status is rolled once, on the first hit | `orna_mechanics.txt` |
| Pets | act-rate formula; Pet Act adds to every non-attack skill; 100% = acts every turn | `orna_guide_beowulf.txt`, `threads_knowledge.txt` |

The 2026-11-04 runs of the crucible question used none of the caps, stacking, PvP or weakness-stacking facts.
Four runs gave four different answers; the best one still missed the raid cap and the game mode.

## Goal

For any build question (gear, crucibles, amities, adornments, pets, buffs; any class), the model gets ONE
tool result that lists every mechanic that can matter, each with the rule and the community passages about
it for this build, and it answers each one (or says it is unknown).

## Success criteria

- `crucible-optimize` passes 3/3 (was 2/3).
- New tier-3 cases pass ≥ 2/3: `pvp-crucible` (the answer says crucible bonuses do not work in PvP),
  `weakness-vs-element` (the answer says weakness exploit applies to blight and sigil both), `gsh-raid-cap`
  (the answer names the damage cap).
- Tier 0 stays ALL GREEN, with a new deterministic check of the checklist composer.
- No regression on the related cases (`anguished-crucible`, `crucible-passives`, `heretic-build`,
  `sirus-strategy`, `falx-facts`, `goats-hood`).

## Design

1. **Facts go into the corpus once.** A new `orna_mechanics.txt` section, "Build math: caps, stacking,
   weakness, status, pets", holds the verified facts above, each with its source and date. Then
   `knowledge_search`, `research` and the new tool all find them. The skill's
   `references/mechanics.md` gets the same section (the file header asks for the two to stay in sync).
2. **`build_check` tool** (`args={"goal": "<what the build boosts>", "mode": "<raid/tower/anguish/pvp/...>",
   "gear": "<named items/pet, optional>"}`). A fixed list of dimensions in code (`_BUILD_DIMENSIONS`), each
   with its rule and a query template. The tool runs the dimension queries in parallel over `_retrieve`,
   keeps the top passages per dimension (deduplicated across dimensions), adds `orna_bonuses.overview()`
   when the question is about crucibles and `orna_releases.search_strict(goal)` for freshness, and returns
   one block: "BUILD CHECKLIST - answer every line; 'unknown' is an answer". The dimensions:
   CONTEXT (game mode, target, immunities), PIPELINE (where the bonus enters the damage formula; the
   penetration threshold comes first), CAPS, STACKING, SOURCE & RELIABILITY, SATURATION, SLOTS &
   AVAILABILITY, TRADE-OFFS (survival, mana, ward), FRESHNESS.
   The composer is a pure function of (goal, mode, gear, retrieve), so tier 0 tests it with a fake retriever.
3. **Recipe.** `_OPTIMIZE_RULE` calls `build_check` after the build's gear is known, and `research` for a pet.
   It asks for the game mode (or states the assumption), then a per-slot pick that answers each dimension.
   PLAN already gets the recipe.
4. **REVIEW.** A new point: for a build question, the draft addresses every checklist dimension or says it
   is unknown; a claim the checklist marks unknown is not stated as fact.

## Non-goals

- No damage simulator. Numbers come from the passages and `calculate()`.
- No new scrapers. The facts are already in the corpora.
- No change to the step, PLAN or REVIEW models.

## Risks

- Context size: about 9 dimensions × 2 passages ≈ 8-10k characters. The loop's context is large; the result
  is cut at a line boundary like the other tools.
- Generic queries can retrieve off-topic passages. Each query combines the dimension's own words with the
  build's goal terms. The plan includes tuning them against real build questions before the cases run.
