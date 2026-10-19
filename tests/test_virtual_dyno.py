#!/usr/bin/env python3
"""Tests for the deterministic virtual dyno (src/virtual_dyno.py).

Self-contained: pure arithmetic against data/dyno_baseline.json. No Ollama, no
Elasticsearch, no NAS, no network, no `.tbw` files. Run from the repo root:

    python3 -m unittest discover -s tests -v
"""
import json
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import guardrails as G
import virtual_dyno as vd


FRAME_KEYS = {
    "t", "rpm", "mph", "gear", "torque", "hp", "afr_front", "afr_rear",
    "afr_target", "injector_duty_pct", "spark_deg", "knock_risk", "cht_f",
}


def ve_change(mag, direction, rpm_lo, rpm_hi, tps_lo=80, tps_hi=100,
              table="ve_front"):
    return {"table": table, "cylinder": "both",
            "rpm_band": [rpm_lo, rpm_hi], "tps_band": [tps_lo, tps_hi],
            "direction": direction, "magnitude": mag, "unit": "ve_pct"}


def spark_change(mag, direction, rpm_lo, rpm_hi, tps_lo=80, tps_hi=100):
    return {"table": "spark_advance_front", "cylinder": "both",
            "rpm_band": [rpm_lo, rpm_hi], "tps_band": [tps_lo, tps_hi],
            "direction": direction, "magnitude": mag, "unit": "deg"}


# ---------------------------------------------------------------------------
# Calibration block
# ---------------------------------------------------------------------------

class TestBaselineBlock(unittest.TestCase):
    def test_load_baseline_is_memoized(self):
        self.assertIs(vd.load_baseline(), vd.load_baseline())

    def test_injector_flow_comes_from_the_bike_profile(self):
        """The hardware of record is bike_profile.json, not this file.

        dyno_baseline.json was seeded 5.5 g/s from an older corpus note for
        base map ZZSSQXETDN100720, but the 2026-08-19 USB pull had already
        moved the bike to 6.3 g/s injectors (S&S 550 / CAM2 /
        HXSSEDCAAN061617). Duty scales INVERSELY with flow, so two sources of
        truth meant every simulated pull could be read against the wrong
        hardware. The profile wins and says so.
        """
        inj = vd.load_baseline()["injectors"]
        prof = json.loads(
            (Path(vd.__file__).resolve().parent / "bike_profile.json").read_text()
        )
        self.assertEqual(inj["flow_gps"], prof["injectors"]["flow_gps"])
        self.assertEqual(inj["unit"], "g/s")
        self.assertEqual(inj["from"], "bike_profile.json")

    def test_published_anchors_land_on_the_curve(self):
        # ~140 lb-ft @ 3500 rpm and ~125 hp @ 5500 rpm.
        self.assertAlmostEqual(vd.baseline_torque(3500), 140.0, delta=0.05)
        hp_5500 = vd.baseline_torque(5500) * 5500 / vd.HP_TORQUE_CONST
        self.assertAlmostEqual(hp_5500, 125.0, delta=0.5)

    def test_torque_curve_is_smooth_and_does_not_overshoot(self):
        # PCHIP must never invent a value above the published peak.
        peak = max(p[1] for p in vd.load_baseline()["baseline"]["torque_curve"])
        for rpm in range(1000, 5801, 10):
            self.assertLessEqual(vd.baseline_torque(rpm), peak + 1e-6)
        # C0/near-C1: no step between adjacent samples.
        prev = vd.baseline_torque(1000)
        for rpm in range(1010, 5801, 10):
            cur = vd.baseline_torque(rpm)
            self.assertLess(abs(cur - prev), 1.0)
            prev = cur

    def test_guardrail_limits_are_not_redefined_locally(self):
        # The dyno must import limits, never shadow them.
        for name in ("AFR_WOT", "AFR_WOT_HARD", "SPARK_CEILING", "SPARK_WOT",
                     "TIMING_BACKBONE", "BELLY_DERATE_DEG", "HEAT_RETARD_CHT_F",
                     "INJECTOR_DUTY_AMBER_PCT", "INJECTOR_DUTY_RED_PCT",
                     "REAR_RICHER_AFR"):
            self.assertFalse(hasattr(vd, name),
                             f"virtual_dyno redefines guardrails.{name}")
        cal = vd.load_baseline()
        self.assertNotIn("timing_backbone", cal)
        self.assertNotIn("belly_derate_deg", cal)

    def test_calibration_status_and_uncertainty_present(self):
        cal = vd.load_baseline()
        self.assertEqual(cal["uncertainty_pct"], 15)
        self.assertIsInstance(cal["calibration_status"], str)
        self.assertTrue(cal["calibration_status"].strip())

    def test_gearing_block_is_flagged_and_usable(self):
        g = vd.load_baseline()["gearing"]
        self.assertTrue(g["needs_confirmation"])
        self.assertTrue(g["_confirm_these"])
        self.assertEqual(sorted(g["transmission_ratios"]),
                         ["1", "2", "3", "4", "5", "6"])
        # RPM -> MPH must be monotone and sane in top gear.
        self.assertGreater(vd.mph_from_rpm(5000, 6), vd.mph_from_rpm(5000, 5))
        self.assertGreater(vd.mph_from_rpm(3000, 5), 0)


