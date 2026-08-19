#!/usr/bin/env python3
"""Numeric tuning guardrails for the 2023 Low Rider ST 131ci — the single
source of every safety limit in the web UI, vetting pipeline, and virtual dyno.

Every constant is source-commented to the shop's own validated material
(tune.py SAFE block in ~/hermes-rag, tune_assistant.SYSTEM_PROMPT house rules,
COLLABORATION.md, or a named corpus doc). The LLM never gets the final word on
safety: only check_change()/check_proposal() findings of severity "block" can
stop a proposal, and they are pure deterministic code — no model, no network.

Stdlib only. Import-safe from a long-running server (no I/O at import).
"""

# --- AFR windows (source: hermes-rag/tune.py SAFE block, validated shop history)
AFR_WOT = (12.4, 12.8)          # target window at wide-open throttle
AFR_WOT_HARD = (12.2, 13.2)     # never leaner than 13.2 under load, never richer than 12.2
AFR_CRUISE = (13.8, 14.6)
AFR_IDLE = (13.8, 14.2)
REAR_RICHER_AFR = 0.2           # rear cylinder ≈0.2 richer (runs hotter)

# --- Spark advance (source: tune.py SAFE block)
SPARK_CRUISE = (28.0, 32.0)     # degrees
SPARK_WOT = (26.0, 30.0)
SPARK_CEILING = 32.0            # hard ceiling
SPARK_MAX_STEP = 2.0            # max change per step, then data-log to verify
# Rear timing must be equal-or-retarded vs front (tune.py SAFE block).

# --- VE / fuel steps
# Source: COLLABORATION.md decel-pop protocol (+2% VE) is the only VALIDATED
# house VE step -> warn above ±2% per band, mirroring SAFE's spark
# "±2° per step then data-log" philosophy.
VE_STEP_WARN_PCT = 2.0
# PROVISIONAL hard-block: the corpus's own ±5% trim / "smooth VE if trims
# exceed" criterion (thundermax_nas_master_log_v1.md). Confirm with Joshua.
VE_STEP_BLOCK_PCT = 5.0

# --- Temperature gates (°F)
AUTOTUNE_ENABLE_F = 200         # AutoTune learning: enable above (SYSTEM_PROMPT)
AUTOTUNE_DISABLE_F = 280        # disable above — heat-soak trims are garbage
HEAT_RETARD_CHT_F = 226         # progressive timing retard past 226°F to
                                # prevent ping during heat soak (SYSTEM_PROMPT)

# --- Injector duty (virtual dyno gauge + vet thresholds)
INJECTOR_DUTY_AMBER_PCT = 80.0  # out of comfortable headroom
INJECTOR_DUTY_RED_PCT = 90.0

# --- House decel-pop protocol (source: SYSTEM_PROMPT / COLLABORATION.md)
# Pops above 4k rpm -> +2% VE at 0-2% TPS, 3840-4608 rpm.
DECEL_POP_HIGH = {"ve_pct": +2.0, "tps": (0, 2), "rpm": (3840, 4608)}
# Broad-range pops -> -1° spark at 0-2% TPS, 2048-2816 rpm.
DECEL_POP_BROAD = {"spark_deg": -1.0, "tps": (0, 2), "rpm": (2048, 2816)}

# --- Timing backbone: validated rpm -> degrees advance reference curve
# (source: docs/corpus/thundermax_nas_thundermax_131st_timing_backbone_verification.md).
# Used by the virtual dyno's knock model as the per-rpm advance reference.
TIMING_BACKBONE = [
    (900, 2.0),    # idle 0-3°
    (1500, 8.0),
    (1800, 11.0),
    (2500, 17.0),
    (3300, 21.0),
    (4000, 30.0),
    (5200, 34.5),  # 34-35° plateau 5200+
]
# Corpus "belly" de-rate: knock-prone midrange cells run 3-4° below backbone
# (union of corpus belly bands: ~1800-3500 rpm at 20-70% TPS).
BELLY_DERATE_DEG = 3.5
BELLY_RPM = (1800, 3500)
BELLY_TPS = (20, 70)

