#!/usr/bin/env python3
"""Deterministic virtual dyno for the 2023 Low Rider ST (FXLRST), M8 131ci,
2-into-1, ThunderMax TBW.

WHAT THIS IS
------------
A first-order physics model of the engine. Joshua feeds it a proposed table
change and watches a simulated WOT pull with live gauges BEFORE the change goes
anywhere near the bike. It is pure arithmetic: **no LLM, no network, no NAS, no
Ollama**. Same input -> same output, every time, forever.

WHAT THIS IS NOT
----------------
A dyno. Every result carries a permanent banner saying so and an uncertainty
band (data/dyno_baseline.json -> uncertainty_pct). Deltas are reported ONLY as
integer ranges ("~ +3 to +5 hp") because the model is not accurate enough to
justify a decimal point.

AUTHORITY
---------
Every issue this module emits is severity "warn" — advisory only. **Hard-block
authority belongs solely to guardrails.check_change() / check_proposal()** on
the proposed steady-state values. A simulated pull can never block, and can
never clear, a proposal.

SOURCES OF NUMBERS
------------------
* Safety limits (AFR windows, spark ceiling/step, VE steps, temp gates, duty
  amber/red, TIMING_BACKBONE, BELLY_DERATE_DEG) -> imported from guardrails.py.
  This module NEVER redefines a guardrail limit.
* Calibration (torque anchors, injector flow, gearing, vehicle mass, ambient
  defaults) -> data/dyno_baseline.json, every entry source-commented there.
* Model shape constants (knock weights, MBT curve, AFR/timing power curves)
  -> this file, each with unit + rationale in the comment above it.

MODEL SUMMARY
-------------
1. baseline_torque(rpm) — monotone cubic Hermite (PCHIP) through the published
   131-kit anchors. Those anchors were measured WITH the baseline's own AFR and
   timing already baked in.
2. Therefore AFR and timing factors are applied as a **ratio against the
   baseline state**, never absolutely:
       torque = baseline_torque(rpm)
                * afr_power_factor(afr_now)     / afr_power_factor(afr_base)
                * timing_power_factor(now-MBT)  / timing_power_factor(base-MBT)
   With no changes both ratios are exactly 1.0, so the model reproduces the
   published curve bit-for-bit (the identity property).
3. A VE / fuel-table edit is modelled as an **AFR SHIFT ONLY** (afr_shift()).
   It is NEVER a torque multiplier. An earlier draft multiplied torque by
   (1 + 0.9*dVE/100) *and* ran the AFR curve, double-counting and
   over-predicting by 4-5x. Torque impact from fuel flows ONLY through
   afr_power_factor.

Stdlib only. Import-safe (the JSON is read lazily and memoized).
"""

from __future__ import annotations

import json
import math
import os
from pathlib import Path

import guardrails as G

# --------------------------------------------------------------------------
# Calibration loading
# --------------------------------------------------------------------------

# data/dyno_baseline.json lives one level up from src/.
BASELINE_PATH = Path(
    os.environ.get(
        "TMAX_DYNO_BASELINE",
        str(Path(__file__).resolve().parent.parent / "data" / "dyno_baseline.json"),
    )
)

_BASELINE_CACHE = None


def load_baseline():
    """Read + memoize data/dyno_baseline.json. Returns the raw dict.

    Memoized because a long-running server calls this on every pull; the file
    is small and never changes at runtime. Override the path with the
    TMAX_DYNO_BASELINE env var (tests use it).
    """
    global _BASELINE_CACHE
    if _BASELINE_CACHE is None:
        with open(BASELINE_PATH, "r", encoding="utf-8") as fh:
            _BASELINE_CACHE = json.load(fh)
        _apply_profile_injectors(_BASELINE_CACHE)
    return _BASELINE_CACHE


def _apply_profile_injectors(cal):
    """The bike's injector flow lives in bike_profile.json — that is the file
    that tracks the actual hardware. Duplicating it here let the two disagree:
    this file was seeded 5.5 g/s from an older corpus note while the 2026-08-19
    USB pull had already moved the bike to 6.3 g/s injectors. Duty scales
    INVERSELY with flow, so a stale value mis-reads every simulated pull. The
    profile wins; the baseline entry is only the fallback.
    """
    try:
        prof_path = Path(__file__).resolve().parent / "bike_profile.json"
        prof = json.loads(prof_path.read_text())
    except (OSError, ValueError):
        return
    inj = prof.get("injectors")
    if isinstance(inj, dict) and inj.get("flow_gps"):
        cal["injectors"] = {**cal.get("injectors", {}), **inj,
                            "from": "bike_profile.json"}


def _reset_baseline_cache():
    """Test hook — drop the memoized calibration."""
    global _BASELINE_CACHE
    _BASELINE_CACHE = None


# --------------------------------------------------------------------------
# Model-shape constants (NOT safety limits — those all live in guardrails.py)
# --------------------------------------------------------------------------

# --- AFR power curve ------------------------------------------------------
# Plateau: an M8 makes its best torque between roughly 12.6 and 13.0 AFR.
AFR_PLATEAU = (12.6, 13.0)          # AFR — loss-free band
# Transition zones. Inside them the *penalty rate* ramps linearly from zero at
# the plateau edge up to the full rate at the outer edge, so the curve is
# continuous AND kink-free, and the shop's 12.4-12.8 WOT safety window comes
# out effectively loss-free (<0.05% at 12.4).
AFR_LEAN_RAMP = (13.0, 13.2)        # AFR — ramp in the lean direction
AFR_RICH_RAMP = (12.2, 12.6)        # AFR — ramp in the rich direction
AFR_LEAN_RATE = 0.02                # fraction of torque lost per AFR point above 13.2
AFR_RICH_RATE_MILD = 0.01           # per AFR point across 12.0-12.2
AFR_RICH_RATE_HARD = 0.02           # per AFR point below 12.0
AFR_RICH_KNEE = 12.0                # AFR where the rich penalty steepens
AFR_FACTOR_FLOOR = 0.60             # never model more than a 40% AFR loss

# --- Timing power curve ---------------------------------------------------
# 1 - 0.0006 * deg^2 : ~1.5% loss at 5 deg from MBT, ~6% at 10 deg. Standard
# textbook spark-sweep shape for a slow-burn, long-stroke pushrod twin.
TIMING_LOSS_K = 0.0006              # torque fraction lost per (deg from MBT)^2
TIMING_MAX_LOSS = 0.25              # clamp: never model worse than a 25% loss
# MBT (minimum advance for best torque) falls as load rises — more charge, more
# turbulence, faster burn. ~34 deg at light load, ~28 deg at WOT.
MBT_DEG_LOW_LOAD = 34.0             # deg BTDC at ~0% TPS
MBT_DEG_WOT = 28.0                  # deg BTDC at 100% TPS
# Above this TPS the cylinder is knock-limited: advancing PAST model-MBT buys
# ~nothing in torque while knock risk keeps climbing (the asymmetry).
KNOCK_LIMITED_TPS = 40.0            # %TPS

