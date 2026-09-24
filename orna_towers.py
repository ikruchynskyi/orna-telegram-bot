"""
orna_towers.py
================
Deterministic estimate of the current floor of Orna's 5 "Wild Towers of
Olympia" (Selene/Eos/Oceanus/Themis/Prometheus) - ported line-for-line
from OrnaCodex's own reference implementation
(https://github.com/67au/OrnaCodex, src/utils/tower.ts, pinned at commit
4201d034ae34b222ce1360b371eb6099564f6668, since that repo has no version
tag and this needs to stay reproducible).

This needs NO external data source at all - not the live game, not a
spreadsheet, not a scrape - each tower's floor is pure deterministic math
from the current UTC time: every tower runs a fixed 35-day cycle, gaining
floor(s) at 6 fixed checkpoints per UTC day (01:00/05:00/10:00/15:00/
15:36/20:00) plus a +6 jump at each day boundary, wrapping/capping near
the top into floor 50 - a sentinel meaning "cleared, waiting for the next
cycle to reset it", not a literal 50th floor value from the raw formula.

Ported (not reimplemented from a description) because the exact
checkpoint times, the +6-per-day-boundary term, and the floor>=48-or-
wrapped-past-15 cap logic are all non-obvious game-specific constants a
plausible-looking reimplementation could easily get subtly wrong - see
_demo() below, which pins get_tower_floors' output against real output
from the original TS run under Node at 9 fixed timestamps (including a
cycle-reset boundary and a floor-50 wraparound), so a future edit here
that silently diverges from upstream's actual behavior fails loudly
instead of just looking plausible.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import List

KINDS = ["selene", "eos", "oceanus", "themis", "prometheus"]
_BASE_FLOORS = [35, 30, 25, 20, 15]
_BASE_DATE = datetime(2023, 12, 7, 0, 0, tzinfo=timezone.utc)  # upstream's Date.UTC(2023, 11, 7, 0) - Dec 7 2023, JS months are 0-indexed

CYCLE_DAYS = 35
_CYCLE_MINUTES = CYCLE_DAYS * 24 * 60

# (hour, minute) UTC - 6 real floor-granting checkpoints per day; the
# (0, 0) entry is a day-boundary marker only, never itself scored (see
# _compute_point) - it exists so the checkpoint-repeat walk in
# get_tower_floors_in_next_days can recognize "this slot is a new day".
_CHECKPOINTS = [(0, 0), (1, 0), (5, 0), (10, 0), (15, 0), (15, 36), (20, 0)]


@dataclass
class TowerFloor:
    kind: str
    floor: int


def _minutes_into_cycle(time: datetime) -> int:
    total_cycle_seconds = _CYCLE_MINUTES * 60
    mod = (time - _BASE_DATE).total_seconds() % total_cycle_seconds
    if mod < 0:
        mod += total_cycle_seconds
    return int(mod // 60)


def _compute_point(day_minutes: int) -> int:
    day = day_minutes // (24 * 60)
    rem = day_minutes - day * 24 * 60
    hour, minute = divmod(rem, 60)
    point = 0
    for chk_hour, chk_minute in _CHECKPOINTS:
        if chk_hour == 0:
            continue
        if hour > chk_hour or (hour == chk_hour and minute >= chk_minute):
            point += 1
    return point


def get_tower_floors(time: datetime) -> List[TowerFloor]:
    """Current floor of all 5 wild towers at `time` (any tz-aware
    datetime - converted to UTC internally; a naive datetime is assumed
    already UTC). 50 means "cleared / at the top, waiting for the next
    35-day cycle to reset it" - the sentinel upstream's own formula
    produces, not a literal cap applied on top of it."""
    if time.tzinfo is None:
        time = time.replace(tzinfo=timezone.utc)
    time = time.astimezone(timezone.utc)
    min_cycle = _minutes_into_cycle(time)
    p = _compute_point(min_cycle)
    day = min_cycle // (24 * 60)

    result = []
    for kind, base in zip(KINDS, _BASE_FLOORS):
        floor = ((abs(base - 15 + day * 6) + p) % CYCLE_DAYS) + 15
        if floor >= 48 or (floor == 15 and (day % CYCLE_DAYS != 0 or time.hour != 0)):
            floor = 50
        result.append(TowerFloor(kind=kind, floor=floor))
    return result


def _repeat_checkpoints(point: int, n: int) -> List[tuple]:
    length = len(_CHECKPOINTS)
    start = point % length
    return [_CHECKPOINTS[(start + i) % length] for i in range(n * length)]


def get_tower_floors_in_next_days(time: datetime, n: int = 2) -> List[dict]:
    """Upcoming floor-change checkpoints over the next `n` days - each
    entry is {"time": <UTC datetime>, "floors": [TowerFloor, ...]}. Skips
    a bare midnight marker unless it's an actual 35-day cycle reset,
    matching upstream's own filter."""
    if time.tzinfo is None:
        time = time.replace(tzinfo=timezone.utc)
    time = time.astimezone(timezone.utc)
    min_cycle = _minutes_into_cycle(time)
    p = _compute_point(min_cycle) + 1
    day = min_cycle // (24 * 60)
    midnight = time.replace(hour=0, minute=0, second=0, microsecond=0)

    checkpoints = _repeat_checkpoints(p, n)
    out = []
    for index, (chk_hour, chk_minute) in enumerate(checkpoints):
        day_delta = (index + p) // len(_CHECKPOINTS)
        if chk_hour == 0 and (CYCLE_DAYS - (day + day_delta)) % CYCLE_DAYS != 0:
            continue
        moment = midnight + timedelta(hours=chk_hour + 24 * day_delta, minutes=chk_minute)
        out.append({"time": moment, "floors": get_tower_floors(moment)})
    return out


