"""
Orna GPS RPG - Item Quality Assessment & Stat Prediction
=========================================================

Python port of the TypeScript assess module.
"""

import math
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any, Callable, Dict, List, Optional, Tuple


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

COMMON_SKIP_KEYS = frozenset({"crit", "dexterity", "level", "quality", "angLevel"})
ANGUISHED_BONUS_KEYS = frozenset({"follower_stats", "summon_stats"})
ANGUISHED_SKIP_KEYS = frozenset({"ward", "foresight"})
APPROXIMATION_MAX_DEPTH = 10


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

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "CodexEntry":
        def pick(*keys, default=None):
            for k in keys:
                if k in d:
                    return d[k]
            return default
        return cls(
            name=pick("name", default=""),
            stats=dict(pick("stats", default={}) or {}),
            is_adornment=bool(pick("is_adornment", "isAdornment", default=False)),
            is_accessory=bool(pick("is_accessory", "isAccessory", default=False)),
            is_celestial_weapon=bool(pick("is_celestial_weapon", "isCelestialWeapon", default=False)),
            is_two_handed=bool(pick("is_two_handed", "isTwoHanded", default=False)),
            is_upgradable=bool(pick("is_upgradable", "isUpgradable", default=True)),
            has_scaling_slots=bool(pick("has_scaling_slots", "hasScalingSlots", default=False)),
            boss_scaling=int(pick("boss_scaling", "bossScaling", default=0)),
        )


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
    range: Optional[Tuple[int, int]] = None


@dataclass
class FullResult:
    entry: CodexEntry
    quality: int = 100
    quality_code: int = -1
    boss_scaling: int = 0
    ang_level: int = 0
    level: int = 1
    stats: Dict[str, Any] = field(default_factory=dict)


# =============================================================================
# JS-compat helpers
# =============================================================================

def js_round(x: float) -> int:
    """JS Math.round: ties round toward +infinity."""
    return math.floor(x + 0.5)


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


def get_upgraded_stat(
    base: float,
    level: int,
    quality: int,
    is_boss_scaling: bool,
    is_celestial_weapon: bool = False,
    ang_level: int = 0,
) -> int:
    base_delta = get_base_delta(base, is_boss_scaling)
    quality_delta = get_quality_delta(level, is_celestial_weapon)
    level_bonus = 0 if level == 1 else level * base_delta
    return math.ceil(((base + level_bonus) * (quality + quality_delta + ang_level * 3)) / 100)


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


def _approximate(
    input_value: float,
    base: float,
    initial_test: float,
    initial_quality: int,
    get_stat: Callable[[int], float],
) -> int:
    test = initial_test
    quality = initial_quality
    direction = 0

    for _ in range(APPROXIMATION_MAX_DEPTH):
        delta = test - input_value
        if delta == 0:
            return quality

        direction_fix = 1 if (base > 0) != (delta > 0) else -1
        quality_fix = quality + direction_fix
        fix = get_stat(quality_fix)

        if direction != 0 and direction != direction_fix:
            if abs(fix - input_value) - abs(delta) > 0:
                return quality
            return quality_fix

        direction = direction_fix
        quality = quality_fix
        test = fix
    return 0


def get_item_quality(input_value: float, base: float, level: int, is_boss_scaling: bool) -> int:
    if base == 0:
        return 100
    quality_delta = get_quality_delta(level)
    base_upgraded = get_upgraded_stat(base, level, 100, is_boss_scaling)
    if base_upgraded == 0:
        return 100
    quality = js_round((input_value / base_upgraded) * (100 + quality_delta) - quality_delta)
    test_upgraded = get_upgraded_stat(base, level, quality, is_boss_scaling)
    return _approximate(
        input_value, base_upgraded, test_upgraded, quality,
        lambda fix: get_upgraded_stat(base, level, fix, is_boss_scaling),
    )


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