# --- Knock model ----------------------------------------------------------
# knock_risk is a 0..1 *index*, not a probability. Weights sum to 1.0 so a
# fully-saturated worst case reads 1.00. Each term is documented at its use.
KNOCK_W_ADVANCE = 0.55              # advance past the validated backbone reference
KNOCK_W_LEAN = 0.25                 # lean-of-window AFR under load
KNOCK_W_CHT = 0.20                  # cylinder-head temperature past the 226 F knee
KNOCK_ADVANCE_SPAN_DEG = 8.0        # deg over backbone that saturates the advance term
KNOCK_LEAN_SPAN_AFR = 1.5           # AFR points leaner than the hard window that saturates
KNOCK_CHT_SPAN_F = 60.0             # degF over the 226 F knee that saturates
KNOCK_REAR_BIAS = 0.06              # rear jug runs hotter -> +0.06 index, same inputs
# Cylinder pressure (hence knock) scales with load. At closed throttle knock is
# essentially impossible; the floor keeps the term from vanishing entirely.
KNOCK_LOAD_FLOOR = 0.25             # index multiplier at 0% TPS (1.0 at 100% TPS)
KNOCK_WARN = 0.50                   # index -> "elevated" advisory
KNOCK_HIGH = 0.75                   # index -> "high" advisory

# --- Physical constants ---------------------------------------------------
R_DRY_AIR = 287.058                 # J/(kg*K) — specific gas constant, dry air
PA_PER_INHG = 3386.389              # Pa per inHg (exact-ish, 0 degC mercury)
HP_TORQUE_CONST = 5252.0            # hp = lb-ft * rpm / 5252
NM_PER_LBFT = 1.35581795            # N*m per lb-ft
MPS_TO_MPH = 2.23693629             # mph per m/s
G_ACCEL = 9.80665                   # m/s^2 — standard gravity


# --------------------------------------------------------------------------
# Interpolation helpers
# --------------------------------------------------------------------------

def _lerp_table(points, x):
    """Piecewise-linear interpolation over sorted [(x, y), ...]. Clamps at the
    ends. Used for the timing backbone (a lookup reference, not a smooth curve).
    """
    if x <= points[0][0]:
        return float(points[0][1])
    if x >= points[-1][0]:
        return float(points[-1][1])
    for (x0, y0), (x1, y1) in zip(points, points[1:]):
        if x0 <= x <= x1:
            if x1 == x0:
                return float(y0)
            return float(y0 + (y1 - y0) * (x - x0) / (x1 - x0))
    return float(points[-1][1])


def _pchip(points, x):
    """Monotone cubic Hermite (PCHIP) interpolation over sorted [(x, y), ...].

    C1-smooth and overshoot-free, so the baseline torque curve passes exactly
    through the published anchors without inventing a bump between them.
    Endpoints clamp.
    """
    n = len(points)
    if n == 1:
        return float(points[0][1])
    xs = [float(p[0]) for p in points]
    ys = [float(p[1]) for p in points]
    if x <= xs[0]:
        return ys[0]
    if x >= xs[-1]:
        return ys[-1]

    h = [xs[i + 1] - xs[i] for i in range(n - 1)]
    d = [(ys[i + 1] - ys[i]) / h[i] for i in range(n - 1)]

    m = [0.0] * n
    m[0] = d[0]
    m[-1] = d[-1]
    for i in range(1, n - 1):
        if d[i - 1] * d[i] <= 0:
            m[i] = 0.0            # local extremum -> flat tangent, no overshoot
        else:
            w1 = 2.0 * h[i] + h[i - 1]
            w2 = h[i] + 2.0 * h[i - 1]
            m[i] = (w1 + w2) / (w1 / d[i - 1] + w2 / d[i])

    # locate the interval
    i = 0
    for k in range(n - 1):
        if xs[k] <= x <= xs[k + 1]:
            i = k
            break
    t = (x - xs[i]) / h[i]
    t2, t3 = t * t, t * t * t
    h00 = 2 * t3 - 3 * t2 + 1
    h10 = t3 - 2 * t2 + t
    h01 = -2 * t3 + 3 * t2
    h11 = t3 - t2
    return (h00 * ys[i] + h10 * h[i] * m[i]
            + h01 * ys[i + 1] + h11 * h[i] * m[i + 1])


def _clamp(v, lo, hi):
    return lo if v < lo else (hi if v > hi else v)


# --------------------------------------------------------------------------
# Baseline engine curves
# --------------------------------------------------------------------------

def baseline_torque(rpm):
    """Crank torque in lb-ft at the BASELINE AFR/timing state (published 131
    kit anchors, PCHIP-interpolated)."""
    b = load_baseline()["baseline"]
    return _pchip([(p[0], p[1]) for p in b["torque_curve"]], float(rpm))


def peak_baseline_torque():
    """(rpm, lb-ft) of the published torque peak."""
    b = load_baseline()["baseline"]
    best = max(b["torque_curve"], key=lambda p: p[1])
    return float(best[0]), float(best[1])


def baseline_ve(rpm):
    """Baseline volumetric efficiency as a fraction (1.00 = 100%).

    Torque is proportional to trapped air mass at fixed AFR/timing, so the
    normalised torque curve IS the VE curve. ve_peak is a documented estimate
    (see dyno_baseline.json -> engine.ve_peak_source).
    """
    eng = load_baseline()["engine"]
    _, t_peak = peak_baseline_torque()
    return float(eng["ve_peak"]) * baseline_torque(rpm) / t_peak


def reference_spark(rpm, tps_pct):
    """Reference (baseline) spark advance in degrees BTDC.

    Built from guardrails.TIMING_BACKBONE (the validated rpm->degrees curve,
    source: thundermax_131st_timing_backbone_verification.md), minus
    guardrails.BELLY_DERATE_DEG inside the knock-prone midrange belly, then
    capped to the top of the house WOT spark window at high load — the backbone
    is a general-load reference and the shop never runs 34 deg at wide-open
    throttle.
    """
    ref = _lerp_table(G.TIMING_BACKBONE, float(rpm))
    if (G.BELLY_RPM[0] <= rpm <= G.BELLY_RPM[1]
            and G.BELLY_TPS[0] <= tps_pct <= G.BELLY_TPS[1]):
        ref -= G.BELLY_DERATE_DEG
    if tps_pct >= 80.0:
        ref = min(ref, G.SPARK_WOT[1])
    return ref


def mbt_deg(tps_pct):
    """Model MBT (minimum advance for best torque), degrees BTDC.

    Linear in load between MBT_DEG_LOW_LOAD and MBT_DEG_WOT.
    """
    f = _clamp(float(tps_pct), 0.0, 100.0) / 100.0
    return MBT_DEG_LOW_LOAD + (MBT_DEG_WOT - MBT_DEG_LOW_LOAD) * f


# --------------------------------------------------------------------------
# Core physics — the pieces the API and the tests call directly
# --------------------------------------------------------------------------

