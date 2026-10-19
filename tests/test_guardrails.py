#!/usr/bin/env python3
"""Tests for src/guardrails.py — the single source of every numeric safety
limit, and the only thing in the app allowed to hard-block a tuning change.

Nothing here imports a model, a server, or the network: guardrails is pure
deterministic code by design, and these tests exist to keep it that way. They
pin BOTH halves of the contract:

  * the constants themselves (an accidental edit to a window or a step limit is
    a change to what the bike is allowed to be told to do), and
  * the findings check_change()/check_proposal() produce for real proposals.

The subtle one, called out explicitly below: per-cell .tbw scaling is still
unconfirmed, so the app frequently cannot know a cell's absolute value. Those
checks must WARN and increment `checks_unverifiable` — never silently pass.

    python3 -m unittest discover -s tests -v
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import guardrails as g


def change(**kw):
    """A structured change with sane defaults; override per test."""
    ch = {"table": "spark_advance_front", "cylinder": "both",
          "rpm_band": [2048, 2816], "tps_band": [0, 2],
          "direction": "increase", "magnitude": 1.0, "unit": "deg",
          "target_value": None, "current_value": None, "claim": ""}
    ch.update(kw)
    return ch


def rules(findings):
    return [f["rule"] for f in findings]


def blocks(findings):
    return [f for f in findings if f["severity"] == "block"]


def warns(findings):
    return [f for f in findings if f["severity"] == "warn"]


class ConstantsTest(unittest.TestCase):
    """The numbers themselves. Every other module imports these rather than
    restating them, so this is the one place they are asserted."""

    def test_afr_windows(self):
        self.assertEqual(g.AFR_WOT, (12.4, 12.8))
        self.assertEqual(g.AFR_WOT_HARD, (12.2, 13.2))
        self.assertEqual(g.AFR_CRUISE, (13.8, 14.6))
        self.assertEqual(g.AFR_IDLE, (13.8, 14.2))
        # the hard limits must bracket the target window, or the target window
        # is unreachable without tripping a block
        self.assertLessEqual(g.AFR_WOT_HARD[0], g.AFR_WOT[0])
        self.assertGreaterEqual(g.AFR_WOT_HARD[1], g.AFR_WOT[1])
        for lo, hi in (g.AFR_WOT, g.AFR_WOT_HARD, g.AFR_CRUISE, g.AFR_IDLE):
            self.assertLess(lo, hi)

    def test_rear_cylinder_rule(self):
        self.assertEqual(g.REAR_RICHER_AFR, 0.2)
        self.assertIn("rear_timing_offset", g.SPARK_TABLES)

    def test_spark_windows_and_step(self):
        self.assertEqual(g.SPARK_CRUISE, (28.0, 32.0))
        self.assertEqual(g.SPARK_WOT, (26.0, 30.0))
        self.assertEqual(g.SPARK_CEILING, 32.0)
        self.assertEqual(g.SPARK_MAX_STEP, 2.0)
        # the ceiling must not sit below either window's top
        self.assertGreaterEqual(g.SPARK_CEILING, g.SPARK_CRUISE[1])
        self.assertGreaterEqual(g.SPARK_CEILING, g.SPARK_WOT[1])

    def test_ve_steps(self):
        self.assertEqual(g.VE_STEP_WARN_PCT, 2.0)
        self.assertEqual(g.VE_STEP_BLOCK_PCT, 5.0)
        self.assertLess(g.VE_STEP_WARN_PCT, g.VE_STEP_BLOCK_PCT)
        # the block threshold is provisional (corpus trim-smoothing criterion),
        # and the payload must say so out loud
        self.assertTrue(g.as_dict()["ve_step"]["block_provisional"])

    def test_temperature_gates(self):
        self.assertEqual(g.AUTOTUNE_ENABLE_F, 200)
        self.assertEqual(g.AUTOTUNE_DISABLE_F, 280)
        self.assertEqual(g.HEAT_RETARD_CHT_F, 226)
        # the heat-retard knee sits inside the AutoTune learning window: timing
        # starts backing out before the trims are thrown away
        self.assertLess(g.AUTOTUNE_ENABLE_F, g.HEAT_RETARD_CHT_F)
        self.assertLess(g.HEAT_RETARD_CHT_F, g.AUTOTUNE_DISABLE_F)

    def test_injector_duty_gauges(self):
        self.assertEqual(g.INJECTOR_DUTY_AMBER_PCT, 80.0)
        self.assertEqual(g.INJECTOR_DUTY_RED_PCT, 90.0)
        self.assertLess(g.INJECTOR_DUTY_AMBER_PCT, g.INJECTOR_DUTY_RED_PCT)

    def test_decel_pop_high_protocol(self):
        """Pops above 4k -> +2% VE @ 0-2% TPS, 3840-4608 rpm."""
        self.assertEqual(g.DECEL_POP_HIGH["ve_pct"], 2.0)
        self.assertEqual(g.DECEL_POP_HIGH["tps"], (0, 2))
        self.assertEqual(g.DECEL_POP_HIGH["rpm"], (3840, 4608))

    def test_decel_pop_broad_protocol(self):
        """Broad-range pops -> -1 deg spark @ 0-2% TPS, 2048-2816 rpm."""
        self.assertEqual(g.DECEL_POP_BROAD["spark_deg"], -1.0)
        self.assertEqual(g.DECEL_POP_BROAD["tps"], (0, 2))
        self.assertEqual(g.DECEL_POP_BROAD["rpm"], (2048, 2816))

    def test_timing_backbone_is_monotonic(self):
        rpms = [r for r, _ in g.TIMING_BACKBONE]
        degs = [d for _, d in g.TIMING_BACKBONE]
        self.assertEqual(rpms, sorted(rpms), "backbone rpm axis must ascend")
        self.assertEqual(degs, sorted(degs), "advance must never fall with rpm")
        self.assertEqual(g.TIMING_BACKBONE[0], (900, 2.0), "idle sits at 0-3 deg")
        self.assertEqual(g.TIMING_BACKBONE[-1], (5200, 34.5), "34-35 deg plateau 5200+")

    def test_backbone_plateau_exceeds_the_proposal_ceiling(self):
        """Not a bug, and pinned here so nobody 'fixes' it: SPARK_CEILING (32)
        governs what a PROPOSAL may ask for, while TIMING_BACKBONE is the
        measured reference curve the dyno's knock model compares against, and
        its top-end plateau really does sit at 34.5 deg."""
        self.assertGreater(max(d for _, d in g.TIMING_BACKBONE), g.SPARK_CEILING)

    def test_belly_derate(self):
        self.assertEqual(g.BELLY_DERATE_DEG, 3.5)
        self.assertEqual(g.BELLY_RPM, (1800, 3500))
        self.assertEqual(g.BELLY_TPS, (20, 70))

    def test_table_vocabulary_is_partitioned(self):
        for group in (g.SPARK_TABLES, g.VE_TABLES, g.AFR_TABLES):
            self.assertTrue(group <= set(g.TABLES))
        self.assertFalse(g.SPARK_TABLES & g.VE_TABLES)
        self.assertFalse(g.SPARK_TABLES & g.AFR_TABLES)
        self.assertFalse(g.VE_TABLES & g.AFR_TABLES)

    def test_as_dict_carries_every_limit_the_ui_needs(self):
        d = g.as_dict()
        for key in ("afr", "spark", "ve_step", "temps_f", "injector_duty",
                    "decel_pop", "timing_backbone", "belly", "tables"):
            self.assertIn(key, d)
        self.assertTrue(d["never_write_tbw"])
        self.assertEqual(d["spark"]["max_step"], g.SPARK_MAX_STEP)
        self.assertEqual(d["afr"]["rear_richer"], g.REAR_RICHER_AFR)
        self.assertEqual(d["temps_f"]["heat_retard"], g.HEAT_RETARD_CHT_F)
        self.assertEqual(tuple(d["tables"]), g.TABLES)


class TableVocabularyTest(unittest.TestCase):
    def test_unknown_table_is_blocked_and_short_circuits(self):
        f = g.check_change(change(table="fuel map 3"))
        self.assertEqual(rules(f), ["table"])
        self.assertEqual(f[0]["severity"], "block")

    def test_every_named_table_is_accepted(self):
        for t in g.TABLES:
            f = g.check_change(change(table=t, unit="", magnitude=0.5,
                                      direction="decrease"))
            self.assertNotIn("table", rules(f), f"{t} should be a known page")


class SparkTest(unittest.TestCase):
    def test_two_degree_step_is_allowed(self):
        for direction in ("increase", "decrease"):
            f = g.check_change(change(magnitude=2.0, direction=direction,
                                      target_value=30.0))
            self.assertEqual(blocks(f), [], f"{direction} of exactly 2 deg is the limit")

    def test_three_degree_step_is_blocked(self):
        f = g.check_change(change(magnitude=3.0, target_value=30.0))
        self.assertIn("spark_step", rules(blocks(f)))

    def test_three_degree_retard_is_also_blocked(self):
        """The +/-2 deg rule is symmetric — a big yank the other way is just as
        unlogged."""
        f = g.check_change(change(magnitude=3.0, direction="decrease",
                                  target_value=24.0))
        self.assertIn("spark_step", rules(blocks(f)))

    def test_target_above_the_ceiling_is_blocked(self):
        f = g.check_change(change(magnitude=1.0, target_value=32.5))
        self.assertIn("spark_ceiling", rules(blocks(f)))

    def test_target_at_the_ceiling_is_allowed(self):
        f = g.check_change(change(magnitude=1.0, target_value=g.SPARK_CEILING))
        self.assertEqual(blocks(f), [])

    def test_cruise_and_wot_window_tops_pass_the_ceiling_check(self):
        for target in (g.SPARK_CRUISE[1], g.SPARK_WOT[1]):
            f = g.check_change(change(magnitude=1.0, target_value=target))
            self.assertEqual(blocks(f), [], f"{target} deg is inside a house window")

    def test_rear_timing_offset_may_not_advance(self):
        f = g.check_change(change(table="rear_timing_offset", magnitude=1.0,
                                  direction="increase", target_value=1.0))
        self.assertIn("rear_timing", rules(blocks(f)))

    def test_rear_timing_offset_may_retard(self):
        """Equal-or-retarded vs front: pulling the rear back is the safe way."""
        f = g.check_change(change(table="rear_timing_offset", magnitude=1.0,
                                  direction="decrease", target_value=-1.0))
        self.assertEqual(blocks(f), [])

    def test_unit_deg_triggers_spark_rules_on_any_table(self):
        """A degree-unit change on a non-spark page still gets the step rule —
        the unit, not just the table name, selects the check."""
        f = g.check_change(change(table="idle_rpm", unit="deg", magnitude=4.0))
        self.assertIn("spark_step", rules(blocks(f)))


class UnverifiableTest(unittest.TestCase):
    """Per-cell .tbw scaling is unconfirmed, so absolute values are often
    unknowable from here. That must produce a visible WARN counted in
    `checks_unverifiable` — never a silent pass."""

    def test_advance_with_unknown_current_value_warns(self):
        f = g.check_change(change(magnitude=1.0, target_value=None,
                                  current_value=None))
        self.assertEqual(blocks(f), [])
        self.assertIn("spark_absolute", rules(warns(f)))
        self.assertIn("TMax Tuner", warns(f)[0]["message"])

    def test_unknown_advance_increments_checks_unverifiable(self):
        r = g.check_proposal([change(magnitude=1.0, target_value=None)])
        self.assertTrue(r["passed"])
        self.assertEqual(r["blocks"], 0)
        self.assertEqual(r["checks_unverifiable"], 1,
                         "an unknowable absolute value must be counted, not ignored")
        self.assertEqual(r["warns"], 1)

    def test_known_target_does_not_count_as_unverifiable(self):
        r = g.check_proposal([change(magnitude=1.0, target_value=30.0)])
        self.assertEqual(r["checks_unverifiable"], 0)
        self.assertEqual(r["warns"], 0)

    def test_retard_with_unknown_value_needs_no_warning(self):
        """Pulling timing out cannot walk into the ceiling, so there is nothing
        to verify."""
        r = g.check_proposal([change(magnitude=1.0, direction="decrease",
                                     target_value=None)])
        self.assertEqual(r["checks_unverifiable"], 0)
        self.assertEqual(r["warns"], 0)

    def test_afr_with_unknown_target_warns_and_counts(self):
        r = g.check_proposal([change(table="afr_target", unit="afr",
                                     rpm_band=[2000, 3000], tps_band=[20, 40],
                                     magnitude=0.2, target_value=None)])
        self.assertTrue(r["passed"])
        self.assertEqual(r["checks_unverifiable"], 1)
        self.assertIn("afr_absolute", rules(r["findings"]))

    def test_unverifiable_counts_accumulate_across_changes(self):
        r = g.check_proposal([
            change(magnitude=1.0, target_value=None),
            change(table="afr_target", unit="afr", tps_band=[20, 40],
                   magnitude=0.2, target_value=None),
        ])
        self.assertEqual(r["checks_unverifiable"], 2)
        self.assertEqual({f["change_idx"] for f in r["findings"]}, {0, 1})


class AfrWindowTest(unittest.TestCase):
    def _afr(self, target, rpm, tps):
        return g.check_change(change(table="afr_target", unit="afr",
                                     rpm_band=rpm, tps_band=tps,
                                     magnitude=0.2, target_value=target))

    def test_wot_hard_limits_bound_the_wot_band(self):
        wot_rpm, wot_tps = [4000, 5500], [80, 100]
        for ok in (12.2, 12.4, 12.6, 12.8, 13.2):
            self.assertEqual(blocks(self._afr(ok, wot_rpm, wot_tps)), [],
                             f"{ok} is inside the WOT hard window")
        for bad in (12.1, 13.3, 14.0):
            self.assertIn("afr_window", rules(blocks(self._afr(bad, wot_rpm, wot_tps))),
                          f"{bad} must be blocked at WOT")

    def test_wot_target_window_sits_inside_the_hard_window(self):
        """12.4-12.8 is the aim; the block only fires outside 12.2-13.2."""
        for target in g.AFR_WOT:
            self.assertEqual(blocks(self._afr(target, [4000, 5500], [80, 100])), [])

    def test_cruise_window(self):
        cruise_rpm, cruise_tps = [2500, 3500], [15, 40]
        for ok in (13.8, 14.2, 14.6):
            self.assertEqual(blocks(self._afr(ok, cruise_rpm, cruise_tps)), [])
        for bad in (13.7, 14.7, 12.6):
            f = blocks(self._afr(bad, cruise_rpm, cruise_tps))
            self.assertIn("afr_window", rules(f))
            self.assertIn("cruise", f[0]["message"])

    def test_idle_window_is_tighter_than_cruise(self):
        idle_rpm, idle_tps = [900, 1100], [0, 3]
        for ok in (13.8, 14.0, 14.2):
            self.assertEqual(blocks(self._afr(ok, idle_rpm, idle_tps)), [])
        for bad in (13.7, 14.3):
            f = blocks(self._afr(bad, idle_rpm, idle_tps))
            self.assertIn("afr_window", rules(f))
            self.assertIn("idle", f[0]["message"])
        # 14.5 is fine cruising but out of the idle window
        self.assertEqual(blocks(self._afr(14.5, [2500, 3500], [15, 40])), [])
        self.assertIn("afr_window", rules(blocks(self._afr(14.5, idle_rpm, idle_tps))))

    def test_band_selection_prefers_wot_over_idle(self):
        """A wide-open pull that starts down low is still a WOT cell."""
        f = self._afr(12.6, [900, 1200], [85, 100])
        self.assertEqual(blocks(f), [], "WOT classification must win on tps")


class VeStepTest(unittest.TestCase):
    def _ve(self, magnitude, direction="increase", table="ve_front"):
        return g.check_change(change(table=table, unit="ve_pct",
                                     magnitude=magnitude, direction=direction,
                                     rpm_band=[3840, 4608], tps_band=[0, 2]))

    def test_two_percent_is_the_validated_house_step(self):
        self.assertEqual(self._ve(2.0), [], "+2% VE is the validated house step")
        self.assertEqual(self._ve(2.0, "decrease"), [])

    def test_beyond_two_percent_warns(self):
        f = self._ve(3.0)
        self.assertEqual(blocks(f), [])
        self.assertIn("ve_step", rules(warns(f)))

    def test_beyond_five_percent_blocks(self):
        f = self._ve(5.5)
        self.assertIn("ve_step", rules(blocks(f)))
        self.assertIn("provisional", blocks(f)[0]["message"])

    def test_exactly_five_percent_is_a_warn_not_a_block(self):
        f = self._ve(5.0)
        self.assertEqual(blocks(f), [])
        self.assertIn("ve_step", rules(warns(f)))

    def test_pulling_fuel_is_bounded_the_same_way(self):
        self.assertIn("ve_step", rules(blocks(self._ve(6.0, "decrease"))))
        self.assertIn("ve_step", rules(warns(self._ve(3.0, "decrease"))))

    def test_every_fuel_page_is_covered(self):
        for table in sorted(g.VE_TABLES):
            self.assertIn("ve_step", rules(blocks(self._ve(9.0, table=table))),
                          f"{table} must be step-limited")


class DecelPopProtocolTest(unittest.TestCase):
    """The two validated house protocols must pass their own guardrails — if
    the shop's own recipe trips a finding, a constant drifted."""

    def test_high_rpm_protocol_is_clean(self):
        p = g.DECEL_POP_HIGH
        r = g.check_proposal([change(table="ve_front", unit="ve_pct",
                                     magnitude=p["ve_pct"], direction="increase",
                                     rpm_band=list(p["rpm"]), tps_band=list(p["tps"]))])
        self.assertTrue(r["passed"])
        self.assertEqual(r["findings"], [])
        self.assertEqual(r["checks_unverifiable"], 0)

    def test_broad_range_protocol_is_clean(self):
        p = g.DECEL_POP_BROAD
        r = g.check_proposal([change(table="spark_advance_front", unit="deg",
                                     magnitude=abs(p["spark_deg"]),
                                     direction="decrease",
                                     rpm_band=list(p["rpm"]), tps_band=list(p["tps"]))])
        self.assertTrue(r["passed"])
        self.assertEqual(r["findings"], [])

    def test_doubling_the_high_rpm_protocol_warns(self):
        p = g.DECEL_POP_HIGH
        r = g.check_proposal([change(table="ve_front", unit="ve_pct",
                                     magnitude=p["ve_pct"] * 2,
                                     rpm_band=list(p["rpm"]), tps_band=list(p["tps"]))])
        self.assertTrue(r["passed"], "4% is a warn, not a block")
        self.assertEqual(r["warns"], 1)


