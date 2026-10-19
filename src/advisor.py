#!/usr/bin/env python3
"""Symptom -> corrective change. The part that knows what to DO about a tune.

Everything else in this project describes: it parses a tune, classifies a
diff, models a pull, argues about a proposal. None of it answers the question
Joshua actually has when he gets off the bike, which is "it pops on decel and
runs hot, what do I change?"

This does. It maps a described symptom to a concrete change in TMax Tuner's
own page language -- table, direction, magnitude, rpm/TPS band -- along with
what to log to confirm it, and what would prove the recommendation wrong.

THREE RULES THIS MODULE OBEYS
-----------------------------
1. EVERY recommendation is run through `guardrails.check_change()` before it
   can be returned, and anything that draws a `block` is dropped, not
   softened. The advisor is not allowed to be the thing that talks the safety
   layer into a bad change -- if the house limits will not permit the fix, the
   honest output is "no legal change", plus the reason.

2. PROVENANCE IS NOT DECORATION. The decel-pop protocol is the only remedy in
   this file that comes from validated shop history on THIS bike. Everything
   else is inference from the manuals, the timing backbone, and physics.
   Those are marked `inferred` and say so out loud, because a rider deserves
   to know which advice has been ridden and which has not.

3. NOTHING HERE IS A MEASUREMENT. There is no datalog in this project (see
   docs/corpus/thundermax_reference_2026-08-26_no-ride-data-open-loop.md), so
   every remedy is a HYPOTHESIS to be tested by the validation-ride protocol,
   never a verdict. Each one ships with the log fields that would confirm or
   refute it.

Deliberately not an LLM. A rider on the side of the road with a hot engine
should get the same answer every time, and get it with Ollama down.
"""
import argparse
import json
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import guardrails as g  # noqa: E402

VALIDATED = "validated"    # ridden and confirmed on this bike
INFERRED = "inferred"      # sound, but not yet ridden here


def _chg(table, unit, magnitude, direction, rpm=None, tps=None, why=""):
    return {"table": table, "unit": unit, "magnitude": abs(magnitude),
            "direction": direction, "rpm_band": rpm, "tps_band": tps,
            "why": why}


def _ve(pct, rpm, tps, why, table="ve_front"):
    return _chg(table, "ve_pct", pct, "increase" if pct > 0 else "decrease",
                rpm, tps, why)


def _spark(deg, rpm, tps, why, table="spark_advance_front"):
    return _chg(table, "deg", deg, "increase" if deg > 0 else "decrease",
                rpm, tps, why)


def _afr(delta, rpm, tps, why, target=None):
    c = _chg("afr_target", "afr", delta,
             "increase" if delta > 0 else "decrease", rpm, tps, why)
    if target is not None:
        c["target_value"] = target
    return c


# ---------------------------------------------------------------------------
# The remedy book
# ---------------------------------------------------------------------------
# `changes` are candidates. They are filtered through guardrails before they
# reach the caller, so a book entry can never outrank a safety limit.