def afr_shift(dVE_pct, afr_target):
    """Delivered-AFR shift (AFR points) caused by a VE / fuel-table edit.

    THE single definition of how a fuel edit moves AFR. Everything that needs
    it calls this — never a second copy.

        fuel_new = fuel * (1 + dVE/100)
        AFR_new  = air / fuel_new = AFR * 100 / (100 + dVE)
        shift    = AFR_new - AFR  = -AFR * dVE / (100 + dVE)

    Adding fuel (dVE > 0) returns a NEGATIVE shift (richer / lower AFR).
    Removing fuel returns a POSITIVE shift (leaner / higher AFR).

    A VE edit is modelled as an AFR shift and NOTHING ELSE. It is never a
    torque multiplier; its torque effect flows only through afr_power_factor().
    """
    dve = float(dVE_pct)
    denom = 100.0 + dve
    if denom <= 0:
        # Removing >=100% of the fuel is nonsense; clamp so the model cannot
        # divide by zero on a malformed change.
        return -float(afr_target)
    return -float(afr_target) * dve / denom


def afr_power_factor(afr):
    """Torque multiplier (0..1) from delivered AFR alone.

    Shape:
      * 12.6-13.0            -> 1.0 flat (best-torque plateau)
      * 13.0-13.2 and
        12.2-12.6            -> transition: the penalty RATE ramps linearly
                                from 0 at the plateau edge to the full rate at
                                the outer edge. Continuous and kink-free, and
                                the shop's 12.4-12.8 WOT window comes out
                                loss-free to within 0.05%.
      * above 13.2           -> -2% per AFR point (AFR_LEAN_RATE)
      * 12.0-12.2            -> -1% per AFR point (AFR_RICH_RATE_MILD)
      * below 12.0           -> -2% per AFR point (AFR_RICH_RATE_HARD)
    Clamped at AFR_FACTOR_FLOOR.
    """
    a = float(afr)
    lo_p, hi_p = AFR_PLATEAU
    if lo_p <= a <= hi_p:
        return 1.0

    if a > hi_p:
        r0, r1 = AFR_LEAN_RAMP                     # 13.0 -> 13.2
        span = r1 - r0
        # Penalty accumulated across the whole ramp = area of the triangle.
        ramp_penalty = 0.5 * AFR_LEAN_RATE * span
        if a <= r1:
            frac = (a - r0) / span
            loss = 0.5 * AFR_LEAN_RATE * span * frac * frac
        else:
            loss = ramp_penalty + AFR_LEAN_RATE * (a - r1)
    else:
        r0, r1 = AFR_RICH_RAMP                     # 12.2 -> 12.6
        span = r1 - r0
        ramp_penalty = 0.5 * AFR_RICH_RATE_MILD * span
        if a >= r0:
            frac = (r1 - a) / span
            loss = 0.5 * AFR_RICH_RATE_MILD * span * frac * frac
        elif a >= AFR_RICH_KNEE:
            loss = ramp_penalty + AFR_RICH_RATE_MILD * (r0 - a)
        else:
            loss = (ramp_penalty
                    + AFR_RICH_RATE_MILD * (r0 - AFR_RICH_KNEE)
                    + AFR_RICH_RATE_HARD * (AFR_RICH_KNEE - a))

    return max(AFR_FACTOR_FLOOR, 1.0 - loss)


def timing_power_factor(deg_from_mbt, tps_pct=None):
    """Torque multiplier (<=1.0) from spark timing.

        factor = 1 - TIMING_LOSS_K * deg^2      (clamped to TIMING_MAX_LOSS)

    ~1.5% loss at 5 deg from MBT, ~6% at 10 deg, never worse than 25%.

    `deg_from_mbt` is SIGNED: negative = retarded from MBT, positive = advanced
    past MBT.

    ASYMMETRY (this is deliberate and load-dependent): at mid/high load
    (tps_pct >= KNOCK_LIMITED_TPS) the cylinder is knock-limited, so advancing
    PAST model-MBT returns ~zero extra torque — the factor simply stays at 1.0
    — while knock_risk() keeps climbing. That is the whole point: the model
    must never pay you torque for advance that only buys you detonation.
    At low load (or when tps_pct is not supplied) over-advance is treated
    symmetrically, since a lightly-loaded cylinder really does lose torque when
    the burn peaks too early.
    """
    d = float(deg_from_mbt)
    if d > 0 and tps_pct is not None and float(tps_pct) >= KNOCK_LIMITED_TPS:
        return 1.0
    loss = TIMING_LOSS_K * d * d
    return 1.0 - min(loss, TIMING_MAX_LOSS)


def rho_air(conditions=None):
    """Intake-air density in **g/cm^3** (NOT kg/m^3 — the injector closed form
    below multiplies it by a cylinder volume in cm^3).

        rho[kg/m^3] = P / (R_dry * T)      ideal gas, dry air
        rho[g/cm^3] = rho[kg/m^3] * 1e-3

    P from conditions["baro_inhg"] (inHg -> Pa), T from conditions["iat_f"]
    (degF -> K), falling back to ambient_f + iat_rise_f. Humidity is ignored:
    it moves density by <1%, well inside the +/-15% band.
    """
    c = merge_conditions(conditions)
    t_f = c["iat_f"]
    if t_f is None:
        t_f = c["ambient_f"] + c["iat_rise_f"]
    t_k = (float(t_f) - 32.0) * 5.0 / 9.0 + 273.15
    p_pa = float(c["baro_inhg"]) * PA_PER_INHG
    rho_kg_m3 = p_pa / (R_DRY_AIR * t_k)
    return rho_kg_m3 * 1e-3


def per_cylinder_cc():
    """Swept volume of ONE cylinder in cm^3 (131 ci / 2 cylinders)."""
    eng = load_baseline()["engine"]
    total_cc = float(eng["displacement_ci"]) * float(eng["ci_to_cc"])
    return total_cc / float(eng["cylinders"])


def injector_duty_pct(rpm, ve, afr, conditions=None):
    """Injector duty cycle in percent, closed form.

        air_gps_per_cyl = per_cyl_cc * VE * rho_air(conditions) * rpm/120
        fuel_gps        = air_gps_per_cyl / AFR
        duty%           = min(100, fuel_gps / injector_flow_gps * 100)

    rpm/120 is the four-stroke intake-event rate: one induction per cylinder
    per two crank revolutions, so events/second = rpm/60/2.

    `ve` is a FRACTION (0.90 = 90%). Injector flow comes from
    dyno_baseline.json -> injectors.flow_gps (5.5 g/s, sourced to the shop's
    own tune notes; the stock ~4.4 g/s value would false-red every pull).
    """
    inj = load_baseline()["injectors"]
    flow = float(inj["flow_gps"])
    air_gps_per_cyl = (per_cylinder_cc() * float(ve) * rho_air(conditions)
                       * float(rpm) / 120.0)
    fuel_gps = air_gps_per_cyl / float(afr)
    return min(100.0, fuel_gps / flow * 100.0)