# ---------------------------------------------------------------------------
# afr_shift
# ---------------------------------------------------------------------------

class TestAfrShift(unittest.TestCase):
    def test_exact_closed_form(self):
        for dve, afr in ((5, 14.7), (-5, 12.7), (2, 12.7), (-12.5, 13.2), (0, 12.7)):
            self.assertAlmostEqual(vd.afr_shift(dve, afr),
                                   -afr * dve / (100 + dve), places=12)

    def test_sign_and_magnitude(self):
        # Adding fuel -> richer -> AFR goes DOWN -> negative shift.
        self.assertAlmostEqual(vd.afr_shift(5, 14.7), -0.7, places=3)
        self.assertLess(vd.afr_shift(5, 12.7), 0)
        # Removing fuel -> leaner -> AFR goes UP -> positive shift.
        self.assertAlmostEqual(vd.afr_shift(-5, 12.7), 12.7 * 5 / 95, places=9)
        self.assertGreater(vd.afr_shift(-5, 12.7), 0)
        # No change -> no shift.
        self.assertEqual(vd.afr_shift(0, 12.7), 0.0)

    def test_shift_lands_on_the_scaled_afr(self):
        afr, dve = 12.7, 6.0
        self.assertAlmostEqual(afr + vd.afr_shift(dve, afr),
                               afr * 100.0 / (100.0 + dve), places=12)

    def test_adding_ve_never_directly_multiplies_torque(self):
        """A +2% VE add must move torque only through the AFR curve.

        The discarded draft multiplied torque by (1 + 0.9*dVE/100), which at
        +2% VE is +1.8% ~= +2.5 lb-ft on a 140 lb-ft peak. The AFR-only path
        moves peak torque by well under 0.5 lb-ft.
        """
        base = vd.simulate_pull([])
        rich = vd.simulate_pull([ve_change(2, "increase", 1000, 6000, 0, 100)])
        d_tq = (rich["summary"]["peak_torque"]
                - base["summary"]["peak_torque"])
        self.assertLess(abs(d_tq), 0.5, f"torque moved {d_tq} lb-ft on +2% VE — "
                                        "that looks like a torque multiplier")
        # ...but the AFR really did shift richer.
        self.assertLess(rich["samples"][0]["afr_front"],
                        base["samples"][0]["afr_front"])


# ---------------------------------------------------------------------------
# afr_power_factor
# ---------------------------------------------------------------------------

class TestAfrPowerFactor(unittest.TestCase):
    def test_ordering(self):
        p = vd.afr_power_factor
        self.assertEqual(p(12.7), p(13.0))
        self.assertGreater(p(13.0), p(13.5))
        self.assertGreater(p(13.5), p(14.7))

    def test_plateau_is_exactly_flat(self):
        for a in (12.6, 12.7, 12.8, 12.9, 13.0):
            self.assertEqual(vd.afr_power_factor(a), 1.0)

    def test_wot_safety_window_is_loss_free(self):
        # guardrails.AFR_WOT is 12.4-12.8; nothing inside it may cost >0.5%.
        lo, hi = G.AFR_WOT
        self.assertAlmostEqual(vd.afr_power_factor(12.4), 1.0, delta=0.005)
        a = lo
        while a <= hi + 1e-9:
            self.assertGreaterEqual(vd.afr_power_factor(a), 0.995)
            a += 0.05

    def test_continuity_at_every_knot(self):
        eps = 1e-6
        for knot in (13.0, 13.2, 12.6, 12.2, 12.0):
            left = vd.afr_power_factor(knot - eps)
            right = vd.afr_power_factor(knot + eps)
            self.assertAlmostEqual(left, right, places=7,
                                   msg=f"discontinuity at AFR {knot}")
            self.assertAlmostEqual(vd.afr_power_factor(knot), left, places=7)

    def test_no_jumps_anywhere_across_the_range(self):
        prev = vd.afr_power_factor(10.0)
        a = 10.01
        while a <= 18.0:
            cur = vd.afr_power_factor(a)
            self.assertLess(abs(cur - prev), 0.002)
            prev = cur
            a += 0.01

    def test_lean_and_rich_rates(self):
        # Beyond 13.2: -2%/AFR point on top of the ramp penalty.
        p132 = vd.afr_power_factor(13.2)
        self.assertAlmostEqual(p132 - vd.afr_power_factor(14.2), 0.02, places=6)
        # Below 12.0: -2%/pt. Across 12.0-12.2: -1%/pt.
        self.assertAlmostEqual(vd.afr_power_factor(12.2)
                               - vd.afr_power_factor(12.0), 0.002, places=6)
        self.assertAlmostEqual(vd.afr_power_factor(12.0)
                               - vd.afr_power_factor(11.0), 0.02, places=6)

    def test_bounded_0_to_1(self):
        for a in (8.0, 10.0, 12.7, 16.0, 22.0):
            v = vd.afr_power_factor(a)
            self.assertGreaterEqual(v, 0.0)
            self.assertLessEqual(v, 1.0)