# Tables a proposal may target, in TMax Tuner page language (parser
# CAPTURE_CHECKLIST) — proposals speak the UI Joshua applies changes in.
TABLES = (
    "afr_target", "ve_front", "ve_rear", "fuel_flow_front", "fuel_flow_rear",
    "spark_advance_front", "spark_advance_rear", "rear_timing_offset",
    "decel_fuel_cut", "autotune_zones", "idle_rpm",
)
SPARK_TABLES = {"spark_advance_front", "spark_advance_rear", "rear_timing_offset"}
VE_TABLES = {"ve_front", "ve_rear", "fuel_flow_front", "fuel_flow_rear"}
AFR_TABLES = {"afr_target"}


def as_dict():
    """Everything the safety card / vet report / dyno gauges need, one payload —
    served by /api/profile so UI and vetting can never disagree."""
    return {
        "afr": {"wot": AFR_WOT, "wot_hard": AFR_WOT_HARD, "cruise": AFR_CRUISE,
                "idle": AFR_IDLE, "rear_richer": REAR_RICHER_AFR},
        "spark": {"cruise": SPARK_CRUISE, "wot": SPARK_WOT,
                  "ceiling": SPARK_CEILING, "max_step": SPARK_MAX_STEP},
        "ve_step": {"warn_pct": VE_STEP_WARN_PCT, "block_pct": VE_STEP_BLOCK_PCT,
                    "block_provisional": True},
        "temps_f": {"autotune_enable": AUTOTUNE_ENABLE_F,
                    "autotune_disable": AUTOTUNE_DISABLE_F,
                    "heat_retard": HEAT_RETARD_CHT_F},
        "injector_duty": {"amber_pct": INJECTOR_DUTY_AMBER_PCT,
                          "red_pct": INJECTOR_DUTY_RED_PCT},
        "decel_pop": {"high": DECEL_POP_HIGH, "broad": DECEL_POP_BROAD},
        "timing_backbone": TIMING_BACKBONE,
        "belly": {"derate_deg": BELLY_DERATE_DEG, "rpm": BELLY_RPM, "tps": BELLY_TPS},
        "tables": TABLES,
        "never_write_tbw": True,
    }


# ----------------------------------------------------------------------------
# Checks. A "change" is one entry of a proposal's changes array:
#   {table, cylinder: both|front|rear, rpm_band: [lo,hi], tps_band: [lo,hi],
#    direction: increase|decrease, magnitude: float, unit: deg|ve_pct|afr,
#    target_value: float|null, current_value: float|null, claim: str}
# Findings: {rule, severity: block|warn, message}
# ----------------------------------------------------------------------------

def _f(rule, severity, message):
    return {"rule": rule, "severity": severity, "message": message}


def _signed(change):
    mag = abs(float(change.get("magnitude", 0)))
    return -mag if change.get("direction") == "decrease" else mag


def _is_wot(change):
    tps = change.get("tps_band") or (0, 0)
    return tps[1] >= 80


def _is_idle(change):
    rpm = change.get("rpm_band") or (0, 0)
    return rpm[1] <= 1280