def knock_risk(rpm, tps_pct, spark_deg, afr, cht_f, cylinder="front"):
    """Knock-risk INDEX in 0..1 (not a probability). See knock_breakdown()
    for the per-term contributions this is built from."""
    return knock_breakdown(rpm, tps_pct, spark_deg, afr, cht_f, cylinder)["risk"]


def knock_breakdown(rpm, tps_pct, spark_deg, afr, cht_f, cylinder="front"):
    """Documented breakdown behind knock_risk().

    Terms (each 0..1 before weighting):
      advance : (spark_deg - reference_spark) / KNOCK_ADVANCE_SPAN_DEG.
                reference_spark is the guardrails TIMING_BACKBONE curve
                (linearly interpolated) minus guardrails.BELLY_DERATE_DEG in
                the corpus "belly" band. Running the validated backbone scores
                zero; 8 deg over it saturates.  weight KNOCK_W_ADVANCE
      lean    : how far past guardrails.AFR_WOT_HARD[1] (13.2) the delivered
                AFR sits, over KNOCK_LEAN_SPAN_AFR.  weight KNOCK_W_LEAN
      cht     : how far past guardrails.HEAT_RETARD_CHT_F (226 degF, the
                sourced heat-retard knee) the head sits, over KNOCK_CHT_SPAN_F.
                weight KNOCK_W_CHT
    The advance and lean terms are then scaled by load
    (KNOCK_LOAD_FLOOR..1.0 across 0..100% TPS) because knock needs cylinder
    pressure. The CHT term is not load-scaled: a heat-soaked head is hot
    regardless of throttle.
    Finally the rear cylinder gets KNOCK_REAR_BIAS added — it sits in the
    front jug's heat shadow and runs hotter on the same numbers.
    """
    ref = reference_spark(rpm, tps_pct)
    excess_deg = float(spark_deg) - ref
    t_advance = _clamp(excess_deg / KNOCK_ADVANCE_SPAN_DEG, 0.0, 1.0)

    lean_edge = G.AFR_WOT_HARD[1]
    t_lean = _clamp((float(afr) - lean_edge) / KNOCK_LEAN_SPAN_AFR, 0.0, 1.0)

    t_cht = _clamp((float(cht_f) - G.HEAT_RETARD_CHT_F) / KNOCK_CHT_SPAN_F,
                   0.0, 1.0)

    load = KNOCK_LOAD_FLOOR + (1.0 - KNOCK_LOAD_FLOOR) * (
        _clamp(float(tps_pct), 0.0, 100.0) / 100.0)

    c_advance = KNOCK_W_ADVANCE * t_advance * load
    c_lean = KNOCK_W_LEAN * t_lean * load
    c_cht = KNOCK_W_CHT * t_cht
    # The rear bias is load-scaled too — at closed throttle neither jug knocks.
    bias = (KNOCK_REAR_BIAS * load) if str(cylinder).lower() == "rear" else 0.0

    risk = _clamp(c_advance + c_lean + c_cht + bias, 0.0, 1.0)
    return {
        "risk": risk,
        "reference_spark_deg": round(ref, 2),
        "advance_over_reference_deg": round(excess_deg, 2),
        "load_scale": round(load, 3),
        "contrib_advance": round(c_advance, 4),
        "contrib_lean": round(c_lean, 4),
        "contrib_cht": round(c_cht, 4),
        "contrib_rear_bias": round(bias, 4),
        "cylinder": cylinder,
    }


# --------------------------------------------------------------------------
# Conditions + gearing
# --------------------------------------------------------------------------

def merge_conditions(conditions=None):
    """Fill a partial conditions dict from dyno_baseline.json defaults.

    Keys: ambient_f, iat_f (may be None), iat_rise_f, baro_inhg, cht_f.
    """
    base = dict(load_baseline()["conditions_default"])
    out = {
        "ambient_f": float(base["ambient_f"]),
        "iat_f": base["iat_f"],
        "iat_rise_f": float(base["iat_rise_f"]),
        "baro_inhg": float(base["baro_inhg"]),
        "cht_f": float(base["cht_f"]),
    }
    if conditions:
        for k in out:
            if k in conditions and conditions[k] is not None:
                out[k] = float(conditions[k])
        if "iat_f" in conditions and conditions["iat_f"] is None:
            out["iat_f"] = None
    return out


def overall_ratio(gear):
    """Crank revs per rear-wheel rev in `gear` (primary * trans * final)."""
    g = load_baseline()["gearing"]
    ratios = g["transmission_ratios"]
    key = str(int(gear))
    if key not in ratios:
        raise ValueError(f"gear {gear} not in the Cruise Drive 6-speed "
                         f"(have {sorted(ratios)})")
    return (float(g["primary_ratio"]) * float(ratios[key])
            * float(g["final_drive_ratio"]))


def mph_from_rpm(rpm, gear):
    """Road speed in mph for a given crank rpm and gear.

        wheel_rev/s = rpm / 60 / overall_ratio
        m/s         = wheel_rev/s * rolling_circumference_m
    """
    g = load_baseline()["gearing"]
    circ = float(g["tire_rolling_circumference_m"])
    mps = float(rpm) / 60.0 / overall_ratio(gear) * circ
    return mps * MPS_TO_MPH


def _rpm_per_mps(gear):
    g = load_baseline()["gearing"]
    circ = float(g["tire_rolling_circumference_m"])
    return 60.0 * overall_ratio(gear) / circ


# --------------------------------------------------------------------------
# Change vocabulary — matches guardrails.py
# --------------------------------------------------------------------------

def _band(change, prefix):
    """Read an rpm/tps band from a change dict.

    Accepts BOTH spellings so nothing upstream has to translate:
      * guardrails style : {"rpm_band": [lo, hi], "tps_band": [lo, hi]}
      * flat style       : {"rpm_min":lo, "rpm_max":hi, "tps_min":.., "tps_max":..}
    Missing band means "applies everywhere" for that axis.
    """
    b = change.get(f"{prefix}_band")
    if b:
        return float(b[0]), float(b[1])
    lo = change.get(f"{prefix}_min")
    hi = change.get(f"{prefix}_max")
    if lo is None and hi is None:
        return (-math.inf, math.inf)
    return (float(lo) if lo is not None else -math.inf,
            float(hi) if hi is not None else math.inf)


def _signed(change):
    """Signed magnitude, exactly as guardrails._signed does it."""
    mag = abs(float(change.get("magnitude", 0) or 0))
    return -mag if change.get("direction") == "decrease" else mag


def _unit_of(change):
    """Unit of a change: 've_pct' | 'deg' | 'afr' | None.

    Explicit `unit` wins; otherwise it is inferred from the table name using
    the guardrails table sets, so the two modules can never disagree about
    what "ve_front" means.
    """
    u = (change.get("unit") or "").strip().lower()
    if u in ("ve_pct", "deg", "afr"):
        return u
    table = change.get("table", "")
    if table in G.SPARK_TABLES:
        return "deg"
    if table in G.VE_TABLES:
        return "ve_pct"
    if table in G.AFR_TABLES:
        return "afr"
    return None