# ---------------------------------------------------------------------------
# timing_power_factor
# ---------------------------------------------------------------------------

class TestTimingPowerFactor(unittest.TestCase):
    def test_quadratic_shape(self):
        self.assertAlmostEqual(vd.timing_power_factor(0.0), 1.0, places=12)
        self.assertAlmostEqual(vd.timing_power_factor(-5.0), 0.985, places=6)
        self.assertAlmostEqual(vd.timing_power_factor(-10.0), 0.94, places=6)

    def test_clamped_to_25_percent_loss(self):
        for d in (-25.0, -40.0, -100.0):
            self.assertAlmostEqual(vd.timing_power_factor(d), 0.75, places=9)

    def test_never_pays_more_than_1_0(self):
        for d in (-20.0, -1.0, 0.0, 1.0, 20.0):
            for tps in (0.0, 50.0, 100.0):
                self.assertLessEqual(vd.timing_power_factor(d, tps), 1.0)

    def test_asymmetric_past_mbt_at_load(self):
        # Knock-limited: past MBT buys nothing (no gain, and modelled flat).
        self.assertEqual(vd.timing_power_factor(5.0, tps_pct=100.0), 1.0)
        self.assertEqual(vd.timing_power_factor(10.0, tps_pct=80.0), 1.0)
        # Light load / unspecified load: over-advance is treated symmetrically.
        self.assertAlmostEqual(vd.timing_power_factor(5.0, tps_pct=5.0),
                               0.985, places=6)
        self.assertAlmostEqual(vd.timing_power_factor(5.0), 0.985, places=6)

    def test_mbt_curve_endpoints(self):
        self.assertAlmostEqual(vd.mbt_deg(0), 34.0, places=6)
        self.assertAlmostEqual(vd.mbt_deg(100), 28.0, places=6)
        self.assertGreater(vd.mbt_deg(20), vd.mbt_deg(90))


# ---------------------------------------------------------------------------
# Knock model
# ---------------------------------------------------------------------------

class TestKnockRisk(unittest.TestCase):
    def test_bounded(self):
        for args in ((3000, 100, 40.0, 15.5, 300.0),
                     (1200, 0, 0.0, 12.0, 100.0),
                     (5800, 100, 30.0, 12.7, 220.0)):
            r = vd.knock_risk(*args)
            self.assertGreaterEqual(r, 0.0)
            self.assertLessEqual(r, 1.0)

    def test_on_the_backbone_is_quiet(self):
        # Sitting exactly on the validated reference with a good AFR and a
        # head under the 226 F knee scores ~zero.
        for rpm in (1500, 2500, 3300, 4000, 5200):
            ref = vd.reference_spark(rpm, 100)
            self.assertLess(vd.knock_risk(rpm, 100, ref, 12.7, 220.0), 0.01)

    def test_advance_over_backbone_raises_risk(self):
        rpm = 4000
        ref = vd.reference_spark(rpm, 100)
        prev = vd.knock_risk(rpm, 100, ref, 12.7, 220.0)
        for extra in (1, 2, 4, 8):
            cur = vd.knock_risk(rpm, 100, ref + extra, 12.7, 220.0)
            self.assertGreater(cur, prev)
            prev = cur

    def test_lean_raises_risk_under_load_only_meaningfully(self):
        rpm, ref = 3800, vd.reference_spark(3800, 100)
        quiet = vd.knock_risk(rpm, 100, ref, 12.7, 220.0)
        lean = vd.knock_risk(rpm, 100, ref, 14.2, 220.0)
        self.assertGreater(lean, quiet)
        # Same lean AFR at closed throttle is far less alarming.
        self.assertLess(vd.knock_risk(rpm, 2, vd.reference_spark(rpm, 2),
                                      14.2, 220.0),
                        lean)

    def test_cht_term_uses_the_sourced_226f_knee(self):
        rpm, ref = 3800, vd.reference_spark(3800, 100)
        at_knee = vd.knock_risk(rpm, 100, ref, 12.7, G.HEAT_RETARD_CHT_F)
        over = vd.knock_risk(rpm, 100, ref, 12.7, G.HEAT_RETARD_CHT_F + 30)
        self.assertGreater(over, at_knee)
        self.assertAlmostEqual(
            vd.knock_risk(rpm, 100, ref, 12.7, G.HEAT_RETARD_CHT_F - 40),
            at_knee, places=9)

    def test_rear_cylinder_bias(self):
        rpm, ref = 3800, vd.reference_spark(3800, 100)
        f = vd.knock_risk(rpm, 100, ref, 12.7, 220.0, cylinder="front")
        r = vd.knock_risk(rpm, 100, ref, 12.7, 220.0, cylinder="rear")
        self.assertGreater(r, f)

    def test_breakdown_is_documented_and_adds_up(self):
        b = vd.knock_breakdown(4000, 100, 36.0, 14.0, 250.0, "rear")
        for k in ("risk", "reference_spark_deg", "advance_over_reference_deg",
                  "contrib_advance", "contrib_lean", "contrib_cht",
                  "contrib_rear_bias", "load_scale"):
            self.assertIn(k, b)
        total = (b["contrib_advance"] + b["contrib_lean"] + b["contrib_cht"]
                 + b["contrib_rear_bias"])
        self.assertAlmostEqual(b["risk"], min(1.0, total), places=3)

    def test_reference_spark_applies_the_belly_derate(self):
        # Inside guardrails.BELLY_RPM x BELLY_TPS the reference drops by
        # BELLY_DERATE_DEG relative to the raw backbone.
        rpm = 2500
        inside = vd.reference_spark(rpm, 50)      # 20-70% TPS -> belly
        outside = vd.reference_spark(rpm, 10)     # below the belly TPS band
        self.assertAlmostEqual(outside - inside, G.BELLY_DERATE_DEG, places=6)


