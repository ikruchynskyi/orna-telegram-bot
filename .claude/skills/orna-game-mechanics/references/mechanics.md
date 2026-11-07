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
  (Superior 101-119, Famed 120-139, Legendary 140-169, Ornate 170+)** that
  fill the gaps guides leave — treat the repo as authoritative for code. The
  170 crossover is settled: the game labels a 169.7-170.0% item "Ornate"
  (Last Martyr's Legwraps screenshot, 2026-11-07).
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
  procs (e.g. Ashen Ruby = lifesteal). **Slots added are driven by FORGE LEVEL,
  not raw quality** — the repo's authoritative rule is
  `orna_assess.get_additional_slots`: godforged (lv13) +4, master/demonforged
  (lv11-12) +3, Ornate (quality ≥170) +2, quality >100 +1, else +0. So
  Masterforging = Ornate + 1, as the guides say. Verified live 2026-09-25
  against `codex.json` + `get_assess_result`: **base** slots run 1-12 (517
  items have 2, 456 have 4; 12 is the max, on Brilliant Feathers / Ymir
  Brilliant Feathers, T9-10 famed polearms), so the real **projected** max is
  **16** (12 base + 4 godforged) — not ~8. Only weapons exceed 9 base; 1509 of
  2764 items carry the stat at all, and an item without it (e.g. Lost Helmet)
  projects a flat 0 at every level. Celestial weapons ignore all of this: they
  use the fixed 20-level `CELESTIAL_WEAPON_SLOTS` ladder (1→5, +1 if
  boss-scaled).
- **Adornments themselves** are a real `place`/`item_type` in the data: 51
  items with `place: armor_(for_adornments)`, `item_type: adornment` (+47
  `augment_(for_celestial_weapons)`), each carrying tier, rarity, `useable_by`,
  `exotic`, `dropped_by`, and a stats dict that reaches stats ordinary gear
  doesn't (`ward_power`, `collateral_chance`, `avidity_chance`,
  `follower_stats`, `manaflask_charge`, `status_protection`). Query them with
  `/orna`'s `query` tool on `place`/`item_type` — no dedicated tool needed.
  NOTE `name` is `None` inside `codex.json`; names live in
  `translations.en.json` and `orna_aussies` merges them.
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
it matters: Arena/Colosseum reward tables, whether
"Settlements" == classic Dukedoms, and any specific quality-boundary % (the
repo's `get_quality_code` and community guides differ by a few points). For the
current value of ANY number, prefer live data + `releases()`.


## 10. Build math: caps, stacking, weakness, status, pets

What decides a build or gear-bonus choice, beyond the damage formula above.
DAMAGE CAPS. Damage is capped at 9,999,999 per hit; Damage Limit Break (an
anguish/crucible bonus) and some amities raise that limit (Reddit, u/Widogeist,
2024-07-17). A raid also caps damage, at the raid's max HP and per player
(player, 2026-07-23), and events set their own raid cap (Apollyon 2025: 50%,
lowered to 40%). Skills that hit many times (Deity's 2000 Seals/Blades) get
past the per-hit cap (players, 2026-07-06). Ara (Heretic specialization) has
5x the damage cap. UNKNOWN: whether Damage Limit Break raises a raid's
per-player cap, and whether Damage Limit Break from several items stacks. So a
build "at the damage cap" gains nothing from more damage of any kind until the
cap rises - first ask which cap it is at.
WEAKNESS STACKING (players, 2026-06-03..04). Blights do not stack with other
blights and sigils replace each other, but ONE blight and ONE sigil both apply:
blight +50% damage, sigil +20%. Weakness exploit applies to both, and a sigil
counts as a weakness for weakness exploit and for flask charging (double
charge). That is why weakness exploit beats +element damage in a sigil build:
it applies twice.
BONUS STACKING. Different buffs multiply (see the damage formula). Anguish
bonuses: proof chance and anguished-gear chance add; the other anguish bonuses
multiply (player, 2026-09-03). A 5% bonus does not cancel a 5% malus, because
they multiply (players, 2026-08-13).
PVP. Anguish bonuses, crucible bonuses included, do not work in PvP (possibly
except against anguished opponents) (players, 2026-08-05 and 2026-08-13).
Demonworking (Anguish Level) stats are PvE-only too (section above).
STATUS EFFECTS. The chance to apply a status is rolled once, on the first
successful hit (damage formula section). Status immunity items protect you from
enemies' debuffs, not from a follower's or your own items' (Reddit, u/Widogeist).
UNKNOWN: whether the player's Status Effect Chance changes a follower's skill
chance.
PETS. A follower's act chance is the sum of its category rates over its skills
plus its attack rate; Pet/Follower Act from gear, spec or amity adds to EACH
buff/spell/debuff/heal skill, not to attack or protect; at 100% it acts every
turn, so more act rate after that only shifts which skill it picks (player
research thread 2026-07-28; Fey Cactus example 2026-02-22). The research tool
shows this estimate per follower.
CRUCIBLES. A crucible replaces an anguished item's bonus with one random bonus
from its own pool; the pool and the slots differ per crucible, and accessories
take none (aussiescodex crucible table).

## Dungeons: Battle Dungeons only (Exploration Dungeons removed 2026-07-29)

Exploration (Deep) Dungeons - the mining, top-down kind added in late 2023 - were
removed on 2026-07-29 (server 1.331, app 3.26.1, playorna.com/releases): Goblin
Fortresses and Mystic Caves are Battle Dungeons again, and the Spelunking Guild
was reworked to be Battle Dungeon focused (dungeon divers earn Proofs of
Spelunking from dungeons and exchange them at the guild). Wiki pages and older
posts that describe Exploration Dungeons, mining, the Deep Kiln, or an
Exploration floor table (tier 11 = 12 floors) are out of date.
A Battle Dungeon's floor count depends on its tier (Ornabook's floor chart,
from Knight411's chart; each count checked against its boss floors):
| Tier | Floors | Boss floors |
|---|---|---|
| 1 | 5 | 5 |
| 2 | 9 | 9 |
| 3 | 10 | 10 |
| 4 | 10 | 10 |
| 5 | 13 | 3, 7, 13 |
| 6 | 13 | 2, 6, 13 |
| 7 | 13 | 3, 6, 13 |
| 8 | 20 | 4, 10, 20 |
| 9 | 20 | 4, 10, 20 |
| 10 | 22 | 5, 11, 16, 22 (22 can roll a Godforge aura) |
| 11 | 25 | 5, 11, 16, 25 (16 and 25: Arisen Superbosses) |
"Approximate floors" tables elsewhere (★4 12, ★7 18, ★9 22, ★10 25) disagree
with this chart; use the chart. For anything paid per run (guild progress,
end-of-dungeon rewards), reward per floor is what to compare across tiers.