def _cylinders_of(change):
    """Which cylinders a change touches: ('front','rear') / ('front',) / ('rear',)."""
    cyl = (change.get("cylinder") or "both").strip().lower()
    table = change.get("table", "")
    if table.endswith("_front"):
        return ("front",)
    if table.endswith("_rear") or table == "rear_timing_offset":
        return ("rear",)
    if cyl == "front":
        return ("front",)
    if cyl == "rear":
        return ("rear",)
    return ("front", "rear")


def _applies(change, rpm, tps_pct, widen=False):
    """Does this change touch the (rpm, tps) operating point?

    With widen=True each band grows by one grid cell in every direction — the
    deterministic worst-case band-edge envelope (a changed cell blends into the
    unchanged cell next to it).
    """
    r_lo, r_hi = _band(change, "rpm")
    t_lo, t_hi = _band(change, "tps")
    if widen:
        be = load_baseline()["band_edge"]
        dr = float(be["rpm_cell"])
        dt = float(be["tps_cell"])
        r_lo, r_hi = r_lo - dr, r_hi + dr
        t_lo, t_hi = t_lo - dt, t_hi + dt
    return (r_lo <= rpm <= r_hi) and (t_lo <= tps_pct <= t_hi)


def _resolve_changes(changes, rpm, tps_pct, widen=False):
    """Fold the active changes at one operating point into per-cylinder deltas.

    Returns {"front": {...}, "rear": {...}} with keys dve_pct, dafr, ddeg.
    """
    out = {c: {"dve_pct": 0.0, "dafr": 0.0, "ddeg": 0.0} for c in ("front", "rear")}
    for ch in changes or []:
        if not _applies(ch, rpm, tps_pct, widen=widen):
            continue
        unit = _unit_of(ch)
        if unit is None:
            continue
        delta = _signed(ch)
        key = {"ve_pct": "dve_pct", "afr": "dafr", "deg": "ddeg"}[unit]
        for cyl in _cylinders_of(ch):
            out[cyl][key] += delta
    return out


# --------------------------------------------------------------------------
# Simulation
# --------------------------------------------------------------------------

def _frame(rpm, tps_pct, gear, cond, changes, widen=False):
    """One deterministic operating point. Returns (frame_dict, extras)."""
    cal = load_baseline()
    afr_cal = cal["afr"]

    base_torque = baseline_torque(rpm)
    ve_base = baseline_ve(rpm)
    spark_base = reference_spark(rpm, tps_pct)
    afr_target_base = float(afr_cal["wot_target"])
    mbt = mbt_deg(tps_pct)

    deltas = _resolve_changes(changes, rpm, tps_pct, widen=widen)

    # Baseline reference factors. The published anchors already contain the
    # baseline's own AFR/timing state, so every factor is applied as a RATIO
    # against these — that is what makes "no changes" an exact identity.
    afr_base_front = afr_target_base
    afr_base_rear = afr_target_base - G.REAR_RICHER_AFR
    f_afr_base = 0.5 * (afr_power_factor(afr_base_front)
                        + afr_power_factor(afr_base_rear))
    f_tim_base = timing_power_factor(spark_base - mbt, tps_pct)

    per_cyl = {}
    for cyl in ("front", "rear"):
        d = deltas[cyl]
        # AFR target moves only if an afr_target table was edited.
        target = afr_target_base + d["dafr"]
        if cyl == "rear":
            target -= G.REAR_RICHER_AFR
        # A VE/fuel edit is an AFR SHIFT ONLY — never a torque multiplier.
        delivered = target + afr_shift(d["dve_pct"], target)
        spark = spark_base + d["ddeg"]
        per_cyl[cyl] = {
            "afr_target": target,
            "afr": delivered,
            "spark": spark,
            "dve_pct": d["dve_pct"],
        }

    # Rear timing must be equal-or-retarded vs front (guardrails house rule).
    if per_cyl["rear"]["spark"] > per_cyl["front"]["spark"]:
        per_cyl["rear"]["spark"] = per_cyl["front"]["spark"]

    f_afr = 0.5 * (afr_power_factor(per_cyl["front"]["afr"])
                   + afr_power_factor(per_cyl["rear"]["afr"]))
    f_tim = 0.5 * (timing_power_factor(per_cyl["front"]["spark"] - mbt, tps_pct)
                   + timing_power_factor(per_cyl["rear"]["spark"] - mbt, tps_pct))

    torque = base_torque * (f_afr / f_afr_base) * (f_tim / f_tim_base)
    hp = torque * rpm / HP_TORQUE_CONST

    cht = cond["cht_f"]
    # NOTE: duty is computed from the AIRFLOW VE (unchanged by a table edit —
    # a VE/fuel table commands fuel, not air) and the DELIVERED AFR. The fuel
    # edit therefore raises duty exactly once, through the AFR shift. Feeding a
    # fuel-scaled VE in here as well would square the effect.
    duty_front = injector_duty_pct(rpm, ve_base, per_cyl["front"]["afr"], cond)
    duty_rear = injector_duty_pct(rpm, ve_base, per_cyl["rear"]["afr"], cond)
    duty = max(duty_front, duty_rear)

    kb_front = knock_breakdown(rpm, tps_pct, per_cyl["front"]["spark"],
                               per_cyl["front"]["afr"], cht, "front")
    kb_rear = knock_breakdown(rpm, tps_pct, per_cyl["rear"]["spark"],
                              per_cyl["rear"]["afr"], cht, "rear")
    risk = max(kb_front["risk"], kb_rear["risk"])

    frame = {
        "rpm": round(rpm, 1),
        "mph": round(mph_from_rpm(rpm, gear), 1),
        "gear": int(gear),
        "torque": round(torque, 1),
        "hp": round(hp, 1),
        "afr_front": round(per_cyl["front"]["afr"], 2),
        "afr_rear": round(per_cyl["rear"]["afr"], 2),
        "afr_target": round(per_cyl["front"]["afr_target"], 2),
        "injector_duty_pct": round(duty, 1),
        "spark_deg": round(per_cyl["front"]["spark"], 1),
        "knock_risk": round(risk, 3),
        "cht_f": round(cht, 1),
    }
    extras = {
        "torque_exact": torque,
        "hp_exact": hp,
        "tps_pct": tps_pct,
        "spark_rear_deg": round(per_cyl["rear"]["spark"], 1),
        "duty_front": duty_front,
        "duty_rear": duty_rear,
        "knock_front": kb_front,
        "knock_rear": kb_rear,
        "mbt_deg": mbt,
    }
    return frame, extras