# ---------------------------------------------------------------------------
# Injector duty
# ---------------------------------------------------------------------------

class TestInjectorDuty(unittest.TestCase):
    def test_pinned_closed_form_by_hand(self):
        """Hand-computed pin: 131 ci, VE 0.90, AFR 12.7, 5000 rpm, ISA-ish.

        per-cylinder swept volume = 131 ci * 16.387064 cc/ci / 2  = 1073.3527 cc
        T = (75-32)*5/9 + 273.15                                  =  297.0389 K
        P = 29.92 inHg * 3386.389 Pa/inHg                         = 101320.76 Pa
        rho = P / (287.058 * T)                        = 1.188385 kg/m^3
            = 0.001188385 g/cm^3
        air = 1073.3527 * 0.90 * 0.001188385 * 5000/120 = 47.8336 g/s
        fuel = 47.8336 / 12.7                           =  3.76643 g/s
        duty = 3.76643 / 5.5 * 100                      = 68.48 %
        """
        per_cyl_cc = 131.0 * 16.387064 / 2.0
        t_k = (75.0 - 32.0) * 5.0 / 9.0 + 273.15
        p_pa = 29.92 * 3386.389
        rho_g_cm3 = (p_pa / (287.058 * t_k)) * 1e-3
        air = per_cyl_cc * 0.90 * rho_g_cm3 * 5000.0 / 120.0
        flow = vd.load_baseline()["injectors"]["flow_gps"]
        expected = (air / 12.7) / flow * 100.0

        # 68.48% on the superseded 5.5 g/s injectors; 59.78% on the 6.3 g/s
        # hardware the bike actually runs. Derived from `flow` rather than
        # hardcoded so the pin tracks the profile instead of silently rotting.
        self.assertAlmostEqual(expected, 68.48 * 5.5 / flow, delta=0.05)
        got = vd.injector_duty_pct(5000, 0.90, 12.7, None)
        self.assertAlmostEqual(got, expected, places=9)

    def test_per_cylinder_displacement(self):
        self.assertAlmostEqual(vd.per_cylinder_cc(), 131.0 * 16.387064 / 2.0,
                               places=6)

    def test_scales_linearly_with_rpm_and_ve_and_inversely_with_afr(self):
        a = vd.injector_duty_pct(3000, 0.90, 12.7)
        self.assertAlmostEqual(vd.injector_duty_pct(6000, 0.90, 12.7), 2 * a,
                               places=6)
        self.assertAlmostEqual(vd.injector_duty_pct(3000, 0.45, 12.7), a / 2,
                               places=6)
        self.assertAlmostEqual(vd.injector_duty_pct(3000, 0.90, 25.4), a / 2,
                               places=6)

    def test_capped_at_100(self):
        self.assertLessEqual(vd.injector_duty_pct(9000, 1.4, 10.0), 100.0)

    def test_hot_thin_air_lowers_duty(self):
        cold = vd.injector_duty_pct(4000, 0.9, 12.7, {"ambient_f": 40})
        hot = vd.injector_duty_pct(4000, 0.9, 12.7, {"ambient_f": 110})
        self.assertGreater(cold, hot)

    def test_rho_air_units_and_direction(self):
        r = vd.rho_air(None)
        self.assertAlmostEqual(r, 0.0011884, delta=2e-6)   # g/cm^3
        self.assertGreater(vd.rho_air({"baro_inhg": 31.0}), r)
        self.assertLess(vd.rho_air({"ambient_f": 110}), r)


# ---------------------------------------------------------------------------
# simulate_pull — shape, identity, and the scenario cases
# ---------------------------------------------------------------------------

