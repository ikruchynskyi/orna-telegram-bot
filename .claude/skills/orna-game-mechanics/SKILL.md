---
name: orna-game-mechanics
description: >-
  Verified mental model of Orna's game mechanics (classes/specs, quality &
  forging, Ascension, gear slots, factions, towers, followers) plus a map of
  where the repo's live authoritative data for each lives. Use when writing or
  fixing bot code/prompts that reason about game mechanics, or when an answer
  about one looks wrong. Not needed for plumbing changes (Telegram, caching,
  OCR, /go).
---

# Orna game mechanics

## Golden rule

Orna patches ~monthly; numbers here can drift. Use this to UNDERSTAND a
mechanic; take the CURRENT value from the repo's live data. If they disagree,
the live data wins - say so, don't silently "correct" verified code.

| mechanic | live source |
|---|---|
| item stats/effects/drops/rarity/place | `orna_codex` + `orna_aussies` (`research` for an entity + all its links in one call) |
| class/spec stats & % modifiers | `orna_classes.json` (`find_class`) |
| AL / PVP scaling | `orna_classes.scale` only (+1%/AL, PVP HP×2), pinned by `_demo` |
| quality tiers, upgrade projection, forge levels, bonus scaling | `orna_assess` |
| Wild Tower floors/resets | `orna_towers` (port of `tower.ts`; beats any guide) |
| proofs / guild forecast | `orna_proofs`, `orna_sheets` |
| amities / crucibles | `orna_bonuses` |
| boss elemental immunities, community facts | `orna_knowledge` (`knowledge_search`) |
| formulas (Ward, damage, altar costs) | `orna_echo`, `orna_mechanics.txt` |
| dev statements / patch changes | `orna_reddit` / `orna_releases` |
| class/build strategy | `orna_guide_*.txt` (`class_guide`) |

## Gotchas

1. **No tier-11 CLASS** (10 tiers, level cap 250, Ascension instead) - but
   ★11 is a real CONTENT tier (Arisen superbosses on floors 16 and 25, double
   Godforge chances at ★11 lv250). Source: `knowledge_search`.
2. **Three nested levels: LINE > CLASS > SPECIALIZATION.** Six lines (Mage,
   Thief, Warrior, Valhallan, Summoner, Demigod), each with a class at every
   tier 1-10. 18 tier-10 classes (Heretic/Hera, Gilgamesh/Gallia,
   Beowulf/Bestla, Grand Summoner, Deity, Realmshifter + celestial variants).
   A specialization (Ranger, Sequencer, Duelist...) sits on top.
   `orna_classes.json`'s pools are INVERTED: `spec_stats` (19) = tier-10
   classes, `classes` (40) = specializations. Pool names stay; everything
   user/model-facing uses the game's words (`_reassign_class_pools`).
   Gear restrictions key on the LINE (`useable_by`, six values). A class's
   line is in no structured source - never hardcode a line table.
3. **Ascension Level**: per class, +1% base stats per level, no known cap
   (`ASCENSION_LEVEL_SANITY_CAP` is a guard, not a rule), doesn't raise level cap.
4. **Quality**: repo's `get_quality_code` (Superior 101-119, Famed 120-139) is
   authoritative over guides (110-119 / 120-130). Forge levels:
   master=11, demon=12, god=13; quality and level are independent axes.
5. **Arisen is a SOURCE tag, not a rarity**; Celestial IS a rarity. Only one
   celestial weapon equippable.
6. **Sigils** = the renamed Rune mechanic (`EarthRune`...).
7. **Four factions** (Earthen Legion, Stormforce, Knights of Inferno,
   Frozenguard): +25% dealt / −20% taken for your element. Kingdoms not
   faction-locked since Nov 2025. Apollyon is a boss.
8. **Summoner is not a pet class**; follower power is Bestial Bond
   (Tamer→Handler→Spirit Tamer, strongest on Valhallan/Beowulf).
9. **Loadout**: 1 head/torso/legs, 2 accessories, two hands = one two-hander
   OR two one-handers (dual wield = 65% of combined stats) - never two-hander
   + off-hand. `two_handed` is a codex TAG; weapon subtype doesn't imply it.

Depth on any mechanic: `references/mechanics.md` - read only the section you need.
