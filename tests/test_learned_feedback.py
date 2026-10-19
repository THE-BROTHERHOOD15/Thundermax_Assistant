"""Tests for learned_feedback -- reading the ECM's own AutoTune learning.

Synthetic tunes only. No NAS, no Ollama, no real .tbw needed.

The statistics carry the whole weight of this module's claims, so most of
these tests are about the test itself refusing to over-claim: a single write
that touches many cells must NOT read as a trend, and the multiple-comparison
correction must actually bite.
"""
import struct
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))

import learned_feedback as lf  # noqa: E402

BAND = "autotune_learned"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def blank():
    """A zero-filled buffer of the right size with a plausible header."""
    data = bytearray(lf.EXPECTED_SIZE)
    struct.pack_into("<4I", data, 0, 0x87, 0x4000, 0x1, 0x147)
    data[0x10] = 0x10
    data[0x11:0x21] = b"HXSSEDCAAN061617"
    return data


def write_cells(data, band, values, start=0):
    lo, _, stride, width = lf.band_grid(band)
    for i, v in enumerate(values):
        struct.pack_into("<h", data, lo + (start + i) * stride, v)
    return data


def make_series(tmp_path, per_save, band_name=BAND, start=0):
    """Write one .tbw per entry in `per_save`; each entry is a cell list."""
    band = lf._band(band_name)
    paths = []
    for n, vals in enumerate(per_save):
        d = blank()
        write_cells(d, band, vals, start)
        p = tmp_path / f"t{n:02d}.tbw"
        p.write_bytes(bytes(d))
        paths.append(p)
    return paths


# ---------------------------------------------------------------------------
# cell reading
# ---------------------------------------------------------------------------

def test_cells_are_signed():
    """A -1 trim is stored as 0xFFFF and must not read as 65535."""
    band = lf._band(BAND)
    d = blank()
    write_cells(d, band, [-1, -22, 236, 0])
    got = lf.cells(bytes(d), band)[:4]
    assert got == [-1, -22, 236, 0]


def test_band_grid_anchors_on_odd_band_start():
    """autotune_learned starts at an ODD offset; cell 0 must land on it.

    Snapping to an even file offset reads every record one byte early and
    inflates deltas by 256 -- the bug fixed on 2026-08-20.
    """
    band = lf._band(BAND)
    lo, _, stride, width = lf.band_grid(band)
    assert lo == 0x0DDC5
    assert lo % 2 == 1, "band start is odd; this test guards the alignment fix"
    assert lf.cell_offset(band, 0) == lo
    assert lf.cell_offset(band, 1) == lo + stride


def test_band_grid_width_is_two_not_stride():
    """learned_ve_bulk must not be read as a 4-byte int (merges two fields)."""
    _, _, stride, width = lf.band_grid(lf._band("learned_ve_bulk"))
    assert width == 2 and stride == 2


def test_read_tune_rejects_wrong_size(tmp_path):
    p = tmp_path / "short.tbw"
    p.write_bytes(b"\x00" * 100)
    with pytest.raises(ValueError, match="expected"):
        lf.read_tune(p)


# ---------------------------------------------------------------------------
# statistics
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("k,m,expect", [
    (6, 6, 0.03125),
    (10, 10, 2 * 0.5 ** 10),
    (5, 6, 0.21875),
    (3, 6, 1.0),
    (0, 0, 1.0),
])
def test_binom_two_sided_exact(k, m, expect):
    assert lf.binom_two_sided(k, m) == pytest.approx(expect)


def test_binom_is_symmetric():
    """k and m-k describe the same lopsidedness."""
    assert lf.binom_two_sided(2, 7) == lf.binom_two_sided(5, 7)


def test_benjamini_hochberg_matches_reference():
    p = [0.001, 0.008, 0.039, 0.041, 0.042, 0.06, 0.074, 0.205, 0.212, 0.216]
    q = lf.benjamini_hochberg(p)
    assert q[0] == pytest.approx(0.01)
    assert q[1] == pytest.approx(0.04)
    assert q[2] == pytest.approx(0.084)
    assert all(a <= b + 1e-12 for a, b in zip(q, q[1:])), "must be monotone"


def test_benjamini_hochberg_empty():
    assert lf.benjamini_hochberg([]) == []


def test_fdr_suppresses_noise():
    """Under a true null, thousands of tests must yield no findings.

    A true null gives p-values UNIFORM on [0,1] -- that is what noise looks
    like. (Handing BH 2000 p-values all equal to 0.04 is not a null at all:
    every test agreeing at 0.04 is a global effect, and BH correctly returns
    q=0.04. That earlier version of this test was checking the wrong thing.)
    """
    n = 2000
    uniform = [(i + 0.5) / n for i in range(n)]
    q = lf.benjamini_hochberg(uniform)
    assert min(q) > 0.05, "uniform p-values must produce no discoveries"


def test_fdr_finds_a_real_signal_buried_in_noise():
    """BH must still have power: a few tiny p-values among noise survive."""
    n = 2000
    vals = [(i + 0.5) / n for i in range(n)]
    vals[:5] = [1e-8, 2e-8, 3e-8, 4e-8, 5e-8]
    q = lf.benjamini_hochberg(vals)
    assert sum(1 for x in q if x <= 0.05) >= 5


# ---------------------------------------------------------------------------
# trend / bias
# ---------------------------------------------------------------------------

def test_trend_needs_two_tunes(tmp_path):
    paths = make_series(tmp_path, [[0] * 8])
    with pytest.raises(ValueError, match="at least two"):
        lf.trend(paths, bands=(BAND,))