class TestSimulatePullShape(unittest.TestCase):
    def setUp(self):
        self.res = vd.simulate_pull([])

    def test_top_level_shape(self):
        self.assertEqual(set(self.res), {"samples", "issues", "summary",
                                         "baseline_status"})
        self.assertTrue(self.res["samples"])

    def test_frame_shape_and_gear_echo(self):
        for f in self.res["samples"]:
            self.assertEqual(set(f), FRAME_KEYS)
            self.assertEqual(f["gear"], 5)
        self.assertEqual(self.res["summary"]["gear"], 5)

    def test_sample_rate_and_duration(self):
        s = self.res["samples"]
        self.assertEqual(self.res["summary"]["sample_hz"], 20)
        self.assertAlmostEqual(s[1]["t"] - s[0]["t"], 0.05, places=6)
        dur = s[-1]["t"]
        self.assertGreaterEqual(dur, 10.0)
        self.assertLessEqual(dur, 20.0)
        self.assertGreaterEqual(len(s), 200)

    def test_gear_selectable_and_reflected(self):
        r4 = vd.simulate_pull([], gear=4)
        self.assertEqual(r4["summary"]["gear"], 4)
        self.assertTrue(all(f["gear"] == 4 for f in r4["samples"]))
        # Lower gear -> lower road speed at the same rpm.
        self.assertLess(r4["samples"][0]["mph"], self.res["samples"][0]["mph"])

    def test_rear_is_richer_and_never_advanced(self):
        for f in self.res["samples"]:
            self.assertAlmostEqual(f["afr_front"] - f["afr_rear"],
                                   G.REAR_RICHER_AFR, places=2)

    def test_cht_is_a_static_input_echo(self):
        chts = {f["cht_f"] for f in self.res["samples"]}
        self.assertEqual(len(chts), 1)
        self.assertIn("STATIC INPUT ECHO",
                      vd.load_baseline()["conditions_default"]["cht_f_note"])
        self.assertIn("STATIC INPUT ECHO", self.res["summary"]["cht_note"])
        hot = vd.simulate_pull([], conditions={"cht_f": 244})
        self.assertTrue(all(f["cht_f"] == 244.0 for f in hot["samples"]))

    def test_banner_and_calibration_status_always_present(self):
        s = self.res["summary"]
        self.assertIn("NOT a dyno", s["banner"])
        self.assertIn("DIRECTIONAL ONLY", s["banner"])
        self.assertTrue(s["calibration_status"])
        self.assertEqual(s["uncertainty_pct"], 15)
        bs = self.res["baseline_status"]
        self.assertTrue(bs["calibration_status"])
        self.assertFalse(bs["llm_involved"])
        self.assertTrue(bs["deterministic"])
        self.assertTrue(bs["needs_confirmation"])

    def test_deterministic(self):
        a = vd.simulate_pull([ve_change(2, "increase", 3000, 4000)])
        b = vd.simulate_pull([ve_change(2, "increase", 3000, 4000)])
        self.assertEqual(a, b)

    def test_peaks_match_the_published_anchors(self):
        s = self.res["summary"]
        self.assertAlmostEqual(s["peak_torque"], 140.0, delta=1.0)
        self.assertAlmostEqual(s["peak_hp"], 125.0, delta=1.5)


class TestIdentity(unittest.TestCase):
    def test_zero_changes_is_zero_delta(self):
        for changes in ([], None):
            res = vd.simulate_pull(changes)
            s = res["summary"]
            self.assertEqual(s["delta_hp_range"], [0, 0])
            self.assertEqual(s["delta_torque_range"], [0, 0])
            self.assertIn("no change", s["delta_hp"])
            self.assertIn("no change", s["delta_torque"])
            self.assertEqual(s["peak_hp"], s["baseline_peak_hp"])
            self.assertEqual(s["peak_torque"], s["baseline_peak_torque"])

    def test_zero_magnitude_change_is_also_identity(self):
        res = vd.simulate_pull([ve_change(0, "increase", 3000, 4000)])
        self.assertEqual(res["summary"]["delta_hp_range"], [0, 0])
        self.assertEqual(res["summary"]["delta_torque_range"], [0, 0])

    def test_out_of_band_change_is_identity(self):
        # Closed-throttle band never touched by a WOT pull.
        res = vd.simulate_pull([ve_change(2, "increase", 3840, 4608, 0, 2)])
        self.assertEqual(res["summary"]["delta_hp_range"], [0, 0])
        base = vd.simulate_pull([])
        self.assertEqual(res["samples"], base["samples"])