def _run(changes, cond, gear, widen=False):
    """Integrate a WOT pull. Returns (samples, extras_list)."""
    cal = load_baseline()
    pull = cal["pull"]
    veh = cal["vehicle"]
    g = cal["gearing"]

    dt = 1.0 / float(pull["sample_hz"])
    tps = float(pull["tps_pct"])
    rpm = float(pull["start_rpm"])
    redline = float(cal["baseline"]["redline_rpm"])
    max_t = float(pull["max_seconds"])

    mass = float(veh["mass_kg"])
    eff = float(veh["driveline_efficiency"])
    cda = float(veh["cda_m2"])
    crr = float(veh["rolling_resistance_coeff"])
    radius = float(g["tire_rolling_circumference_m"]) / (2.0 * math.pi)
    ratio = overall_ratio(gear)
    rpm_per_mps = _rpm_per_mps(gear)
    # Air density in kg/m^3 for the aero term (rho_air returns g/cm^3).
    rho_kg = rho_air(cond) * 1e3

    samples, extras = [], []
    t = 0.0
    while True:
        frame, ex = _frame(rpm, tps, gear, cond, changes, widen=widen)
        frame["t"] = round(t, 2)
        samples.append(frame)
        extras.append(ex)
        if rpm >= redline or t >= max_t:
            break

        # Longitudinal dynamics: rear-wheel thrust minus drag & rolling loss.
        wheel_nm = ex["torque_exact"] * NM_PER_LBFT * ratio * eff
        thrust_n = wheel_nm / radius
        v_mps = rpm / rpm_per_mps
        drag_n = 0.5 * rho_kg * cda * v_mps * v_mps
        roll_n = crr * mass * G_ACCEL
        accel = (thrust_n - drag_n - roll_n) / mass
        # A pull never decelerates in the model; if drag wins we are simply at
        # terminal velocity in this gear and the sweep stalls out at max_t.
        accel = max(accel, 0.0)
        rpm = min(redline, rpm + accel * rpm_per_mps * dt)
        t += dt
        if t > max_t:
            break

    # Reorder keys so `t` leads every frame (nicer for the UI/NDJSON).
    ordered = []
    for f in samples:
        ordered.append({
            "t": f["t"], "rpm": f["rpm"], "mph": f["mph"], "gear": f["gear"],
            "torque": f["torque"], "hp": f["hp"],
            "afr_front": f["afr_front"], "afr_rear": f["afr_rear"],
            "afr_target": f["afr_target"],
            "injector_duty_pct": f["injector_duty_pct"],
            "spark_deg": f["spark_deg"], "knock_risk": f["knock_risk"],
            "cht_f": f["cht_f"],
        })
    return ordered, extras


# --------------------------------------------------------------------------
# Issue detection — deterministic, modelled values only, ALWAYS severity "warn"
# --------------------------------------------------------------------------

def _issue(t, code, message, rpm, detail):
    """Every pull-derived finding is advisory. Hard-block authority belongs
    solely to guardrails.check_change() on the proposed steady-state values."""
    return {"t": round(float(t), 2), "severity": "warn", "code": code,
            "message": message, "rpm": int(round(float(rpm))), "detail": detail}


def _worst(samples, key, pick=max):
    if not samples:
        return None
    return pick(samples, key=lambda s: s[key])


def _detect(samples, extras):
    """Scan modelled frames for findings. One finding per code, reported at the
    worst frame, with the rpm span it covers in `detail`.

    NOTHING here reads a visualisation value — only modelled physics.
    """
    found = {}

    def note(code, cond_fn, worst_key, msg_fn, detail_fn, pick=max):
        hits = [(s, e) for s, e in zip(samples, extras) if cond_fn(s, e)]
        if not hits:
            return
        s_worst, e_worst = pick(hits, key=lambda p: p[0][worst_key])
        rpms = [s["rpm"] for s, _ in hits]
        found[code] = _issue(s_worst["t"], code, msg_fn(s_worst, e_worst),
                             s_worst["rpm"],
                             detail_fn(s_worst, e_worst)
                             + f" Seen from {min(rpms):.0f} to {max(rpms):.0f} rpm.")

    lean_hard = G.AFR_WOT_HARD[1]
    rich_hard = G.AFR_WOT_HARD[0]
    wot_lo, wot_hi = G.AFR_WOT

    note("afr_lean_of_hard_limit",
         lambda s, e: e["tps_pct"] >= 80 and max(s["afr_front"], s["afr_rear"]) > lean_hard,
         "afr_front",
         lambda s, e: (f"Lean of the hard WOT limit: modelled {s['afr_front']:.2f} AFR "
                       f"front / {s['afr_rear']:.2f} rear at {s['rpm']:.0f} rpm "
                       f"(never leaner than {lean_hard} under load)."),
         lambda s, e: "Lean under load raises detonation risk and head temperature.")

    note("afr_rich_of_hard_limit",
         lambda s, e: e["tps_pct"] >= 80 and min(s["afr_front"], s["afr_rear"]) < rich_hard,
         "afr_front",
         lambda s, e: (f"Rich of the hard WOT limit: modelled {s['afr_front']:.2f} AFR "
                       f"front / {s['afr_rear']:.2f} rear at {s['rpm']:.0f} rpm "
                       f"(never richer than {rich_hard} under load)."),
         lambda s, e: "Over-rich under load washes bores, fouls plugs and costs torque.",
         pick=min)

    note("afr_outside_wot_window",
         lambda s, e: (e["tps_pct"] >= 80
                       and not (wot_lo <= s["afr_front"] <= wot_hi)
                       and rich_hard <= s["afr_front"] <= lean_hard),
         "afr_front",
         lambda s, e: (f"Front AFR {s['afr_front']:.2f} at {s['rpm']:.0f} rpm sits "
                       f"outside the house WOT window {wot_lo}-{wot_hi} "
                       f"(still inside the hard limits)."),
         lambda s, e: "Advisory only — inside the hard window, outside the validated one.")

    note("injector_duty_red",
         lambda s, e: s["injector_duty_pct"] >= G.INJECTOR_DUTY_RED_PCT,
         "injector_duty_pct",
         lambda s, e: (f"Injector duty {s['injector_duty_pct']:.0f}% at "
                       f"{s['rpm']:.0f} rpm is at/over the "
                       f"{G.INJECTOR_DUTY_RED_PCT:.0f}% red line."),
         lambda s, e: ("Injectors are running out of time. Modelled with "
                       f"{load_baseline()['injectors']['flow_gps']} g/s injectors "
                       "(needs_confirmation)."))

    if "injector_duty_red" not in found:
        note("injector_duty_amber",
             lambda s, e: s["injector_duty_pct"] >= G.INJECTOR_DUTY_AMBER_PCT,
             "injector_duty_pct",
             lambda s, e: (f"Injector duty {s['injector_duty_pct']:.0f}% at "
                           f"{s['rpm']:.0f} rpm is past the "
                           f"{G.INJECTOR_DUTY_AMBER_PCT:.0f}% comfort line."),
             lambda s, e: "Headroom is thin — any further fuel adds will run out of injector.")

    note("knock_risk_high",
         lambda s, e: s["knock_risk"] >= KNOCK_HIGH,
         "knock_risk",
         lambda s, e: (f"Knock-risk index {s['knock_risk']:.2f} at {s['rpm']:.0f} rpm "
                       f"(high). Front spark {s['spark_deg']:.1f} deg vs backbone "
                       f"reference {e['knock_front']['reference_spark_deg']:.1f} deg."),
         lambda s, e: ("Breakdown (front): advance "
                       f"{e['knock_front']['contrib_advance']:.2f}, lean "
                       f"{e['knock_front']['contrib_lean']:.2f}, CHT "
                       f"{e['knock_front']['contrib_cht']:.2f}."))

    if "knock_risk_high" not in found:
        note("knock_risk_elevated",
             lambda s, e: s["knock_risk"] >= KNOCK_WARN,
             "knock_risk",
             lambda s, e: (f"Knock-risk index {s['knock_risk']:.2f} at {s['rpm']:.0f} rpm "
                           f"(elevated). Front spark {s['spark_deg']:.1f} deg vs backbone "
                           f"reference {e['knock_front']['reference_spark_deg']:.1f} deg."),
             lambda s, e: ("Breakdown (front): advance "
                           f"{e['knock_front']['contrib_advance']:.2f}, lean "
                           f"{e['knock_front']['contrib_lean']:.2f}, CHT "
                           f"{e['knock_front']['contrib_cht']:.2f}."))

    note("spark_over_ceiling",
         lambda s, e: max(s["spark_deg"], e["spark_rear_deg"]) > G.SPARK_CEILING,
         "spark_deg",
         lambda s, e: (f"Modelled spark {s['spark_deg']:.1f} deg at {s['rpm']:.0f} rpm "
                       f"is over the {G.SPARK_CEILING:.0f} deg hard ceiling."),
         lambda s, e: "guardrails.check_change() will hard-block this on the real values.")

    # Only fires when the *change* pushed spark above the validated backbone
    # reference AND past model-MBT. A baseline pull sits exactly on the
    # backbone, so it never trips this.
    note("past_mbt_no_gain",
         lambda s, e: (e["tps_pct"] >= KNOCK_LIMITED_TPS
                       and s["spark_deg"] - e["mbt_deg"] > 1.0
                       and e["knock_front"]["advance_over_reference_deg"] > 0.25),
         "knock_risk",
         lambda s, e: (f"At {s['rpm']:.0f} rpm the modelled spark {s['spark_deg']:.1f} deg "
                       f"is {s['spark_deg'] - e['mbt_deg']:.1f} deg PAST model-MBT "
                       f"({e['mbt_deg']:.1f} deg) — no torque left to gain, only knock risk."),
         lambda s, e: "Knock-limited region: extra advance here buys detonation, not power.")

    return found