class ProposalTest(unittest.TestCase):
    def test_findings_carry_their_change_index(self):
        r = g.check_proposal([
            change(magnitude=1.0, target_value=30.0),          # clean
            change(magnitude=4.0, target_value=30.0),          # blocked
        ])
        self.assertFalse(r["passed"])
        self.assertEqual(r["blocks"], 1)
        self.assertEqual(r["findings"][0]["change_idx"], 1)

    def test_clean_proposal_passes(self):
        r = g.check_proposal([change(magnitude=1.0, target_value=29.0)])
        self.assertTrue(r["passed"])
        self.assertEqual(r["findings"], [])
        self.assertEqual(r["warns"], 0)

    def test_empty_proposal_is_vacuously_clean(self):
        r = g.check_proposal([])
        self.assertTrue(r["passed"])
        self.assertEqual(r["blocks"], 0)

    def test_counts_are_self_consistent(self):
        r = g.check_proposal([
            change(magnitude=4.0),                                  # block + warn
            change(table="ve_front", unit="ve_pct", magnitude=3.0),  # warn
        ])
        self.assertEqual(r["blocks"] + r["warns"], len(r["findings"]))
        self.assertFalse(r["passed"])


class StackingGuardTest(unittest.TestCase):
    """The cross-proposal net: three separately-'safe' +2 deg steps must not
    ladder into +6 deg one approval at a time."""

    def test_net_spark_over_the_step_limit_blocks(self):
        r = g.check_proposal([change(magnitude=2.0, target_value=30.0)],
                             overlapping_net={"deg": 4.0})
        self.assertIn("spark_stacking", rules(blocks(r["findings"])))
        self.assertFalse(r["passed"])

    def test_net_spark_at_the_limit_is_allowed(self):
        r = g.check_proposal([change(magnitude=2.0, target_value=30.0)],
                             overlapping_net={"deg": 2.0})
        self.assertTrue(r["passed"])

    def test_net_retard_is_bounded_too(self):
        r = g.check_proposal([change(magnitude=2.0, direction="decrease",
                                     target_value=26.0)],
                             overlapping_net={"deg": -4.0})
        self.assertIn("spark_stacking", rules(blocks(r["findings"])))

    def test_net_ve_warns_then_blocks(self):
        warn = g.check_proposal([change(table="ve_front", unit="ve_pct", magnitude=2.0)],
                                overlapping_net={"ve_pct": 4.0})
        self.assertTrue(warn["passed"])
        self.assertIn("ve_stacking", rules(warns(warn["findings"])))
        hard = g.check_proposal([change(table="ve_front", unit="ve_pct", magnitude=2.0)],
                                overlapping_net={"ve_pct": 6.0})
        self.assertFalse(hard["passed"])
        self.assertIn("ve_stacking", rules(blocks(hard["findings"])))

    def test_no_overlap_means_no_stacking_findings(self):
        r = g.check_proposal([change(magnitude=2.0, target_value=30.0)],
                             overlapping_net=None)
        self.assertEqual(rules(r["findings"]), [])


class PurityTest(unittest.TestCase):
    def test_guardrails_imports_nothing(self):
        """The block authority must stay offline and deterministic: no urllib,
        no model client, no retrieval — in fact no imports at all, so it can
        never acquire one by transitivity either."""
        import ast
        tree = ast.parse(Path(g.__file__).read_text())
        imported = [n for n in ast.walk(tree)
                    if isinstance(n, (ast.Import, ast.ImportFrom))]
        self.assertEqual(imported, [],
                         "guardrails.py must import nothing at all")

    def test_no_io_or_model_calls_at_module_scope(self):
        for banned in ("urlopen", "requests.", "socket.", "open(", "subprocess"):
            self.assertNotIn(banned, Path(g.__file__).read_text(),
                             f"guardrails must not reach for {banned}")

    def test_checks_are_repeatable(self):
        ch = change(magnitude=3.0, target_value=33.0)
        self.assertEqual(g.check_change(ch), g.check_change(ch))


if __name__ == "__main__":
    unittest.main()