class TestBaselineWotPull(unittest.TestCase):
    def setUp(self):
        self.res = vd.simulate_pull([])

    def test_injector_duty_lands_in_the_expected_band(self):
        """A healthy WOT pull should use most of the injector but keep real
        headroom below the amber gate. The floor is 55% rather than the 70%
        that suited the superseded 5.5 g/s injectors: going up to 6.3 g/s
        buys headroom, so the SAME airflow now reads ~9 points lower. A peak
        that crept back near 70% on 6.3s would mean airflow assumptions grew,
        not that the bike got healthier."""
        peak = self.res["summary"]["peak_injector_duty_pct"]
        self.assertGreaterEqual(peak, 55.0, f"peak duty {peak}% is implausibly low")
        self.assertLessEqual(peak, 85.0, f"peak duty {peak}% is implausibly high")
        self.assertLess(peak, G.INJECTOR_DUTY_AMBER_PCT,
                        "a stock-baseline pull must not sit in the amber zone")

    def test_no_red_level_duty_finding(self):
        codes = [i["code"] for i in self.res["issues"]]
        self.assertNotIn("injector_duty_red", codes)
        self.assertNotIn("injector_duty_amber", codes)
        self.assertLess(self.res["summary"]["peak_injector_duty_pct"],
                        G.INJECTOR_DUTY_RED_PCT)

    def test_a_healthy_baseline_pull_is_quiet(self):
        self.assertEqual(self.res["issues"], [])


class TestLeanRun(unittest.TestCase):
    def test_ve_removal_warns_lean_at_the_right_rpm(self):
        res = vd.simulate_pull([ve_change(5, "decrease", 3000, 4000)])
        lean = [i for i in res["issues"] if i["code"] == "afr_lean_of_hard_limit"]
        self.assertEqual(len(lean), 1, f"expected one lean finding, got "
                                       f"{[i['code'] for i in res['issues']]}")
        it = lean[0]
        self.assertEqual(it["severity"], "warn")
        self.assertGreaterEqual(it["rpm"], 3000)
        self.assertLessEqual(it["rpm"], 4000)
        self.assertIn("Lean", it["message"])
        # And the modelled AFR really is leaner than the hard limit in-band.
        in_band = [f for f in res["samples"] if 3000 <= f["rpm"] <= 4000]
        self.assertTrue(in_band)
        self.assertTrue(all(f["afr_front"] > G.AFR_WOT_HARD[1] for f in in_band))
        # ...and untouched outside it.
        out = [f for f in res["samples"] if f["rpm"] > 4400]
        self.assertTrue(all(abs(f["afr_front"] - 12.7) < 0.01 for f in out))

    def test_lean_shift_matches_afr_shift(self):
        res = vd.simulate_pull([ve_change(5, "decrease", 3000, 4000)])
        f = next(f for f in res["samples"] if 3000 <= f["rpm"] <= 4000)
        self.assertAlmostEqual(f["afr_front"], 12.7 + vd.afr_shift(-5, 12.7),
                               places=2)


class TestSparkPastMbt(unittest.TestCase):
    def test_advance_past_mbt_gains_no_torque_but_raises_knock(self):
        base = vd.simulate_pull([])
        adv = vd.simulate_pull([spark_change(2, "increase", 4000, 5000)])

        # Baseline spark in that band already sits at/past model-MBT at WOT.
        band = [(f, i) for i, f in enumerate(base["samples"])
                if 4000 <= f["rpm"] <= 5000]
        self.assertTrue(band)
        self.assertGreaterEqual(band[0][0]["spark_deg"], vd.mbt_deg(100))

        # ~no torque gain
        d_hp = adv["summary"]["peak_hp"] - base["summary"]["peak_hp"]
        d_tq = adv["summary"]["peak_torque"] - base["summary"]["peak_torque"]
        self.assertLessEqual(d_hp, 0.5, "advance past MBT must not pay torque")
        self.assertLessEqual(d_tq, 0.5, "advance past MBT must not pay torque")

        # ...but knock risk definitely rises
        self.assertGreater(adv["summary"]["max_knock_risk"],
                           base["summary"]["max_knock_risk"])
        codes = [i["code"] for i in adv["issues"]]
        self.assertIn("past_mbt_no_gain", codes)
        self.assertTrue(all(i["severity"] == "warn" for i in adv["issues"]))

    def test_retarding_from_mbt_costs_torque(self):
        base = vd.simulate_pull([])
        ret = vd.simulate_pull([spark_change(6, "decrease", 1000, 6000, 0, 100)])
        self.assertLess(ret["summary"]["peak_torque"],
                        base["summary"]["peak_torque"])