def _precondition_issues(cond):
    """t=0 findings about the STATIC conditions the pull starts from."""
    out = []
    cht = cond["cht_f"]
    if cht > G.AUTOTUNE_DISABLE_F:
        out.append(_issue(0.0, "cht_precondition_autotune_disable",
                          f"Starting CHT {cht:.0f} degF is above the "
                          f"{G.AUTOTUNE_DISABLE_F} degF AutoTune disable gate.",
                          0, "Heat-soaked trims are garbage — do not learn or "
                             "validate on this. Let it cool first."))
    elif cht > G.HEAT_RETARD_CHT_F:
        out.append(_issue(0.0, "cht_precondition_heat_retard",
                          f"Starting CHT {cht:.0f} degF is already past the "
                          f"{G.HEAT_RETARD_CHT_F} degF heat-retard knee.",
                          0, "The ECM will be pulling timing before the pull even "
                             "starts, so this pull under-reports what the tune does cold."))
    elif cht < G.AUTOTUNE_ENABLE_F:
        out.append(_issue(0.0, "cht_precondition_autotune_cold",
                          f"Starting CHT {cht:.0f} degF is below the "
                          f"{G.AUTOTUNE_ENABLE_F} degF AutoTune enable gate.",
                          0, "AutoTune will not learn here; a validation ride must be "
                             "up to temperature before its trims mean anything."))
    return out


# --------------------------------------------------------------------------
# Summary
# --------------------------------------------------------------------------

def _delta_range(delta, unit_label, uncertainty_pct):
    """Format a delta as an INTEGER RANGE string. Never sub-integer.

    The band is +/- uncertainty_pct of the delta itself, then widened outward
    to whole numbers (floor the low end, ceil the high end). e.g. +4.0 hp at
    15% -> 3.4 .. 4.6 -> "~ +3 to +5 hp".
    Returns (string, lo_int, hi_int).
    """
    u = float(uncertainty_pct) / 100.0
    if delta >= 0:
        lo_f, hi_f = delta * (1.0 - u), delta * (1.0 + u)
    else:
        lo_f, hi_f = delta * (1.0 + u), delta * (1.0 - u)
    lo, hi = int(math.floor(lo_f)), int(math.ceil(hi_f))
    if lo == 0 and hi == 0:
        return f"≈ 0 {unit_label} (no change)", 0, 0
    if lo == hi:
        return f"≈ {lo:+d} {unit_label}", lo, hi
    return f"≈ {lo:+d} to {hi:+d} {unit_label}", lo, hi


def _collect_needs_confirmation(cal):
    """Every calibration block the JSON flags as unconfirmed, for the UI."""
    flagged = []
    if cal["engine"].get("ve_peak_needs_confirmation"):
        flagged.append("engine.ve_peak (estimated peak volumetric efficiency)")
    if cal["injectors"].get("needs_confirmation"):
        flagged.append(f"injectors.flow_gps = {cal['injectors']['flow_gps']} g/s "
                       "(from the shop's tune notes, not measured)")
    if cal["gearing"].get("needs_confirmation"):
        flagged.append("gearing (" + "; ".join(cal["gearing"]["_confirm_these"]) + ")")
    if cal["baseline"].get("torque_curve_needs_confirmation"):
        flagged.append("baseline.torque_curve (drawn through published kit anchors, "
                       "no dyno sheet for this bike)")
    if cal["baseline"]["anchor_peak_torque"].get("needs_confirmation"):
        flagged.append("baseline.anchor_peak_torque / anchor_peak_hp (published kit chart)")
    if cal["vehicle"].get("mass_needs_confirmation"):
        flagged.append("vehicle.mass_kg (affects only how fast the gauges sweep)")
    if cal["band_edge"].get("needs_confirmation"):
        flagged.append("band_edge.rpm_cell / tps_cell (TMax grid step, inferred)")
    return flagged


# --------------------------------------------------------------------------
# Public entry point
# --------------------------------------------------------------------------