REMEDIES = {
    "decel_pop_high": {
        "symptom": "Popping on closed-throttle decel, mostly above 4000 rpm",
        "provenance": VALIDATED,
        "source": "COLLABORATION.md house decel-pop protocol",
        "changes": [
            _ve(+2.0, g.DECEL_POP_HIGH["rpm"], g.DECEL_POP_HIGH["tps"],
                "lean overrun in the high decel cells lets unburnt charge "
                "light off in a hot pipe; a small VE add richens it just "
                "enough to stop the pop"),
        ],
        "log": ["TPS", "RPM", "AFR actual vs target", "CHT"],
        "confirm": "closed-throttle 4000->2000 rpm decels in 3rd and 4th",
        "refute": "pops unchanged, or AFR at 0-2% TPS was already <14.0 "
                  "before the change (then it is not a lean-overrun pop)",
    },
    "decel_pop_broad": {
        "symptom": "Popping on decel across a broad rpm range, not just high",
        "provenance": VALIDATED,
        "source": "COLLABORATION.md house decel-pop protocol",
        "changes": [
            _spark(-1.0, g.DECEL_POP_BROAD["rpm"], g.DECEL_POP_BROAD["tps"],
                   "pulling a degree in the low decel cells moves combustion "
                   "later so less burnable mixture reaches the pipe"),
        ],
        "log": ["TPS", "RPM", "AFR actual vs target", "CHT"],
        "confirm": "closed-throttle 4000->2000 rpm decels in 3rd and 4th",
        "refute": "no change in pop frequency after a full heat cycle",
    },
    "running_hot": {
        "symptom": "High cylinder head temp; heat soak in traffic; leg heat",
        "provenance": INFERRED,
        "source": "timing backbone + belly de-rate + AFR cruise window",
        "changes": [
            _afr(-0.4, (2048, 3584), (10, 40),
                 "a richer cruise target uses fuel as charge cooling; this is "
                 "the cheapest heat lever before touching timing",
                 target=13.8),
            _spark(-2.0, g.BELLY_RPM, g.BELLY_TPS,
                   "the knock-prone midrange belly already runs ~3.5 degrees "
                   "below the backbone; pulling further here cuts peak "
                   "pressure and heat where the engine spends its life"),
        ],
        "log": ["CHT", "AFR actual vs target", "RPM", "TPS", "AutoTune trims"],
        "confirm": "same route, same ambient, compare peak and steady CHT",
        "refute": "CHT unchanged, or the bike now feels flat without running "
                  "cooler -- back it out; heat may be airflow or oil, not tune",
        "notes": [
            "Heat is the recurring theme across this bike's whole tune "
            "history (hopefullycooler, COOLERTUNEWITHMOREFUEL...). With no "
            "CHT ever logged, nothing in this project can yet tell you "
            "whether any past cooling attempt worked.",
            "Check the AutoTune gates first: learning is only valid between "
            f"{g.AUTOTUNE_ENABLE_F}F and {g.AUTOTUNE_DISABLE_F}F. Trims "
            "learned at 330-345F are heat-soak garbage and must not be "
            "locked in.",
        ],
    },
    "knock_under_load": {
        "symptom": "Detonation, pinging or rattle under load in the midrange",
        "provenance": INFERRED,
        "source": "timing backbone + belly de-rate",
        "changes": [
            _spark(-2.0, g.BELLY_RPM, g.BELLY_TPS,
                   "retard the belly cells where cylinder pressure peaks; "
                   f"the house step limit is {g.SPARK_MAX_STEP:.0f} degrees, "
                   "so take it in one step and ride it"),
        ],
        "log": ["RPM", "TPS", "CHT", "AFR actual vs target"],
        "confirm": "roll-on in top gear from 2000 rpm under load, listen",
        "refute": "knock persists after 2 degrees out -- stop pulling timing "
                  "and look at fuel quality, AFR, or a mechanical cause",
        "notes": ["Knock is the one symptom where doing nothing is worse than "
                  "an imperfect fix. Retard first, diagnose after."],
    },
    "lean_surge_cruise": {
        "symptom": "Surging or hunting at steady cruise, light throttle",
        "provenance": INFERRED,
        "source": "AFR cruise window",
        "changes": [
            _afr(-0.5, (2048, 3584), (5, 25),
                 "steady-cruise surge is usually a too-lean target chasing "
                 "closed-loop; bring the cruise target down toward the rich "
                 "end of the house window",
                 target=13.8),
        ],
        "log": ["AFR actual vs target", "TPS", "RPM", "AutoTune trims"],
        "confirm": "steady 45-60 mph cruise, watch for hunting",
        "refute": "AFR was already near 13.8 -- then it is not a lean surge; "
                  "look at the AutoTune zones or a vacuum leak",
    },
    "rich_black_plugs": {
        "symptom": "Fuel smell, sooty plugs, poor economy, eyes watering",
        "provenance": INFERRED,
        "source": "AFR cruise window",
        "changes": [
            _afr(+0.5, (2048, 3584), (5, 25),
                 "lean the cruise target back toward the middle of the house "
                 "window; do not chase the lean edge for economy",
                 target=14.2),
        ],
        "log": ["AFR actual vs target", "AutoTune trims", "CHT"],
        "confirm": "plug colour after a 30-minute steady ride",
        "refute": "CHT climbs -- richness was doing cooling work; put it back",
        "notes": ["Leaning for economy raises heat. On a bike whose history "
                  "is dominated by heat complaints, that is a bad trade."],
    },
    "stumble_off_idle": {
        "symptom": "Hesitation or stumble on initial tip-in from closed throttle",
        "provenance": INFERRED,
        "source": "VE low-rpm low-TPS cells",
        "changes": [
            _ve(+2.0, (768, 1792), (0, 10),
                "tip-in stumble is usually a momentary lean spot as the "
                "throttle plate opens ahead of the fuel; a small VE add in "
                "the low cells covers it"),
        ],
        "log": ["TPS", "RPM", "AFR actual vs target"],
        "confirm": "repeated gentle tip-ins from idle in 1st and 2nd",
        "refute": "no change -- on a TBW bike this can be throttle mapping "
                  "rather than fuel; do not keep adding fuel",
    },
    "flat_at_wot": {
        "symptom": "Pulls weakly at wide-open throttle, no top-end punch",
        "provenance": INFERRED,
        "source": "AFR WOT window + timing backbone",
        "changes": [
            _afr(-0.3, (3840, 5200), (80, 100),
                 "bring the WOT target into the house power window; richer "
                 "than 12.4 costs power, leaner than 12.8 costs safety margin",
                 target=12.6),
        ],
        "log": ["AFR actual vs target at WOT", "RPM", "CHT", "injector duty"],
        "confirm": "3rd gear roll-on to redline on a closed road or dyno",
        "refute": "AFR was already 12.4-12.8 -- then it is not fuelling; "
                  "check timing against the backbone and injector duty",
        "notes": [
            "Do NOT add timing to chase this without knowing you are below "
            f"MBT. The hard ceiling is {g.SPARK_CEILING:.0f} degrees and the "
            "virtual dyno has shown a +3 degree WOT change gaining no power "
            "while tripling knock risk.",
            "Check injector duty first: these are 6.3 g/s injectors. If duty "
            f"is over {g.INJECTOR_DUTY_AMBER_PCT:.0f}% you are out of fuel, "
            "not out of tune.",
        ],
    },
    "runs_bad_after_key_cycle": {
        "symptom": "The bike runs badly after a key cycle, or seems to revert "
                   "to a different tune between key cycles",
        "provenance": VALIDATED,
        "source": "ThunderMax product support 'How to Initialize a Thunder Max "
                  "Module' + ThunderMax 3-Mode Switch guide (docs/corpus)",
        "changes": [],
        "log": ["how long the key was held ON and OFF (seconds)",
                "whether a 3-mode / map switch is wired",
                "BARO sensor connector condition", "battery voltage at crank",
                "does it recover after a proper re-initialise?"],
        "confirm": "re-run the initialise EXACTLY: key ON 30s, OFF 30s, three "
                   "full times, then start and run to operating temperature",
        "refute": "still runs badly after a correctly-timed initialise and with "
                  "any map switch ruled out -- then stop tuning and look for a "
                  "hardware fault (sensor, connector, charging system)",
        "notes": [
            "THIS IS A DOCUMENTED CONDITION, NOT A MYSTERY. ThunderMax product "
            "support publishes the recovery verbatim: 'If after completing the "
            "Key Cycle the bike is running poorly, immediately shut engine off "
            "for one minute and repeat the process from the beginning.'",
            "CAUSE 1 -- THE KEY CYCLE WAS TOO FAST. The initialise is not a "
            "flick of the key. It is: key ON for 30 seconds, key OFF for 30 "
            "seconds, repeated three full times, then start and run to normal "
            "operating temperature. The support document says 'It is VERY "
            "important that when performing this cycle, the key remains in the "
            "ON and OFF position for a full 30 Seconds.' Short cycles leave "
            "the module part-initialised, which presents exactly as running "
            "badly or seeming to load different settings.",
            "CAUSE 2 -- THE BARO READING AT KEY-ON, and this is the one that "
            "explains 'reverted to another tune'. ThunderMax reads the BARO "
            "sensor PRIMARILY AT STARTUP and adjusts fuelling from it -- baro "
            "is read as altitude, once, at key-on. (The planned-but-unbuilt "
            "3-Mode Switch exploits exactly this: it spoofs the BARO voltage "
            "with resistor dividers to force a 'map switch'. The mechanism "
            "being real enough to build a feature on is what makes it real "
            "enough to be your fault mode.) NO SWITCH IS WIRED ON THIS BIKE -- "
            "confirmed 2026-08-27 -- so if each key-on lands on different "
            "behaviour, the prime suspect is the BARO input itself drifting or "
            "intermittent: a dirty/loose connector, chafed wire, or a failing "
            "sensor feeding a different 'altitude' to each start. That "
            "presents EXACTLY as 'it reverted to another tune'. Inspect the "
            "BARO connector and wiring before suspecting the module.",
            "CAUSE 3 -- low battery voltage during the cycle or cranking can "
            "stop the module committing cleanly. This one is general EFI "
            "experience rather than something stated in your ThunderMax "
            "documents, so treat it as the last thing to check, not the first.",
            "WHILE YOU ARE IN THERE: after any WRITE, the USB README procedure "
            "is initialise if asked, hands off the throttle, one start with no "
            "throttle, and both pipes hot within 30 seconds or shut it off.",
        ],
    },

    "decel_autotune_procedure": {
        "symptom": "Decel pop, and you want AutoTune to fix it rather than "
                   "hand-editing cells (requires WIDEBAND O2 sensors)",
        "provenance": VALIDATED,
        "source": "ThunderMax 'The Deceleration Pop Problem' guide "
                  "(docs/corpus/thundermax_nas_decelpop1_1.md)",
        "changes": [],
        "log": ["Decel Fuel Cut state (must be OFF)", "TPS", "RPM",
                "AFR actual vs target at 0-2% TPS", "CHT"],
        "confirm": "after the full ladder plus key cycles, ride the same "
                   "closed-throttle decels and listen",
        "refute": "pop unchanged after the complete ladder with Decel Cut OFF "
                  "-- then hand-apply the house decel-pop change instead, or "
                  "accept that this exhaust/engine combination can only be "
                  "minimised rather than cured",
        "notes": [
            "THE REASON AUTOTUNE 'DID NOTHING' IS USUALLY THIS. ThunderMax's "
            "own guide says the closed-throttle cells are 'NOT quickly "
            "developed in normal riding'. AutoTune only adjusts cells it "
            "actually visits with enough samples. Ride normally and it never "
            "sees decel properly, so it correctly reports no change.",
            "DECEL CUT MUST BE OFF for this. The guide states it in capitals: "
            "'Decel Cut Feature MUST be OFF during this development.' Decel "
            "Cut turns fuel off on overrun -- it is called 'a Band-Aid' in the "
            "same guide, and it masks the problem instead of tuning it. Your "
            "own tune notes also say Decel Fuel Cut OFF is correct for a "
            "2-into-1.",
            "THE LADDER. In 3rd or 4th so the rpm drops slowly, accelerate to "
            "the top figure, close the throttle completely, let it fall to the "
            "bottom figure. Repeat each 5+ times: 2500->1500, 2750->2250, "
            "3000->2500, 3250->2750, 3500->3000, 4000->3500, 4500->4000, "
            "5000->4500.",
            "THEN FEATHER. Repeat each band 'feathering' the throttle just off "
            "the closed position. This is what teaches the transition cells, "
            "and inaccurate transition fuel creates its own pops when the "
            "throttle re-opens.",
            "KEY-CYCLE BETWEEN SETS. 'Before repeating the above process "
            "again, stop, turn the engine off, restart and repeat.' The guide "
            "says this signals the AutoTune module that it may make MORE "
            "AGGRESSIVE adjustments. Skipping the key cycles is why a long "
            "single ride can still produce small corrections.",
            "EXPECTATION SETTING, in the guide's own words: 'There are no "
            "magical methods to cure ANY tuning system.' Some exhaust/engine "
            "combinations tune out completely; others can only be minimised. "
            "An open 2-into-1 is on the harder end.",
        ],
    },

    "autotune_wont_change_fuel": {
        "symptom": "AutoTune runs but refuses to change fuel/AFR — it only "
                   "suggests a little timing, while the bike runs hot and pops",
        "provenance": VALIDATED,
        "source": "ThunderMax AutoTune Zone Locking guide + ThunderMax "
                  "narrowband/closed-loop documentation (docs/corpus)",
        "changes": [],
        "log": ["AFR Target table — look for 0.00 cells (check FIRST)",
                "which zones show Closed Loop disabled",
                "AFR actual vs target", "CHT", "TPS", "RPM"],
        "confirm": "Tuning Maps -> Air/Fuel Ratio vs TPS @ RPM. Page through "
                   "the RPM/TPS grid and look for cells reading 0.00",
        "refute": "no 0.00 cells anywhere and actual AFR is NOT tracking the "
                  "target — then AutoTune really is failing to correct, and "
                  "the O2 sensors or their wiring are the next suspect",
        "notes": [
            "AutoTune IS NOT BROKEN AND IT IS NOT IGNORING YOU. It is doing a "
            "narrower job than most people expect, and three documented "
            "mechanisms each produce exactly this symptom.",
            "1) LOCKED CELLS. ThunderMax's own zone-locking guide says: 'Any "
            "cell set to 0.00 AFR is ignored by AutoTune, effectively locking "
            "it.' 0.00 is the documented way to switch AutoTune OFF for a "
            "zone. If a base map or an earlier session left 0.00 in the cells "
            "you care about, AutoTune will never touch fuel there no matter "
            "how many miles you ride. This is the first thing to check and it "
            "takes two minutes.",
            "2) IT CHASES THE TARGET, IT DOES NOT JUDGE THE TARGET. "
            "'AutoTune makes fueling changes by referencing the AFR Target "
            "Table.' If the engine is successfully hitting the target it was "
            "given, AutoTune has nothing to correct and reports no change -- "
            "even while the bike runs hot and pops, because a lean TARGET is "
            "not an error to AutoTune, it is the instruction. Fixing heat or "
            "pop caused by a lean target means changing the TARGET yourself. "
            "That is a tuner decision AutoTune will never make for you.",
            "3) IT ONLY ADJUSTS CELLS IT ACTUALLY VISITS. This is the one that "
            "catches people. ThunderMax's own decel-pop guide is explicit that "
            "the closed-throttle cells are 'NOT quickly developed in normal "
            "riding' -- you have to deliberately ride the pattern that exposes "
            "them. Ride normally and AutoTune sees cruise cells, finds them "
            "already on target, and correctly reports nothing to change. That "
            "is not a refusal, it is a lack of data. See the "
            "`decel_autotune_procedure` remedy for the exact ladder.",
            "SENSOR NOTE: with NARROWBAND O2 sensors, decel and WOT really are "
            "out of reach -- narrowbands are only accurate near stoich "
            "(~14.3-15.2 AFR) and outside that the system runs open loop with "
            "no O2 feedback at all. With WIDEBAND sensors that limit does not "
            "apply and AutoTune can work across the range, so on a wideband "
            "bike an empty result points at locked cells, on-target AFRs, or "
            "missing exposure -- not at the sensors.",
        ],
    },

    "autotune_not_learning": {
        "symptom": "AutoTune trims are not moving, or look wrong",
        "provenance": VALIDATED,
        "source": "COLLABORATION.md AutoTune gating rules + TMAX USB "
                  "READ_ME_FIRST flash procedure",
        "changes": [],
        "log": ["AutoTune enabled? (check FIRST)", "CHT",
                "AFR actual vs target", "AutoTune trims", "RPM", "TPS"],
        "confirm": f"with AutoTune ENABLED, ride until CHT sits between "
                   f"{g.AUTOTUNE_ENABLE_F}F and {g.AUTOTUNE_DISABLE_F}F, then "
                   f"re-check trims",
        "refute": "trims still flat with AutoTune ON and CHT inside the window "
                  "-- then suspect the O2 sensors or the AutoTune zone setup, "
                  "not the fuel map",
        "notes": [
            "CHECK THE SWITCH BEFORE THE TEMPERATURE. A fresh base-map flash "
            "is deliberately done with AutoTune OFF (per the TMAX USB "
            "READ_ME_FIRST procedure: injector size 6.3, idle 1024, decel cut "
            "OFF, AutoTune OFF). That is correct for the flash -- but AutoTune "
            "has to be turned back ON afterwards or the ECM never adapts the "
            "map to the engine, and every fuelling error simply stays.",
            "Diagnosing this as a heat problem when it is actually an OFF "
            "switch sends you chasing cooling for weeks. Confirmed on this "
            "bike 2026-08-27: an 'auto tune run' file differed from its base "
            "map by 47 bytes out of 214,967, against ~1,252 cells for a real "
            "session. AutoTune was off.",
            f"Once it IS on, it only learns between {g.AUTOTUNE_ENABLE_F}F and "
            f"{g.AUTOTUNE_DISABLE_F}F. Below that is cold-start enrichment, "
            "above it is heat soak; both are garbage as learning input. So the "
            "switch and the temperature window are two separate gates and BOTH "
            "have to be satisfied.",
            "This is a gating problem, not a map problem -- there is no table "
            "change to make. Fix the conditions and let the ECM do the work it "
            "is there to do.",
        ],
    },
}

