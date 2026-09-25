---
name: orna-game-mechanics
description: >-
  A verified mental model of how the mobile game Orna actually works, for
  writing accurate bot code and prompts. Read this WHENEVER you are building
  or changing anything that reasons about Orna's mechanics - stats, item
  quality/upgrading/forging, classes & specializations, Ascension Level, gear
  slots & adornments, celestial/arisen gear, elements & factions, kingdoms,
  raids/dungeons/gauntlet, Wild Towers, flasks/sigils, followers/bestial
  bond - or when a tool/answer about any of those looks wrong. It gives the
  current (2026-verified) mechanics, the gotchas that are easy to get wrong,
  and - most usefully - a map from each mechanic to WHERE the repo's live
  authoritative data for it lives, so you use that instead of hardcoding a
  number that will be stale by the next patch. See references/mechanics.md for
  the full verified reference.
---

# Orna game mechanics (for building the bot accurately)

## Golden rule: this is a mental model, not the source of numbers

Orna is patched roughly monthly (solo dev "Odie"). Any concrete number here
(a %, a level, a cost) can drift. Use this doc to **understand** a mechanic;
for the **current value**, always prefer the repo's own live data and, when it
matters, the live game:

- item stats/facts/effects/drops/rarity/place → the codex, via
  `orna_codex` (playorna) + `orna_aussies` (aussiescodex `codex.json`), i.e.
  the `/orna` `search_codex`/`query`/`open_entry` tools.
- class & specialization base stats and % modifiers → `orna_classes.json`
  (`orna_classes.find_class`/`estimate`); the AL/PVP scaling rules live ONLY
  in `orna_classes.scale`, pinned by that module's `_demo`.
- item **quality tiers, upgrade projection, forge levels, bonus scaling** →
  `orna_assess` (`get_quality_code`, `_QUALITY_NAME_TO_PERCENT`,
  `BONUS_QUALITY_SCALING`, `get_assess_result`).
- **Ascension Level & PVP** scaling → `orna_classes.scale` (+1%/level, HP×2).
- **Wild Tower** floor heights → `orna_towers` (a line-for-line port of
  OrnaCodex's `tower.ts`, cross-checked under Node - **trust it over any
  guide site's tower schedule**).
- proof pricing / guild resource forecast → `orna_proofs`, `orna_sheets`.
- amities / crucibles → `orna_bonuses`.
- community facts the codex doesn't track, above all **per-boss elemental
  resistances/immunities** → `orna_knowledge` (`knowledge_search`).
- what the devs have SAID → `orna_reddit`; current patch numbers →
  `orna_releases` (the `releases()` tool). Class/build strategy →
  `orna_guide_*.txt` (`class_guide`).

If a mechanic below and the repo's live data disagree, the live data wins -
and tell the user, don't silently "correct" verified code (the quality
boundaries below are the standing example).

## The gotchas that are easy to get wrong (read these before you touch mechanics code)

1. **There is NO Tier 11 CLASS - but "★11" is a real CONTENT tier, so don't
   answer "there is no tier 11" flatly.** There are 10 class tiers and the
   character level cap is 250; people say "Tier 11" for the bare level-225→250
   bracket, and no new class lives there - the developers shipped **Ascension**
   instead. `orna_classes.json` correctly tops out at ★10 (82 classes), and
   nothing in the codex carries a tier above 10 in ANY category (verified
   2026-09-25 across items/monsters/bosses/raids/followers/classes/spells).
   What ★11 *does* mean is harder content, and it has consequences worth
   answering: ★11 dungeons/towers put Arisen Superbosses on **floor 16 and
   floor 25** (★10 only on the final floor), and a character at ★11 level 250
   gets **double the Godforging chances per run**. None of that is in any
   structured source - it lives in `orna_echo`/`orna_mechanics`, via
   `knowledge_search`.

2. **"Specialization" is TWO different things - don't conflate them.**
   (a) the six tier-10 CLASS specializations (Gilgamesh, Heretic,
   Realmshifter, Beowulf, Grand Summoner, Deity, + their Celestial variants
   like Ara/Corvus/Auriga/Hydrus/Ursa/Hercules) - THIS is what
   `orna_classes`' `spec_stats` / the `estimate_stats` tool mean by
   "specialization"; and (b) a separate level-50 passive-package system
   (Hunter/Berserker/Guardian/Scholar/Stargazer/Cleric). The bot's code means
   (a); a user might mean (b).

3. **Ascension Level is per-CLASS, +1% to base stats per level, effectively
   uncapped.** AL 100 ≈ +100% (double) a class's base stats; all variants of
   one class ascend together. It does NOT raise the level cap (still 250) -
   it's a parallel axis. No dev-confirmed cap (the repo has seen AL 500+;
   `ASCENSION_LEVEL_SANITY_CAP` is a runaway guard, not a game rule).

4. **Item quality: the repo's `get_quality_code` boundaries are the port from
   OrnaCodex and may differ slightly from community guides** (guides say
   Superior 110-119 / Famed 120-130; the repo uses Superior 101-119 / Famed
   120-139, filling the gaps the guides leave). Treat the repo's version as
   authoritative for code; if an exact % near a boundary ever matters, verify
   on a live playorna assess page rather than trusting a guide. Forge levels
   are solid: masterforged=11, demonforged=12, godforged=13 (Godforge is a
   boss-RNG drop, not a paid recipe).

5. **"Arisen" is a SOURCE tag (superboss drops), NOT a rarity.** Arisen gear
   can be Famed rarity. "Celestial" IS a real top rarity (Titan-crafted from
   Skyshards). Don't treat "Arisen" like a quality/rarity level.

6. **"Sigils" are (almost certainly) the renamed old "Rune" mechanic** - the
   codex slugs are literally `EarthRune`/`IceRune`/… - not a separate new
   endgame system. Mark an enemy for bonus elemental damage on a matching hit.

7. **Factions: there are exactly FOUR** - Earthen Legion (Earth), Stormforce
   (Lightning), Knights of Inferno (Fire), Frozenguard (Water). "Apollyon" is
   a raid boss, not a faction. The faction effect is **+25% damage dealt with
   your element and −20% damage TAKEN from it** (a defensive bonus - not the
   often-quoted "−25% resistance"). As of Nov 2025, kingdoms are **no longer
   faction-locked**.

8. **Summoner is NOT a pet/follower class** (it auto-casts summon spells); the
   old "Beastmaster" is renamed to the Tamer→Handler→Spirit Tamer line, and
   the follower-power system is **Bestial Bond** (tiered synergy), strongest
   on the Valhallan/Beowulf line. Followers act via their own AI, not player
   command.

9. **Per-boss elemental immunities/resistances are NOT in any structured data
   source** (not playorna's codex, not aussies) - only in `knowledge_search`'s
   community "Monster Data" and on the web. This is why `_STRATEGY_RULE`
   forces `knowledge_search`/`web_search` for "how do I beat X".

## Full verified reference

`references/mechanics.md` has the complete, source-cited breakdown (progression,
stats, quality/upgrading, gear/adornments/celestial, elements/factions,
endgame systems - Wild Towers, Flasks, Monuments, Arena/Colosseum, Bestial
Bond -, kingdoms, areas, currencies, multiplicative bonuses), each marked with
"still true / changed / new since the 2020 guide" and its source. Read it when
you need a specific mechanic in depth; the map above is where its LIVE numbers
live in this repo.