def simulate_pull(changes, conditions=None, gear=5):
    """Simulate a WOT pull with `changes` applied and compare it to baseline.

    changes    : list of change dicts using the guardrails vocabulary
                 {table, cylinder, rpm_band|rpm_min/rpm_max,
                  tps_band|tps_min/tps_max, direction, magnitude, unit}.
                 Units: "ve_pct" (VE/fuel percent), "afr" (AFR target points),
                 "deg" (spark degrees). Unit is inferred from `table` when
                 omitted. An empty/None list is the baseline pull.
    conditions : partial dict merged over dyno_baseline.conditions_default
                 (ambient_f, iat_f, baro_inhg, cht_f).
    gear       : Cruise Drive gear 1-6. Default 5.

    Returns {"samples": [...], "issues": [...], "summary": {...},
             "baseline_status": {...}} — see the module README in API.md.
    """
    cal = load_baseline()
    cond = merge_conditions(conditions)
    gear = int(gear)
    changes = list(changes or [])
    unc = float(cal["uncertainty_pct"])

    base_samples, base_extras = _run([], cond, gear)
    samples, extras = _run(changes, cond, gear)

    # --- issues -----------------------------------------------------------
    issues = list(_precondition_issues(cond))
    nominal = _detect(samples, extras)
    issues.extend(nominal[k] for k in sorted(nominal))

    # Deterministic worst-case band-edge envelope: re-run with every band
    # widened by one grid cell. Anything NEW is a band-edge finding.
    if changes:
        env_samples, env_extras = _run(changes, cond, gear, widen=True)
        envelope = _detect(env_samples, env_extras)
        for code in sorted(envelope):
            if code in nominal:
                continue
            it = dict(envelope[code])
            it["message"] = it["message"] + " (worst-case band edge)"
            it["detail"] = (
                "worst-case band edge — this does NOT appear inside the band as "
                "typed. It shows up only when the changed cells blend into the "
                "unchanged cells next door (one grid cell = "
                f"{cal['band_edge']['rpm_cell']} rpm / "
                f"{cal['band_edge']['tps_cell']:.0f}% TPS). " + it["detail"])
            issues.append(it)

    issues.sort(key=lambda i: (i["t"], i["code"]))

    # --- summary ----------------------------------------------------------
    pk_hp = _worst(samples, "hp")
    pk_tq = _worst(samples, "torque")
    b_pk_hp = _worst(base_samples, "hp")
    b_pk_tq = _worst(base_samples, "torque")

    d_hp = pk_hp["hp"] - b_pk_hp["hp"]
    d_tq = pk_tq["torque"] - b_pk_tq["torque"]
    s_hp, hp_lo, hp_hi = _delta_range(d_hp, "hp", unc)
    s_tq, tq_lo, tq_hi = _delta_range(d_tq, "lb-ft", unc)

    peak_duty = max(s["injector_duty_pct"] for s in samples)
    max_knock = max(s["knock_risk"] for s in samples)
    base_peak_duty = max(s["injector_duty_pct"] for s in base_samples)
    base_max_knock = max(s["knock_risk"] for s in base_samples)

    summary = {
        "banner": cal["banner"],
        "calibration_status": cal["calibration_status"],
        "uncertainty_pct": int(unc),
        "gear": gear,
        "pull_seconds": samples[-1]["t"],
        "sample_hz": int(cal["pull"]["sample_hz"]),
        "rpm_range": [int(round(samples[0]["rpm"])), int(round(samples[-1]["rpm"]))],
        "peak_hp": pk_hp["hp"],
        "peak_hp_rpm": int(round(pk_hp["rpm"])),
        "peak_torque": pk_tq["torque"],
        "peak_torque_rpm": int(round(pk_tq["rpm"])),
        "baseline_peak_hp": b_pk_hp["hp"],
        "baseline_peak_torque": b_pk_tq["torque"],
        # Deltas are INTEGER RANGES ONLY. The model is not accurate enough to
        # justify a decimal point, so it is not allowed to print one.
        "delta_hp": s_hp,
        "delta_hp_range": [hp_lo, hp_hi],
        "delta_torque": s_tq,
        "delta_torque_range": [tq_lo, tq_hi],
        "peak_injector_duty_pct": round(peak_duty, 1),
        "baseline_peak_injector_duty_pct": round(base_peak_duty, 1),
        "injector_duty_amber_pct": G.INJECTOR_DUTY_AMBER_PCT,
        "injector_duty_red_pct": G.INJECTOR_DUTY_RED_PCT,
        "max_knock_risk": round(max_knock, 3),
        "baseline_max_knock_risk": round(base_max_knock, 3),
        "issue_count": len(issues),
        "severity_note": ("Every pull finding is advisory (severity 'warn'). "
                          "Only guardrails.check_change() can block a proposal."),
        "conditions": dict(cond),
        "cht_note": ("CHT is a STATIC INPUT ECHO, not a simulated gauge — it "
                     "cannot be modelled meaningfully over a 15 s pull, so the "
                     "starting value is repeated in every frame."),
    }

    baseline_status = {
        "source": str(BASELINE_PATH),
        "schema_version": cal["schema_version"],
        "label": cal["label"],
        "setup_key": cal["setup_key"],
        "calibration_status": cal["calibration_status"],
        "uncertainty_pct": int(unc),
        "banner": cal["banner"],
        "injectors": dict(cal["injectors"]),
        "anchors": {
            "peak_torque": cal["baseline"]["anchor_peak_torque"],
            "peak_hp": cal["baseline"]["anchor_peak_hp"],
        },
        "gear_ratio_overall": round(overall_ratio(gear), 4),
        "timing_backbone_source": "guardrails.TIMING_BACKBONE (single source)",
        "belly_derate_deg": G.BELLY_DERATE_DEG,
        "needs_confirmation": _collect_needs_confirmation(cal),
        "deterministic": True,
        "llm_involved": False,
    }

    return {"samples": samples, "issues": issues, "summary": summary,
            "baseline_status": baseline_status}


# --------------------------------------------------------------------------
# CLI (handy for eyeballing a pull without the web UI)
# --------------------------------------------------------------------------

def _main(argv):
    import argparse
    ap = argparse.ArgumentParser(description="Deterministic virtual dyno (no LLM).")
    ap.add_argument("--gear", type=int, default=5)
    ap.add_argument("--changes", default="[]",
                    help="JSON list of change dicts (guardrails vocabulary)")
    ap.add_argument("--conditions", default="{}", help="JSON conditions dict")
    ap.add_argument("--json", action="store_true", help="dump the full payload")
    a = ap.parse_args(argv)

    res = simulate_pull(json.loads(a.changes), json.loads(a.conditions), a.gear)
    if a.json:
        print(json.dumps(res, indent=2))
        return 0
    s = res["summary"]
    print(s["banner"])
    print(s["calibration_status"])
    print(f"\ngear {s['gear']} · {s['pull_seconds']}s · "
          f"{s['rpm_range'][0]:.0f}-{s['rpm_range'][1]:.0f} rpm")
    print(f"peak {s['peak_hp']} hp @ {s['peak_hp_rpm']:.0f} · "
          f"{s['peak_torque']} lb-ft @ {s['peak_torque_rpm']:.0f}")
    print(f"delta: {s['delta_hp']} / {s['delta_torque']}")
    print(f"peak injector duty {s['peak_injector_duty_pct']}% · "
          f"max knock index {s['max_knock_risk']}")
    for i in res["issues"]:
        print(f"  [{i['severity']}] t={i['t']}s {i['code']}: {i['message']}")
    return 0


if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    raise SystemExit(_main(sys.argv[1:]))