# Words a rider actually uses -> remedy key.
ALIASES = {
    "pop": ("decel_pop_high", "decel_pop_broad", "decel_autotune_procedure"),
    "wideband": ("decel_autotune_procedure",),
    "widebands": ("decel_autotune_procedure",),
    "ladder": ("decel_autotune_procedure",),
    "popping": ("decel_pop_high", "decel_pop_broad"),
    "backfire": ("decel_pop_high", "decel_pop_broad"),
    "decel": ("decel_pop_high", "decel_pop_broad"),
    "hot": ("running_hot",),
    "heat": ("running_hot",),
    "temp": ("running_hot",),
    "cht": ("running_hot",),
    "knock": ("knock_under_load",),
    "ping": ("knock_under_load",),
    "pinging": ("knock_under_load",),
    "detonation": ("knock_under_load",),
    "rattle": ("knock_under_load",),
    "surge": ("lean_surge_cruise",),
    "surging": ("lean_surge_cruise",),
    "hunting": ("lean_surge_cruise",),
    "lean": ("lean_surge_cruise",),
    "rich": ("rich_black_plugs",),
    "sooty": ("rich_black_plugs",),
    "smell": ("rich_black_plugs",),
    "economy": ("rich_black_plugs",),
    "stumble": ("stumble_off_idle",),
    "hesitation": ("stumble_off_idle",),
    "hesitate": ("stumble_off_idle",),
    "tip-in": ("stumble_off_idle",),
    "bog": ("stumble_off_idle",),
    "flat": ("flat_at_wot",),
    "wot": ("flat_at_wot",),
    "power": ("flat_at_wot",),
    "slow": ("flat_at_wot",),
    "autotune": ("autotune_wont_change_fuel", "autotune_not_learning"),
    "revert": ("runs_bad_after_key_cycle",),
    "reverted": ("runs_bad_after_key_cycle",),
    "reverts": ("runs_bad_after_key_cycle",),
    "key": ("runs_bad_after_key_cycle",),
    "restart": ("runs_bad_after_key_cycle",),
    "initialize": ("runs_bad_after_key_cycle",),
    "initialise": ("runs_bad_after_key_cycle",),
    "baro": ("runs_bad_after_key_cycle",),
    "trims": ("autotune_not_learning", "autotune_wont_change_fuel"),
    "learning": ("autotune_not_learning", "autotune_wont_change_fuel"),
    "locked": ("autotune_wont_change_fuel",),
    "0.00": ("autotune_wont_change_fuel",),
    "refuses": ("autotune_wont_change_fuel",),
    "wont": ("autotune_wont_change_fuel",),
    "recommend": ("autotune_wont_change_fuel",),
    "recommended": ("autotune_wont_change_fuel",),
}