class TestIssuesContract(unittest.TestCase):
    ISSUE_KEYS = {"t", "severity", "code", "message", "rpm", "detail"}

    def _all_runs(self):
        return [
            vd.simulate_pull([]),
            vd.simulate_pull([ve_change(5, "decrease", 3000, 4000)]),
            vd.simulate_pull([ve_change(12, "increase", 4000, 5800)]),
            vd.simulate_pull([spark_change(2, "increase", 4000, 5000)]),
            vd.simulate_pull([], conditions={"cht_f": 250}),
            vd.simulate_pull([], conditions={"cht_f": 300}),
            vd.simulate_pull([], conditions={"cht_f": 150}),
        ]

    def test_every_issue_is_advisory_and_well_formed(self):
        for res in self._all_runs():
            for i in res["issues"]:
                self.assertEqual(set(i), self.ISSUE_KEYS)
                self.assertEqual(i["severity"], "warn",
                                 "the virtual dyno may never emit a block")
                self.assertTrue(i["code"])
                self.assertTrue(i["message"])
                self.assertTrue(i["detail"])

    def test_cht_precondition_findings_are_at_t0(self):
        hot = vd.simulate_pull([], conditions={"cht_f": 250})
        pre = [i for i in hot["issues"] if i["code"].startswith("cht_precondition")]
        self.assertEqual(len(pre), 1)
        self.assertEqual(pre[0]["t"], 0.0)
        self.assertEqual(pre[0]["code"], "cht_precondition_heat_retard")

        very_hot = vd.simulate_pull([], conditions={"cht_f": 300})
        codes = [i["code"] for i in very_hot["issues"]]
        self.assertIn("cht_precondition_autotune_disable", codes)

        cold = vd.simulate_pull([], conditions={"cht_f": 150})
        codes = [i["code"] for i in cold["issues"]]
        self.assertIn("cht_precondition_autotune_cold", codes)

    def test_band_edge_envelope_is_labelled(self):
        """A change just below the pull's start rpm shows up ONLY once the
        band is widened by one grid cell — the worst-case band edge."""
        res = vd.simulate_pull([ve_change(5, "decrease", 2100, 2300)])
        edge = [i for i in res["issues"] if "worst-case band edge" in i["message"]]
        self.assertEqual(len(edge), 1)
        self.assertEqual(edge[0]["code"], "afr_lean_of_hard_limit")
        self.assertEqual(edge[0]["severity"], "warn")
        self.assertIn("worst-case band edge", edge[0]["detail"])
        # In-band (unwidened) nothing is lean, because the pull starts at 2500.
        self.assertTrue(all(f["afr_front"] <= G.AFR_WOT_HARD[1]
                            for f in res["samples"]))

    def test_no_band_edge_labels_without_changes(self):
        res = vd.simulate_pull([])
        self.assertFalse([i for i in res["issues"]
                          if "worst-case band edge" in i["message"]])

    def test_big_fuel_add_trips_the_duty_gauges(self):
        """+30%, not the +12% this pinned before the injector correction: on
        6.3 g/s injectors a 12% add peaks ~71%, still clear of the 80% amber
        gate. The gauge must fire on real headroom loss, not on a number that
        only looked alarming against the wrong hardware."""
        res = vd.simulate_pull([ve_change(30, "increase", 4000, 5800)])
        codes = [i["code"] for i in res["issues"]]
        self.assertTrue(any(c.startswith("injector_duty_") for c in codes))
        self.assertGreater(res["summary"]["peak_injector_duty_pct"],
                           G.INJECTOR_DUTY_AMBER_PCT)

    def test_a_modest_fuel_add_stays_out_of_the_amber_zone(self):
        """The other half of the same guarantee — no false alarms."""
        res = vd.simulate_pull([ve_change(12, "increase", 4000, 5800)])
        codes = [i["code"] for i in res["issues"]]
        self.assertFalse([c for c in codes if c.startswith("injector_duty_")])
        self.assertLess(res["summary"]["peak_injector_duty_pct"],
                        G.INJECTOR_DUTY_AMBER_PCT)


# ---------------------------------------------------------------------------
# Summary deltas: integer ranges ONLY
# ---------------------------------------------------------------------------

SUBINTEGER = re.compile(r"\d+\.\d")


