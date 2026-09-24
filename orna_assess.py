"""
Orna GPS RPG - Item Quality Assessment & Stat Prediction
=========================================================

Python port of the TypeScript assess module.
"""

import math
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any, Dict, List, Optional, Tuple


# =============================================================================
# Constants
# =============================================================================

ASSESS_KEYS: Tuple[str, ...] = (
    "hp", "mana", "attack", "magic", "defense",
    "resistance", "dexterity", "ward", "crit", "foresight",
)
ASSESS_KEY_SET = frozenset(ASSESS_KEYS)

CELESTIAL_WEAPON_SLOTS: Tuple[int, ...] = (
    1, 1, 1, 1, 2, 2, 2, 2, 2, 3, 3, 3, 3, 3, 4, 4, 4, 4, 4, 5,
)

ANGUISHED_SKIP_KEYS = frozenset({"ward", "foresight"})


# =============================================================================
# Quality enum & related tables
# =============================================================================

class Quality(IntEnum):
    BROKEN = 0
    POOR = 1
    REGULAR = 2
    SUPERIOR = 3
    FAMED = 4
    LEGENDARY = 5
    ORNATE = 6
    MASTERFORGED = 7
    DEMONFORGED = 8
    GODFORGED = 9


BONUS_QUALITY_SCALING: Dict[str, int] = {
    "broken": -90, "poor": 0, "regular": 0, "superior": 10, "famed": 15,
    "legendary": 20, "ornate": 25, "masterforged": 30, "demonforged": 40,
    "godforged": 50,
}

QUALITY_CODE_BONUS_KEYS = frozenset({
    "orn_bonus", "exp_bonus", "luck_bonus", "gold_bonus",
    "monster_encounters", "manaflask_power", "apex",
    "no_follower_bonus", "bestial_bond",
    "player_r_follower_ability_chance", "mana_overspend_chance",
})

NO_BASE_BONUS_KEYS = frozenset({
    "no_follower_bonus", "bestial_bond",
    "player_r_follower_ability_chance", "mana_overspend_chance",
})

QUALITY_NUMBER_BONUS_KEYS = frozenset({
    "act_first_chance__pve_",
    "swap_defense_resistance",
})


def get_quality_name(code: int) -> Optional[str]:
    """Translation key like 'quality.masterforged'."""
    try:
        return "quality." + Quality(code).name.lower()
    except ValueError:
        return None


# =============================================================================
# Data classes
# =============================================================================

@dataclass
class CodexEntry:
    name: str = ""
    stats: Dict[str, Any] = field(default_factory=dict)
    is_adornment: bool = False
    is_accessory: bool = False
    is_celestial_weapon: bool = False
    is_two_handed: bool = False
    is_upgradable: bool = True
    has_scaling_slots: bool = False
    boss_scaling: int = 0  # 0 none | 1 boss-scaled | -1 celestial


@dataclass
class AssessInput:
    entry: CodexEntry
    level: int = 1
    boss_scaling: int = 0
    quality: int = 100
    quality_code: int = -1
    ang_level: int = 0
    stats: Dict[str, float] = field(default_factory=dict)


@dataclass
class StatRow:
    base: float
    values: List[float]


@dataclass
class AssessResult:
    entry: CodexEntry
    quality: int = 100
    quality_code: int = -1
    boss_scaling: int = 0
    ang_level: int = 0
    stats: Dict[str, StatRow] = field(default_factory=dict)
    levels: int = 0
    exact: bool = False


# =============================================================================
# JS-compat helpers
# =============================================================================

def in_range(n: float, lo: float, hi: float) -> bool:
    return lo <= n < hi


# =============================================================================
# Core stat math
# =============================================================================

def pick_assess_stats(stats: Optional[Dict[str, Any]]) -> Dict[str, float]:
    if not stats:
        return {}
    return {k: v for k, v in stats.items() if k in ASSESS_KEY_SET}


def get_base_delta(base: float, is_boss_scaling: bool) -> int:
    pos_div = 8 if is_boss_scaling else 10
    neg_div = -600 if is_boss_scaling else -75
    return math.ceil(base / (pos_div if base > 0 else neg_div))


def get_quality_delta(level: int, is_celestial_weapon: bool = False) -> int:
    return level - 10 if level > 10 and not is_celestial_weapon else 0


def get_upgraded_stat_array(
    base: float,
    quality: int,
    is_boss_scaling: bool,
    levels: int,
    key: Optional[str] = None,
    ang_level: int = 0,
) -> List[float]:
    if levels == 0:
        return []

    if key == "crit":
        return [base] * levels

    base_delta = get_base_delta(base, is_boss_scaling)
    is_celestial = levels > 13

    if key == "dexterity":
        return [
            math.ceil(base + (level * base_delta if level > 1 else 0))
            for level in range(1, levels + 1)
        ]

    ang_delta = ang_level * 3 if ang_level > 0 and key not in ANGUISHED_SKIP_KEYS else 0

    out: List[float] = []
    for level in range(1, levels + 1):
        quality_delta = get_quality_delta(level, is_celestial)
        level_bonus = level * base_delta if level > 1 else 0
        v = math.ceil(((base + level_bonus) * (quality + ang_delta + quality_delta)) / 100)
        out.append(v)
    return out


def get_additional_slots(quality: int, level: Optional[int] = None) -> int:
    if level == 13:
        return 4
    if level in (11, 12):
        return 3
    if quality >= 170:
        return 2
    if quality > 100:
        return 1
    return 0