def vet_changes(changes):
    """Split candidate changes into (legal, rejected) using guardrails ONLY.

    A change that draws a `block` is dropped whole. It is never rescaled to
    sneak under the limit: the limits encode a step-then-verify sequence, and
    quietly halving a change to make it "pass" defeats the sequencing that
    makes it safe.
    """
    legal, rejected = [], []
    for c in changes:
        findings = g.check_change(c)
        blocks = [f for f in findings if f["severity"] == "block"]
        entry = dict(c)
        entry["findings"] = findings
        entry["warns"] = [f for f in findings if f["severity"] == "warn"]
        if blocks:
            entry["blocked_by"] = blocks
            rejected.append(entry)
        else:
            legal.append(entry)
    return legal, rejected


def advise(symptom_key):
    """Full recommendation for one remedy key, guardrail-filtered."""
    r = REMEDIES.get(symptom_key)
    if r is None:
        raise KeyError(symptom_key)
    legal, rejected = vet_changes(r["changes"])
    return {
        "key": symptom_key,
        "symptom": r["symptom"],
        "provenance": r["provenance"],
        "source": r["source"],
        "changes": legal,
        "rejected": rejected,
        "log": r["log"],
        "confirm": r["confirm"],
        "refute": r["refute"],
        "notes": r.get("notes", []),
        "protocol": ("Apply ONE change, ride the validation protocol, log the "
                     "fields above, then re-assess. Do not stack changes."),
    }