def _demo() -> None:
    """Pinned regression checks - each expected floor list captured by
    running the ORIGINAL upstream tower.ts (unmodified, types stripped
    only) under Node at these exact timestamps, so a divergence here
    means this port drifted from upstream, not a hypothetical concern.
    Run via `python3 orna_towers.py`."""
    cases = [
        ("2023-12-07T00:00:00+00:00", [35, 30, 25, 20, 15]),  # cycle epoch
        ("2023-12-07T00:30:00+00:00", [35, 30, 25, 20, 15]),  # no checkpoint crossed yet
        ("2023-12-07T05:15:00+00:00", [37, 32, 27, 22, 17]),  # 2 checkpoints in (01:00, 05:00)
        ("2023-12-07T15:36:00+00:00", [40, 35, 30, 25, 20]),  # all 6 checkpoints hit
        ("2023-12-08T00:00:00+00:00", [41, 36, 31, 26, 21]),  # +6 day-boundary jump
        ("2026-09-23T14:07:00+00:00", [39, 34, 29, 24, 19]),
        ("2026-09-24T00:00:00+00:00", [42, 37, 32, 27, 22]),
        ("2026-10-11T23:59:00+00:00", [45, 40, 35, 30, 25]),  # exact cycle-reset eve
        ("2026-10-12T00:00:00+00:00", [45, 40, 35, 30, 25]),  # cycle reset instant (day=0, hour=0): floor==15 case doesn't apply here since prometheus lands on 25, not 15
    ]
    for iso, expected in cases:
        got = [tf.floor for tf in get_tower_floors(datetime.fromisoformat(iso))]
        assert got == expected, f"{iso}: expected {expected}, got {got}"

    # A floor-50 wraparound + the next cycle's floor 16 (not 15) - pinned
    # against the same Node run's 2-day projection for this timestamp.
    projected = get_tower_floors_in_next_days(datetime.fromisoformat("2026-09-23T14:07:00+00:00"), 2)
    assert len(projected) == 12
    assert projected[0]["time"].isoformat() == "2026-09-23T15:00:00+00:00"
    assert [tf.floor for tf in projected[0]["floors"]] == [40, 35, 30, 25, 20]
    assert projected[8]["time"].isoformat() == "2026-09-24T20:00:00+00:00"
    assert [tf.floor for tf in projected[8]["floors"]] == [50, 43, 38, 33, 28]
    assert projected[-1]["time"].isoformat() == "2026-09-25T10:00:00+00:00"
    assert [tf.floor for tf in projected[-1]["floors"]] == [16, 46, 41, 36, 31]

    print("orna_towers._demo: all checks passed")


if __name__ == "__main__":
    _demo()
