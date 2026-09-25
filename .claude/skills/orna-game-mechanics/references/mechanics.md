# Orna mechanics — verified reference (as of early 2026)

Built from the 2020 ornalegends "Ultimate Beginner Basics Guide" as a base,
then fact-checked against current sources (playorna.com codex + blog.ornarpg.com
[official], ornarpg.fandom.com wiki, playerecho.com, ornalegends.com, reddit
r/OrnaRPG) in 2026. Each item is marked **still true / changed / new / renamed**
vs. that 2020 guide. **Orna patches ~monthly — for an exact current number,
prefer this repo's live data (`orna_assess`/`orna_classes`/codex) and the
`releases()` tool over anything written here.** "unverified" = could not
confirm from a primary source this pass.

---

## 1. Progression: tiers, levels, classes

- **Character level cap: 250.** (still true) Tier 10 unlocks at level 225.
- **10 class tiers, not 11.** (correction) playorna's live codex tops out at
  ★10 (82 classes). The level-225→250 bracket is casually called "Tier 11" but
  has no new class - the devs shipped **Ascension** instead of a real 11th
  tier. Source: playorna.com/codex/classes/.
- **Starting classes Warrior/Mage/Thief.** (still true) The class tree branches
  per line; "spec choice at tiers 3/5/7/9" from the 2020 guide is imprecise
  (branch points aren't evenly spaced) — treat as approximate.
- **Six tier-10 specializations: Gilgamesh, Heretic, Realmshifter, Beowulf,
  Grand Summoner, Deity** (still true) — each with **Celestial evolved
  variants** via the Titans (Beowulf→Auriga/Hydrus, Deity→Ara/Ursa,
  Gilgamesh→Hercules/Ursa, Heretic→Ara/Corvus, etc.) (new since 2020). These
  are the `spec_stats` entries in `orna_classes.json`.
- **Separate level-50 "Specializations" system** (Hunter/Berserker/Guardian/
  Scholar/Stargazer/Cleric) — a passive/skill package layered on your class,
  distinct from the tier-10 specializations above (easy to confuse; the bot's
  code always means the tier-10 kind). Source: ornarpg.fandom.com/wiki/
  Specializations.

## 2. Ascension Level (AL) — NEW since 2020 (~2022)

- Unlocked at Tier 10 (level 225) by building the **Altar of Ascension**
  (≈50,000,000 orns + 1,000 stone).
- **+1% to base stats per AL, per CLASS** (not account-wide). AL 100 ≈ +100%
  (double) that class's base stats. All variants of a class ascend together.
- Effectively **uncapped** (no dev-confirmed cap; community sees high ALs; this
  repo has handled AL 500+). Does **not** raise the level cap.
- In this repo: `orna_classes.scale(base, modifiers, ascension_level, pvp)`
  applies class % modifiers, then AL (+1%/lvl), then **PVP doubles HP only**.
  Sources: playerecho.com/orna/ascension-guide, ornalegends.com ascension guide.

## 3. Core stats

- Primary set (still true): **HP, Mana, Attack, Defense, Magic, Resistance,
  Dexterity, Ward, Crit** (+ Foresight, View Distance).
- Wider current vocabulary the 2020 list omits: **Crit Damage** (a stat
  distinct from Crit/Crit Chance), **Follower Stats**, **Summon Stats**,
  **Multi-target Damage**, and %-stacking **bonus** stats (orn/exp/gold/luck
  bonus). The repo's `_ESTIMATE_STATS` covers hp/mana/attack/defense/magic/
  resistance/dexterity/foresight/crit/ward/view_distance; `orna_aussies`'
  `translations['stats']` (~155 keys) is the real full vocabulary.
- **Ward** matters more late-game (still true, more so now): endgame hits can
  be 20k-50k/turn vs 5k-15k HP pools; ward pool ≈ (HP+Mana)/2 scaled by gear
  Ward%. **HP doubles in PvP** (still true).

## 4. Item quality & upgrading

- **Quality tiers** (community/wiki, still ~as in 2020): Broken 70-89%, Poor
  90-99%, Common/Regular 100%, Superior 110-119%, Famed 120-130%, Legendary
  140-170%, Ornate 170-200% (170% is a Legendary/Ornate crossover).
  **In this repo `orna_assess.get_quality_code` uses contiguous bands
  (Superior 101-119, Famed 120-139, Legendary 140-170, Ornate >170)** that
  fill the gaps guides leave — treat the repo as authoritative for code; verify
  a live assess page if an exact boundary % ever matters.
- **Upgrade path** (still true): Blacksmith levels 1-10 (time+materials+gold);
  **Masterforge = level 11** (instant; materials ≈ quality-fraction × 333, e.g.
  200%→666), **Demonforge = level 12** (× 666), **Godforge = level 13** (RNG
  drop from a yellow-aura "Arisen Superboss" while a demonforged item is
  equipped — no material recipe). Masterforge/Demonforge happen at the
  Alchemist/Demonologist buildings. Repo: `_FORGED_LEVELS`
  (masterforged=11/demonforged=12/godforged=13), `BONUS_QUALITY_SCALING`
  (superior +10 … godforged +50) for %-bonus stats.
- Whetstones jump a weapon instantly to level 2 (Fine → level 3). Dismantling
  gives ×1 material regardless of quality/level; smelting a level down returns
  ×1.

## 5. Gear: slots, adornments, celestial, arisen

- **Slots / `place`**: Weapon / Head (head) / Body (torso) / Feet (legs) /
  Off-hand / Accessory (any class), + material / armor_(for_adornments) /
  augment_(for_celestial_weapons). (still true; matches aussies' `place` enum.)
- **Adornment slots** (new since 2020): socketable gems with on-hit/passive
  procs (e.g. Ashen Ruby = lifesteal). **Max slot count is driven by FORGE
  LEVEL, not raw quality** — Masterforging unlocks an item's max slots
  regardless of quality (≈ its Ornate slot count + 1). Real max is >4
  (a Celestial example has 4; community builds show up to ~8) — exact per-slot
  chart unverified.
- **Celestial gear** (new since 2020): the top rarity, Tier 10, "refined
  Skyshards", Titan-augmented via **Celestial Augments** at the Titan Workshop
  (augments are scoped to celestial weapons). Repo: `is_celestial_weapon`, the
  celestial 20-level upgrade path, the `augment_(for_celestial_weapons)` place.
- **Arisen gear** (new since 2020) is a **source tag** (drops from Arisen
  superbosses), **NOT a rarity** — an Arisen item can be Famed rarity. Don't
  treat "Arisen" as a quality level.

## 6. Elements & factions

- **Four factions** (still true, no 5th): Earthen Legion (Earth), Stormforce
  (Lightning), Knights of Inferno (Fire), Frozenguard (Water). Unlock at
  level 10. Change costs $0.99 (RuneShop, real money), repeatable.
- **Faction effect: +25% damage dealt with your element, −20% damage TAKEN
  from that element** (correction — a defensive damage-reduction bonus, not the
  "−25% resistance" the 2020 guide implied). Some mobs are immune to specific
  elements; physical can't be immuned by players.
- **Elemental sigils/runes** mark an enemy for bonus matching-element damage
  (renamed, not new). Weapon element affects Attack skills; elemental skills
  override weapon element.
- **Per-boss elemental resistances/immunities are NOT in any structured source**
  — only `knowledge_search`'s "Monster Data" and the web. (Still the single
  biggest data gap; matches `orna_knowledge.py`'s own note.)

## 7. Endgame systems (mostly NEW since 2020 — the guide's biggest gaps)

- **Wild Towers of Olympia** (new, Jan 2023): 5 titans — Selene, Eos, Oceanus,
  Themis, Prometheus — Tier 9+ endgame. Wild towers auto-climb (start ~15,
  cap 50, weekly reset, a different reset day per titan). Entry: Olympian Key;
  currency: Tower Shards → Titan Workshop (celestial upgrades, celestial
  classes, personal towers). **Repo `orna_towers` computes live floor heights
  (verified port) — trust it over guide-site schedules.**
- **Flasks / Omniflask** (new, 2025): a Mage-tree battle mechanic (own battle
  slot, Tier 2+) that charges by casting elemental spells (faster on hitting a
  weakness) and persists across battles in Towers/Monuments. Variants:
  Manaflask, Bloodflask, Ward Flask Infusion, Mana-surge, Inflection;
  **Omniflask** consumes a charged flask for an all-elements blast. (This is
  the "omniflask build" the class guides reference.)
- **Monuments & kingdom guild halls** (new, Nov 2023): Monuments = 4
  single-player Fallen-God dungeons (Ithra/Thor/Demeter/Vulcan; free if
  faction-matched else a Skeleton Key; reward "Proofs of Monument"). Titan
  Workshop gates Tower access + Titan weapons/classes. Guild halls: Traveler's
  (Guild Trials), Conqueror's (real-world "Settlements", Duke→Emperor), Blades
  of Finesse (live PvP).
- **PvP Arena / Colosseum** (new): fought vs. AI running another real player's
  loadout. Arena (Sparring/PvP/Mirror, 1 token each); Coliseum (needs a
  kingdom Castle, 20 tokens, a 20-fight scaling ladder). No items; HP/Mana
  start full.
- **Raids / Dungeons / Gauntlet** (continuations, not new): Gauntlet = 10
  floors, one loss ends it, needs level 50 + a **Skeleton Key** (renamed from
  Gauntlet Key). Dungeons unlock ~Tier 3/level 50, per-dungeon cooldowns.
  Raids = World Raids, party up to 6, solo-able, privately summonable.

## 8. Followers / pets

- Acquired via **Bestiaries** (built ≈5,000 orn + materials; sell followers by
  tier), stored in the **Keep** (capacity scales with Keep level); follower
  stats auto-scale to player level (no separate feeding grind found).
- **Bestial Bond** (new) — tiered follower synergy: T1 stronger + faster act
  rate; T2 chance to act twice/turn + better AI; T3 chance to protect the
  player. Item-granted Bestial Bond now scales with item quality (Aug 2025);
  "Bestial Bond 1" is an obtainable Amity for non-Valhallan classes.
- Class names: **"Beastmaster" is renamed** to Tamer→Handler→Spirit Tamer; the
  Valhallan Beowulf/Bestla line is the Bestial-Bond powerhouse (→ Hydrus /
  Auriga, the latter letting followers act twice/turn). **Summoner is NOT a
  follower class** — it auto-casts summon spells (Grand Summoner auto-casts the
  first two summons each battle).

## 9. Kingdoms, areas, currencies, bonuses

- **Kingdoms**: max 50 members (still true); **join at level 25** (still true)
  but **create at level 150** (changed — the 2020 guide's "25" conflated join
  vs. create), ≈100,000 orns to found; **no longer faction-locked** as of Nov
  2025 ("Fellowship Across Factions"). Wars, raid bosses, territory.
- **Areas/territory** (still true): ~442m diameter, interact within 250m,
  guardians (one-time double reward), daily orn income to owner (minor), become
  a "ghost" (stat drop) after ~30 days unvisited.
- **Currencies**: Gold (buy/upgrade gear; endgame upgrades cost billions);
  **Orns** (unlock classes, swap specs, buy followers, build, some upgrades);
  Elemental Stones (enchanting); **Proofs** (per-guild materials — this repo's
  `orna_proofs` prices the 10 guild proof currencies); Tower Shards; Skeleton/
  Olympian Keys; materials (from dismantling).
- **Multiplicative stat bonuses** (still true): Weapon Proficiency +5% (Mage/
  Thief offensive), Origin Town +10% all core, Party +10% all core — they
  MULTIPLY (1.05 × 1.10 × 1.10 ≈ +27%, not +25%).

---

### Freshness / trust

Verified early 2026 against the sources named at top. Highest confidence:
playorna.com codex/blog (primary) and this repo's own ported math
(`orna_assess`, `orna_classes`, `orna_towers`). Lower confidence / re-check if
it matters: exact adornment-slot maxima, Arena/Colosseum reward tables, whether
"Settlements" == classic Dukedoms, and any specific quality-boundary % (the
repo's `get_quality_code` and community guides differ by a few points). For the
current value of ANY number, prefer live data + `releases()`.