def match(text):
    """Remedy keys whose alias words appear in a free-text complaint."""
    words = set(str(text).lower().replace("/", " ").replace(",", " ").split())
    hits, seen = [], set()
    for w in words:
        w = w.strip(".!?;:'\"")
        for key in ALIASES.get(w, ()):
            if key not in seen:
                seen.add(key)
                hits.append(key)
    return hits


def render(a):
    L = []
    p = L.append
    tag = ("VALIDATED on this bike" if a["provenance"] == VALIDATED
           else "INFERRED - sound, but not yet ridden here")
    p(f"# {a['symptom']}")
    p("")
    p(f"**Provenance:** {tag}  ")
    p(f"**Source:** {a['source']}")
    p("")
    if a["changes"]:
        multi = len(a["changes"]) > 1
        p("## Change to make" if not multi else
          "## Changes, IN ORDER — apply the first, ride it, then re-assess")
        if multi:
            p("")
            p("These are sequential steps, NOT a list to apply together. "
              "Stacking them means that if the bike changes you will not know "
              "which change did it, and the house protocol is one change per "
              "validation ride.")
        p("")
        for n, c in enumerate(a["changes"], start=1):
            if multi:
                p(f"**Step {n}"
                  + ("** — try this first" if n == 1 else
                     "** — only if step 1 did not do enough"))
            sign = "+" if c["direction"] == "increase" else "-"
            unit = {"ve_pct": "%", "deg": "°", "afr": " AFR"}.get(c["unit"], "")
            where = []
            if c.get("rpm_band"):
                where.append(f"{c['rpm_band'][0]}-{c['rpm_band'][1]} rpm")
            if c.get("tps_band"):
                where.append(f"{c['tps_band'][0]}-{c['tps_band'][1]}% TPS")
            p(f"- **{c['table']}** {sign}{c['magnitude']:g}{unit}"
              + (f" @ {', '.join(where)}" if where else ""))
            if c.get("target_value") is not None:
                p(f"  - resulting target: {c['target_value']}")
            p(f"  - {c['why']}")
            for w in c["warns"]:
                p(f"  - ⚠️ {w['message']}")
    else:
        p("## No table change")
        p("")
        p("This symptom is not corrected by editing the map.")
    if a["rejected"]:
        p("")
        p("## Refused by the safety limits")
        p("")
        for c in a["rejected"]:
            for b in c["blocked_by"]:
                p(f"- {c['table']}: {b['message']}")
    p("")
    p("## Confirm it")
    p("")
    p(f"- Ride: {a['confirm']}")
    p(f"- Log: {', '.join(a['log'])}")
    p(f"- It did NOT work if: {a['refute']}")
    if a["notes"]:
        p("")
        p("## Before you do it")
        p("")
        for n in a["notes"]:
            p(f"- {n}")
    p("")
    p(f"_{a['protocol']}_")
    return "\n".join(L)