class TestSummaryDeltas(unittest.TestCase):
    RUNS = None

    def _runs(self):
        return [
            vd.simulate_pull([]),
            vd.simulate_pull([ve_change(5, "decrease", 3000, 4000)]),
            vd.simulate_pull([ve_change(3, "increase", 2500, 5800, 0, 100)]),
            vd.simulate_pull([spark_change(2, "increase", 4000, 5000)]),
            vd.simulate_pull([spark_change(6, "decrease", 1000, 6000, 0, 100)]),
            vd.simulate_pull([{"table": "afr_target", "cylinder": "both",
                               "rpm_min": 3000, "rpm_max": 5800,
                               "tps_min": 80, "tps_max": 100,
                               "direction": "increase", "magnitude": 0.6,
                               "unit": "afr"}]),
        ]

    def test_no_subinteger_delta_anywhere_in_summary(self):
        for res in self._runs():
            for key, val in res["summary"].items():
                if "delta" not in key:
                    continue
                if isinstance(val, str):
                    self.assertIsNone(
                        SUBINTEGER.search(val),
                        f"summary[{key}] = {val!r} contains a sub-integer delta")
                elif isinstance(val, list):
                    for v in val:
                        self.assertIsInstance(v, int)
                        self.assertNotIsInstance(v, bool)
                else:
                    self.assertIsInstance(val, int)
                    self.assertNotIsInstance(val, bool)

    def test_delta_strings_are_ranges_with_units(self):
        for res in self._runs():
            s = res["summary"]
            self.assertTrue(s["delta_hp"].startswith("≈"))
            self.assertTrue(s["delta_hp"].endswith("hp")
                            or s["delta_hp"].endswith("(no change)"))
            self.assertIn("lb-ft", s["delta_torque"])
            lo, hi = s["delta_hp_range"]
            self.assertLessEqual(lo, hi)
            lo, hi = s["delta_torque_range"]
            self.assertLessEqual(lo, hi)

    def test_range_widens_outward_with_uncertainty(self):
        # +4.0 hp at 15% -> 3.4..4.6 -> "≈ +3 to +5 hp"
        txt, lo, hi = vd._delta_range(4.0, "hp", 15)
        self.assertEqual((lo, hi), (3, 5))
        self.assertEqual(txt, "≈ +3 to +5 hp")
        # Negative deltas widen the same way, outward.
        txt, lo, hi = vd._delta_range(-4.0, "hp", 15)
        self.assertEqual((lo, hi), (-5, -3))
        # Zero stays zero.
        txt, lo, hi = vd._delta_range(0.0, "hp", 15)
        self.assertEqual((lo, hi), (0, 0))
        self.assertIn("no change", txt)
        # A tiny delta must never print a decimal.
        txt, lo, hi = vd._delta_range(0.4, "hp", 15)
        self.assertEqual((lo, hi), (0, 1))
        self.assertIsNone(SUBINTEGER.search(txt))

    def test_true_delta_lies_inside_the_reported_range(self):
        for res in self._runs():
            s = res["summary"]
            true_hp = s["peak_hp"] - s["baseline_peak_hp"]
            lo, hi = s["delta_hp_range"]
            self.assertLessEqual(lo - 1e-9, true_hp)
            self.assertGreaterEqual(hi + 1e-9, true_hp)


# ---------------------------------------------------------------------------
# Change vocabulary
# ---------------------------------------------------------------------------

class TestChangeVocabulary(unittest.TestCase):
    def test_both_band_spellings_are_equivalent(self):
        a = vd.simulate_pull([{"table": "ve_front", "rpm_band": [3000, 4000],
                               "tps_band": [80, 100], "direction": "decrease",
                               "magnitude": 5, "unit": "ve_pct"}])
        b = vd.simulate_pull([{"table": "ve_front", "rpm_min": 3000,
                               "rpm_max": 4000, "tps_min": 80, "tps_max": 100,
                               "direction": "decrease", "magnitude": 5,
                               "unit": "ve_pct"}])
        self.assertEqual(a["samples"], b["samples"])

    def test_unit_is_inferred_from_the_guardrails_table_sets(self):
        for table, unit in (("ve_front", "ve_pct"), ("fuel_flow_rear", "ve_pct"),
                            ("spark_advance_front", "deg"),
                            ("rear_timing_offset", "deg"),
                            ("afr_target", "afr")):
            self.assertEqual(vd._unit_of({"table": table}), unit)

    def test_afr_target_change_moves_the_target_and_the_delivered_afr(self):
        res = vd.simulate_pull([{"table": "afr_target", "rpm_band": [3000, 5800],
                                 "tps_band": [80, 100], "direction": "increase",
                                 "magnitude": 0.5, "unit": "afr"}])
        f = next(f for f in res["samples"] if f["rpm"] >= 3200)
        self.assertAlmostEqual(f["afr_target"], 13.2, places=2)
        self.assertAlmostEqual(f["afr_front"], 13.2, places=2)

    def test_front_only_table_leaves_the_rear_alone(self):
        res = vd.simulate_pull([ve_change(5, "decrease", 3000, 4000,
                                          table="ve_front")])
        f = next(f for f in res["samples"] if 3000 <= f["rpm"] <= 4000)
        self.assertGreater(f["afr_front"], 13.2)
        self.assertAlmostEqual(f["afr_rear"], 12.7 - G.REAR_RICHER_AFR, places=2)

    def test_rear_timing_never_ends_up_advanced_of_front(self):
        res = vd.simulate_pull([{"table": "rear_timing_offset",
                                 "rpm_band": [1000, 6000],
                                 "tps_band": [0, 100], "direction": "increase",
                                 "magnitude": 3, "unit": "deg"}])
        base = vd.simulate_pull([])
        # Front untouched, rear clamped to front -> the pull is unchanged.
        self.assertEqual(res["samples"], base["samples"])

    def test_unknown_unit_is_ignored_not_crashed(self):
        res = vd.simulate_pull([{"table": "idle_rpm", "rpm_band": [1000, 6000],
                                 "tps_band": [0, 100], "direction": "increase",
                                 "magnitude": 50}])
        self.assertEqual(res["summary"]["delta_hp_range"], [0, 0])

    def test_bad_gear_raises(self):
        with self.assertRaises(ValueError):
            vd.simulate_pull([], gear=9)


if __name__ == "__main__":
    unittest.main()