def check_change(change):
    """Deterministic per-change findings. Absolute-window checks need a known
    target/current value; when it is unknowable they WARN (and the caller
    counts it toward checks_unverifiable) instead of silently passing."""
    findings = []
    table = change.get("table", "")
    unit = change.get("unit", "")
    delta = _signed(change)
    target = change.get("target_value")

    if table not in TABLES:
        findings.append(_f("table", "block",
                           f"unknown table '{table}' — not a TMax Tuner page"))
        return findings

    if table in SPARK_TABLES or unit == "deg":
        if abs(delta) > SPARK_MAX_STEP:
            findings.append(_f("spark_step", "block",
                               f"spark change {delta:+.1f}° exceeds ±{SPARK_MAX_STEP:.0f}°/step "
                               "house limit — step, then data-log to verify"))
        if target is not None:
            if target > SPARK_CEILING:
                findings.append(_f("spark_ceiling", "block",
                                   f"target {target:.1f}° exceeds the {SPARK_CEILING:.0f}° hard ceiling"))
        elif delta > 0:
            findings.append(_f("spark_absolute", "warn",
                               "advance increase with unknown resulting value — verify the "
                               f"cells stay under {SPARK_CEILING:.0f}° in TMax Tuner before applying"))
        if table == "rear_timing_offset" and delta > 0:
            findings.append(_f("rear_timing", "block",
                               "rear timing must be equal-or-retarded vs front (rear runs hotter)"))

    if table in VE_TABLES or unit == "ve_pct":
        if abs(delta) > VE_STEP_BLOCK_PCT:
            findings.append(_f("ve_step", "block",
                               f"VE/fuel change {delta:+.1f}% exceeds ±{VE_STEP_BLOCK_PCT:.0f}% "
                               "hard limit (provisional — corpus trim-smoothing criterion)"))
        elif abs(delta) > VE_STEP_WARN_PCT:
            findings.append(_f("ve_step", "warn",
                               f"VE/fuel change {delta:+.1f}% exceeds the validated ±{VE_STEP_WARN_PCT:.0f}%/step "
                               "house step — apply in stages with a validation ride between"))

    if table in AFR_TABLES or unit == "afr":
        lo, hi = (AFR_WOT_HARD if _is_wot(change)
                  else AFR_IDLE if _is_idle(change) else AFR_CRUISE)
        band = "WOT" if _is_wot(change) else ("idle" if _is_idle(change) else "cruise")
        if target is not None:
            if not (lo <= target <= hi):
                findings.append(_f("afr_window", "block",
                                   f"AFR target {target:.1f} outside the {band} window {lo}-{hi}"))
        else:
            findings.append(_f("afr_absolute", "warn",
                               f"AFR change with unknown resulting target — confirm cells stay "
                               f"inside {lo}-{hi} ({band}) in TMax Tuner"))

    return findings


def check_proposal(changes, overlapping_net=None):
    """Vet a whole proposal. `overlapping_net` (optional) is the net signed
    spark/VE delta per unit including still-active earlier proposals that
    overlap the same table+bands — the cross-proposal stacking guard: laddering
    +2° steps past SAFE's log-then-verify sequencing gets blocked here.
    Returns {findings, blocks, warns, checks_unverifiable, passed}."""
    findings = []
    for i, ch in enumerate(changes):
        for f in check_change(ch):
            findings.append({**f, "change_idx": i})
    if overlapping_net:
        net_spark = overlapping_net.get("deg", 0.0)
        if abs(net_spark) > SPARK_MAX_STEP:
            findings.append(_f("spark_stacking", "block",
                               f"net spark delta {net_spark:+.1f}° across still-active proposals "
                               f"exceeds ±{SPARK_MAX_STEP:.0f}° — validate the earlier step first"))
        net_ve = overlapping_net.get("ve_pct", 0.0)
        if abs(net_ve) > VE_STEP_BLOCK_PCT:
            findings.append(_f("ve_stacking", "block",
                               f"net VE delta {net_ve:+.1f}% across still-active proposals "
                               f"exceeds ±{VE_STEP_BLOCK_PCT:.0f}%"))
        elif abs(net_ve) > VE_STEP_WARN_PCT:
            findings.append(_f("ve_stacking", "warn",
                               f"net VE delta {net_ve:+.1f}% across still-active proposals — "
                               "stage the changes with validation rides between"))
    blocks = sum(1 for f in findings if f["severity"] == "block")
    unverifiable = sum(1 for f in findings if f["rule"].endswith("_absolute"))
    return {"findings": findings, "blocks": blocks,
            "warns": len(findings) - blocks,
            "checks_unverifiable": unverifiable, "passed": blocks == 0}