def main(argv=None):
    p = argparse.ArgumentParser(
        description="What to change on a ThunderMax tune for a given symptom")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="every symptom this can advise on")
    a1 = sub.add_parser("ask", help="describe the symptom in your own words")
    a1.add_argument("text", nargs="+")
    a1.add_argument("--json", action="store_true")
    a2 = sub.add_parser("show", help="one remedy by key")
    a2.add_argument("key")
    a2.add_argument("--json", action="store_true")
    a = p.parse_args(argv)

    if a.cmd == "list":
        for k, r in REMEDIES.items():
            tag = "validated" if r["provenance"] == VALIDATED else "inferred "
            print(f"  [{tag}] {k:24} {r['symptom']}")
        return 0

    keys = [a.key] if a.cmd == "show" else match(" ".join(a.text))
    if not keys:
        print("No remedy matched that description. Known symptoms:\n")
        for k, r in REMEDIES.items():
            print(f"  {k:24} {r['symptom']}")
        return 1
    out = []
    for k in keys:
        try:
            out.append(advise(k))
        except KeyError:
            print(f"unknown remedy key: {k}", file=sys.stderr)
            return 1
    if getattr(a, "json", False):
        print(json.dumps(out, indent=2))
        return 0
    for i, adv in enumerate(out):
        if i:
            print("\n" + "-" * 70 + "\n")
        print(render(adv))
    if len(out) > 1:
        print("\n> More than one remedy matched. They are alternatives, not a "
              "list to apply together — pick the one whose symptom description "
              "fits, change ONE thing, and ride it.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