def get_quality_code(quality: int, level: int) -> int:
    if level > 10:
        return level - 4
    if quality > 170:
        return 6
    # 140-170 inclusive is Legendary; Ornate's floor is 171 (see
    # _QUALITY_NAME_TO_PERCENT). in_range is [lo, hi), so the upper bound
    # must be 171, not 170 - otherwise exactly 170 fell through every branch
    # to the final `return 0` (Broken), mislabeling the item AND scaling its
    # bonus stats at -90% instead of ~+20%.
    if in_range(quality, 140, 171):
        return 5
    if in_range(quality, 120, 140):
        return 4
    if in_range(quality, 101, 120):
        return 3
    if quality == 100:
        return 2
    if in_range(quality, 90, 100):
        return 1
    if in_range(quality, 70, 90):
        return 0
    return 0


def get_quality_bonus(
    base: float,
    quality: int,
    quality_code: Optional[int] = None,
    is_adornment: bool = False,
    key: Optional[str] = None,
) -> float:
    if key is None or base == 0:
        return base

    if key in QUALITY_NUMBER_BONUS_KEYS:
        r = (base * quality) / 100
        return r if r < 100 else 100

    if key in QUALITY_CODE_BONUS_KEYS:
        try:
            name = Quality(quality_code).name.lower()
        except (ValueError, TypeError):
            return base
        scaling = BONUS_QUALITY_SCALING[name]

        if is_adornment or key in NO_BASE_BONUS_KEYS:
            return (base * (scaling + 100)) / 100
        return ((100 + base) * (100 + scaling) - 100 * 100) / 100

    return base


# =============================================================================
# Top-level assessment
# =============================================================================

def _meta_flags(entry: CodexEntry) -> Dict[str, bool]:
    return {
        "is_adornment": entry.is_adornment,
        "is_accessory": entry.is_accessory,
        "is_celestial_weapon": entry.is_celestial_weapon,
        "is_two_handed": entry.is_two_handed,
        "is_upgradable": entry.is_upgradable,
        "has_scaling_slots": entry.has_scaling_slots,
    }


def get_assess_result(inp: AssessInput, is_quality_calc: bool = False) -> Optional[AssessResult]:
    entry = inp.entry
    base_stats = entry.stats or {}
    flags = _meta_flags(entry)

    result = AssessResult(
        entry=entry,
        quality=inp.quality,
        quality_code=inp.quality_code,
        boss_scaling=inp.boss_scaling,
        ang_level=inp.ang_level if is_quality_calc else 0,
        stats={},
        levels=0,
    )

    if inp.boss_scaling == 0 and flags["is_upgradable"]:
        return result

    # Every caller passes is_quality_calc=True (the assess UI computes quality
    # itself). The old `else` reverse-engineered quality from OCR'd observed
    # stats but was unreachable dead code - removed here along with its
    # helpers (get_item_quality/_approximate/get_upgraded_stat).
    result.exact = True

    result.quality_code = get_quality_code(result.quality, 1)

    if not flags["is_upgradable"]:
        result.levels = 1
    elif flags["is_celestial_weapon"]:
        result.levels = 20
    else:
        result.levels = 13

    result.stats = {
        k: StatRow(
            base=v,
            values=get_upgraded_stat_array(
                v, result.quality, inp.boss_scaling > 0,
                result.levels, k, result.ang_level,
            ),
        )
        for k, v in pick_assess_stats(base_stats).items()
    }

    if flags["is_upgradable"]:
        if flags["is_celestial_weapon"]:
            slots = list(CELESTIAL_WEAPON_SLOTS)
            if flags["is_two_handed"]:
                slots = [v + 1 for v in slots]
            result.stats["adornment_slots"] = StatRow(
                base=2 if flags["is_two_handed"] else 1,
                values=slots,
            )
            return result

        base_slots = base_stats.get("adornment_slots", 0) or 0
        if flags["has_scaling_slots"]:
            if result.ang_level > 0:
                result.stats["adornment_slots"] = StatRow(
                    base=base_slots,
                    values=[base_slots + 4] * result.levels,
                )
            else:
                additional = get_additional_slots(result.quality)
                head = [base_slots + additional] * (result.levels - 3)
                tail = [base_slots + 3, base_slots + 3, base_slots + 4]
                result.stats["adornment_slots"] = StatRow(
                    base=base_slots,
                    values=head + tail,
                )
        else:
            result.stats["adornment_slots"] = StatRow(
                base=base_slots,
                values=[base_slots] * result.levels,
            )

    return result


def _demo() -> None:
    """`python3 orna_assess.py` - pins get_quality_code's tier boundaries,
    the densest math in this module and the site of a fixed off-by-one at
    exactly 170. in_range is [lo, hi): 140-170 is Legendary (code 5) and
    Ornate's floor is 171 (code 6), so the Legendary upper bound must be 171.
    An edit that drops it back to an exclusive 170 re-opens the gap where 170
    fell through to Broken(0); this check fails loudly if that happens."""
    assert get_quality_code(100, 1) == 2
    assert get_quality_code(139, 1) == 4
    assert get_quality_code(140, 1) == 5
    assert get_quality_code(169, 1) == 5
    assert get_quality_code(170, 1) == 5   # regression guard: was 0 (Broken) before the fix
    assert get_quality_code(171, 1) == 6
    assert get_quality_code(200, 1) == 6
    # level > 10 ignores quality (masterforged/demonforged/godforged = 11/12/13)
    assert get_quality_code(100, 11) == 7
    assert get_quality_code(100, 13) == 9
    print("orna_assess._demo: get_quality_code boundary checks passed")


if __name__ == "__main__":
    _demo()