def make_input(entry: CodexEntry) -> AssessInput:
    return AssessInput(
        entry=entry,
        level=1,
        boss_scaling=-1 if entry.is_celestial_weapon else entry.boss_scaling,
        quality=100,
        quality_code=-1,
        ang_level=0,
        stats=pick_assess_stats(entry.stats),
    )


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

    if is_quality_calc:
        result.exact = True
    else:
        # Pick the largest-magnitude observed stat that ALSO has a non-zero
        # base in the codex. Otherwise we'd try to back-calc quality from a
        # stat the codex doesn't track (base=0), which short-circuits to 100%
        # and produces a meaningless answer.
        observed = [
            (k, v) for k, v in inp.stats.items()
            if k not in COMMON_SKIP_KEYS
            and v != 0                           # 0 observation tells us nothing
            and base_stats.get(k, 0) != 0        # need a base to back-calc against
        ]
        if not observed:
            # No usable signal - leave result.quality at its default and bail.
            return result

        max_key, max_value = max(observed, key=lambda kv: abs(kv[1]))
        base_stat = base_stats[max_key]
        result.quality = get_item_quality(max_value, base_stat, inp.level, inp.boss_scaling > 0)

        def upgraded(q: int) -> int:
            return get_upgraded_stat(base_stat, inp.level, q, inp.boss_scaling > 0)

        current_stat = upgraded(result.quality)
        result.exact = current_stat == max_value

        if result.exact and abs(max_value) < 100 and base_stat != 0:
            sign_offset_neg = -1 if current_stat > 0 else 0
            sign_offset_pos = 0 if current_stat > 0 else 1
            left = math.ceil(((current_stat + sign_offset_neg) / base_stat) * 100)
            right = math.ceil(((current_stat + sign_offset_pos) / base_stat) * 100)
            left_out = upgraded(left)
            right_out = upgraded(right)

            def normalize(n: int, out: int) -> int:
                if current_stat == out:
                    return n
                return n + (1 if n != 0 else -1)

            result.range = (normalize(left, left_out), normalize(right, right_out))

            if current_stat > 0:
                result.quality = max(result.quality, result.range[1])
            else:
                result.quality = min(result.quality, result.range[0])

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


def get_full_result(inp: AssessInput) -> FullResult:
    entry = inp.entry
    base_stats = entry.stats or {}
    flags = _meta_flags(entry)

    result = FullResult(
        entry=entry,
        quality=inp.quality,
        quality_code=inp.quality_code,
        boss_scaling=inp.boss_scaling,
        ang_level=inp.ang_level,
        level=inp.level,
        stats={},
    )

    if inp.boss_scaling == 0 and not (flags["is_accessory"] or flags["is_adornment"]):
        return result
    if not base_stats:
        return result

    assess_result = get_assess_result(inp, is_quality_calc=True)
    if assess_result is None:
        return result

    quality_code = (
        inp.quality_code if inp.quality_code > -1
        else get_quality_code(inp.quality, inp.level)
    )

    for k, v in base_stats.items():
        result.stats[k] = v
        assess_stat = assess_result.stats.get(k)
        if assess_stat is not None:
            if 1 <= inp.level <= len(assess_stat.values):
                result.stats[k] = assess_stat.values[inp.level - 1]
        else:
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                result.stats[k] = get_quality_bonus(
                    v, inp.quality, quality_code, flags["is_adornment"], k,
                )

        if inp.ang_level > 0 and k in ANGUISHED_BONUS_KEYS:
            result.stats["_" + k] = inp.ang_level * 3

    if flags["is_upgradable"]:
        slots_key = "adornment_slots"
        if flags["is_celestial_weapon"]:
            idx = inp.level
            slot = CELESTIAL_WEAPON_SLOTS[idx] if 0 <= idx < len(CELESTIAL_WEAPON_SLOTS) else 0
            result.stats[slots_key] = slot + (1 if flags["is_two_handed"] else 0)
        else:
            base_slots = base_stats.get(slots_key, 0) or 0
            if flags["has_scaling_slots"]:
                if inp.ang_level > 0:
                    result.stats[slots_key] = base_slots + 4
                else:
                    additional = get_additional_slots(inp.quality, inp.level)
                    if additional > 0 or base_slots > 0:
                        result.stats[slots_key] = base_slots + additional
            elif base_slots > 0:
                result.stats[slots_key] = base_slots

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
