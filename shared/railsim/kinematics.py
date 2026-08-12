"""Deterministic train kinematics.

Every quantity the CP-SAT model needs as a *constant* is computed here. Keeping
the physics out of the solver file matters for two reasons: the numbers can be
unit-tested without OR-Tools installed, and a judge can check the arithmetic
against a textbook without reading a constraint model.

Units: SI internally (metres, seconds, m/s). Conversions at the boundary only.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict

# Service (non-emergency) braking and traction limits by stock type.
# Freight accelerates roughly a third as hard as a passenger rake -- this
# asymmetry is the single most important physical fact in the whole model.
@dataclass(frozen=True)
class TractionProfile:
    service_decel_ms2: float
    accel_ms2: float
    train_length_m: float


PROFILES: Dict[str, TractionProfile] = {
    "VANDE_BHARAT": TractionProfile(service_decel_ms2=0.60, accel_ms2=0.55, train_length_m=400.0),
    "RAJDHANI":     TractionProfile(service_decel_ms2=0.55, accel_ms2=0.40, train_length_m=600.0),
    "SHATABDI":     TractionProfile(service_decel_ms2=0.55, accel_ms2=0.42, train_length_m=500.0),
    "DURONTO":      TractionProfile(service_decel_ms2=0.55, accel_ms2=0.40, train_length_m=600.0),
    "TEJAS":        TractionProfile(service_decel_ms2=0.55, accel_ms2=0.42, train_length_m=500.0),
    "GATIMAAN":     TractionProfile(service_decel_ms2=0.58, accel_ms2=0.45, train_length_m=450.0),
    "JAN_SHATABDI": TractionProfile(service_decel_ms2=0.55, accel_ms2=0.40, train_length_m=550.0),
    "GARIB_RATH":   TractionProfile(service_decel_ms2=0.52, accel_ms2=0.36, train_length_m=600.0),
    "MEMU":         TractionProfile(service_decel_ms2=0.60, accel_ms2=0.55, train_length_m=250.0),
    "EMU":          TractionProfile(service_decel_ms2=0.60, accel_ms2=0.60, train_length_m=200.0),
    "SUBURBAN":     TractionProfile(service_decel_ms2=0.60, accel_ms2=0.60, train_length_m=200.0),
    "PARCEL":       TractionProfile(service_decel_ms2=0.45, accel_ms2=0.20, train_length_m=600.0),
    "SUPERFAST": TractionProfile(service_decel_ms2=0.55, accel_ms2=0.40, train_length_m=600.0),
    "EXPRESS":   TractionProfile(service_decel_ms2=0.50, accel_ms2=0.35, train_length_m=600.0),
    "PASSENGER": TractionProfile(service_decel_ms2=0.50, accel_ms2=0.30, train_length_m=450.0),
    "SPECIAL":   TractionProfile(service_decel_ms2=0.50, accel_ms2=0.35, train_length_m=600.0),
    "FREIGHT":   TractionProfile(service_decel_ms2=0.40, accel_ms2=0.12, train_length_m=700.0),
}

DEFAULT_PROFILE = PROFILES["EXPRESS"]

#: A driver regulating rather than stopping will not go below this fraction of
#: line speed -- below it, the block section is better cleared by stopping.
MIN_REGULATION_FRACTION = 0.35


def profile_for(train_type: str) -> TractionProfile:
    return PROFILES.get(str(train_type).upper(), DEFAULT_PROFILE)


def kmh_to_ms(kmh: float) -> float:
    return kmh / 3.6


def ms_to_kmh(ms: float) -> float:
    return ms * 3.6


def braking_distance_m(speed_ms: float, decel_ms2: float) -> float:
    """v^2 / 2a -- the distance consumed bringing the train to a stand."""
    if speed_ms <= 0:
        return 0.0
    return (speed_ms ** 2) / (2.0 * decel_ms2)


def traverse_seconds_accelerating(
    length_m: float,
    entry_ms: float,
    target_ms: float,
    accel_ms2: float,
) -> float:
    """Time to cover `length_m`, accelerating from `entry_ms` toward `target_ms`.

    The general case. `traverse_seconds_running` is entry == target and
    `traverse_seconds_from_stop` is entry == 0; both are kept as named wrappers
    because the call sites read better, not because they compute anything this
    does not.

    Instantaneous speed is the wrong basis for a projection minutes long: a
    freight pulling away from a loop at 5 km/h does not take eight hours to
    clear a 40 km section. Entry speed is where the train actually is, so a
    train standing at a signal starts from zero rather than being clamped to a
    fictional crawl.
    """
    if length_m <= 0:
        return 0.0

    vt = max(target_ms, entry_ms)
    if vt <= 0:
        return math.inf
    if entry_ms >= vt or accel_ms2 <= 0:
        return math.inf if entry_ms <= 0 else length_m / entry_ms

    accel_distance = (vt ** 2 - entry_ms ** 2) / (2.0 * accel_ms2)
    if length_m <= accel_distance:
        return (-entry_ms + math.sqrt(entry_ms ** 2 + 2.0 * accel_ms2 * length_m)) / accel_ms2
    return (vt - entry_ms) / accel_ms2 + (length_m - accel_distance) / vt


def stop_restart_penalty_s(speed_ms: float, decel_ms2: float, accel_ms2: float) -> float:
    """Time lost to a full stop and restart, EXCLUDING the stand time itself.

    Derivation, braking phase:
        distance covered   d_b = v^2 / 2a_b
        time taken         t_b = v / a_b
        time at line speed d_b / v = v / 2a_b
        lost               t_b - d_b/v = v / 2a_b

    The acceleration phase is symmetric with a_a. Total lost time is therefore

        v / 2a_b  +  v / 2a_a

    For a 110 km/h express (a_b=0.50, a_a=0.35) that is ~74 s. For a 45 km/h
    freight (a_b=0.40, a_a=0.12) it is ~68 s -- despite the freight being less
    than half the speed, because its traction is so much weaker. This is the
    term that makes "hold the freight" non-free, and it is why the model cannot
    be reduced to a simple priority comparison.
    """
    if speed_ms <= 0:
        return 0.0
    return speed_ms / (2.0 * decel_ms2) + speed_ms / (2.0 * accel_ms2)


def traverse_seconds_running(length_m: float, speed_ms: float) -> float:
    """Time to clear a block at constant speed, from head-in to tail-out."""
    return traverse_seconds_accelerating(length_m, speed_ms, speed_ms, 0.0)


def traverse_seconds_from_stop(length_m: float, target_ms: float, accel_ms2: float) -> float:
    """Time to clear a block starting from a stand at the block entry.

    A stopped train occupies the bottleneck LONGER than a running one, which is
    a real operational cost of holding: the held train then blocks the section
    for the next train too.
    """
    return traverse_seconds_accelerating(length_m, 0.0, target_ms, accel_ms2)


def earliest_arrival_s(distance_m: float, speed_ms: float) -> float:
    """Unimpeded run time to the bottleneck. The zero-delay datum."""
    if speed_ms <= 0:
        return math.inf
    return distance_m / speed_ms


def absorbable_delay_s(
    distance_m: float,
    speed_ms: float,
    min_fraction: float = MIN_REGULATION_FRACTION,
) -> float:
    """Seconds a train can shed on the approach WITHOUT coming to a stand.

    Running the approach at f*v instead of v takes d/(f*v) instead of d/v, so
    the recoverable slack is

        d/(f*v) - d/v  =  (d/v) * (1/f - 1)

    Any wait longer than this forces a stop, and only then does the stop/restart
    penalty apply. This is precisely the difference between the two dispatch
    actions a controller chooses between: regulate, or hold.
    """
    if speed_ms <= 0 or distance_m <= 0:
        return 0.0
    return (distance_m / speed_ms) * (1.0 / min_fraction - 1.0)


def regulated_speed_kmh(distance_m: float, speed_ms: float, wait_s: float) -> float:
    """Line speed a regulated train should be given to absorb `wait_s` exactly."""
    if speed_ms <= 0 or distance_m <= 0 or wait_s <= 0:
        return ms_to_kmh(speed_ms)
    base = distance_m / speed_ms
    return ms_to_kmh(distance_m / (base + wait_s))