def test_trend_records_direction_and_net(tmp_path):
    paths = make_series(tmp_path, [[0], [5], [9]])
    res = lf.trend(paths, bands=(BAND,))[BAND]
    cell = next(c for c in res["cells"] if c["cell"] == 0)
    assert cell["up"] == 2 and cell["down"] == 0
    assert cell["net_raw"] == 9
    assert cell["direction"] == "rich/up"
    assert res["steps"] == 2


def test_unmoved_cells_are_not_reported(tmp_path):
    paths = make_series(tmp_path, [[0, 3], [0, 4]])
    res = lf.trend(paths, bands=(BAND,))[BAND]
    assert [c["cell"] for c in res["cells"]] == [1]


# ---------------------------------------------------------------------------
# the guard that matters most
# ---------------------------------------------------------------------------

def test_single_write_across_many_cells_is_not_a_trend(tmp_path):
    """One save moving a whole block must NOT be called a trend.

    This is the real failure mode. On the latestandgreatest series, 48
    adjacent learned_ve_bulk cells all moved down inside a single save; a
    naive pooled sign test scored that p=1.6e-13 as though 48 independent
    measurements agreed. It is one write.
    """
    n = 96
    flat = [0] * n
    moved = [-5] * n
    # 5 saves, but the block only ever changes once (step 0)
    paths = make_series(tmp_path, [flat, moved, moved, moved, moved])
    res = lf.region_bias(paths, bands=(BAND,), window=48)[BAND]
    assert res["established"] == []
    assert res["suggestive"] == []
    flagged = [w for w in res["windows"] if w["moves"]]
    assert flagged, "the window did move; it should be reported as single-event"
    assert all(w["verdict"].startswith("single event") for w in flagged)


def test_repeated_same_direction_is_established(tmp_path):
    """A block pulled the same way on every save is a real trend."""
    n = 96
    saves = [[-2 * s] * n for s in range(9)]      # 8 steps, all downward
    paths = make_series(tmp_path, saves)
    res = lf.region_bias(paths, bands=(BAND,), window=48)[BAND]
    assert res["established"], "8 consecutive same-direction saves must clear"
    w = res["established"][0]
    assert w["direction"] == "lean/down"
    assert w["steps_moved"] == 8 and w["steps_same_way"] == 8
    assert w["q_steps"] <= 0.05


def test_oscillation_is_not_a_trend(tmp_path):
    """AutoTune hunting around a target must read as no bias."""
    n = 96
    saves = [[(5 if s % 2 else -5)] * n for s in range(9)]
    paths = make_series(tmp_path, saves)
    res = lf.region_bias(paths, bands=(BAND,), window=48)[BAND]
    assert res["established"] == []
    w = next(w for w in res["windows"] if w["moves"])
    assert w["steps_moved"] == 8
    assert w["share"] == pytest.approx(0.5)


def test_region_bias_needs_two_tunes(tmp_path):
    paths = make_series(tmp_path, [[0] * 8])
    with pytest.raises(ValueError, match="at least two"):
        lf.region_bias(paths, bands=(BAND,))


# ---------------------------------------------------------------------------
# lineage
# ---------------------------------------------------------------------------

def test_family_keys_does_not_orphan_the_parent_save():
    """'automaprun' must group with automaprun2/3, not split off alone.

    A blind trailing-character strip turns 'automaprun' into 'automapru',
    which does not match what automaprun2/3 strip down to, so the parent tune
    silently leaves its own family and the trend quietly runs on fewer saves.
    """
    stems = ["automaprun", "automaprun2", "automaprun3"]
    keys = lf.family_keys(stems)
    assert len(set(keys.values())) == 1


def test_family_keys_groups_letter_and_letter_digit_suffixes():
    stems = ["latestandgreatest", "latestandgreatestB", "latestandgreatestG2"]
    keys = lf.family_keys(stems)
    assert len(set(keys.values())) == 1


def test_family_keys_keeps_unrelated_names_apart():
    stems = ["hopefullycooler", "advancedtiming", "dynopull"]
    keys = lf.family_keys(stems)
    assert len(set(keys.values())) == 3


def test_lineage_orders_by_mtime_and_skips_junk(tmp_path):
    import os
    band = lf._band(BAND)
    for n, name in enumerate(["famB.tbw", "famA.tbw", "fam.tbw"]):
        d = blank()
        write_cells(d, band, [n])
        (tmp_path / name).write_bytes(bytes(d))
        os.utime(tmp_path / name, (1000 + n * 100, 1000 + n * 100))
    (tmp_path / "._famB.tbw").write_bytes(b"\x00" * 4096)   # AppleDouble
    (tmp_path / "truncated.tbw").write_bytes(b"\x00" * 512)  # wrong size

    fams = lf.lineage(tmp_path)
    assert len(fams) == 1
    items = next(iter(fams.values()))
    assert [i["name"] for i in items] == ["famB.tbw", "famA.tbw", "fam.tbw"]
    assert all(i["base_map_id"] == "HXSSEDCAAN061617" for i in items)


def test_lineage_label_is_a_real_stem(tmp_path):
    band = lf._band(BAND)
    for name in ["automaprun.tbw", "automaprun2.tbw"]:
        d = blank()
        write_cells(d, band, [1])
        (tmp_path / name).write_bytes(bytes(d))
    fams = lf.lineage(tmp_path)
    key = next(iter(fams))
    assert key == "automaprun", "family label must be a stem you can type back"


# ---------------------------------------------------------------------------
# never write a .tbw
# ---------------------------------------------------------------------------

def test_module_never_opens_a_tbw_for_writing():
    """Structural check: no write mode anywhere in the source."""
    src = (SRC / "learned_feedback.py").read_text()
    for bad in ('"wb"', "'wb'", '"w+b"', "write_bytes("):
        assert bad not in src, f"learned_feedback must never write: found {bad}"
