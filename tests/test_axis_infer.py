"""Tests for axis_infer -- recovering table geometry from labelled pairs.

Pure arithmetic on synthetic index sets. No NAS, no tune files needed for the
geometry maths, which is the part that carries the claims.

The whole point of this module is to produce evidence a human will act on, so
the tests are mostly about it REFUSING to invent structure: noise must not
become a row length, and an under-reported record period must not split one
record into imaginary halves.
"""
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))

import axis_infer as ai  # noqa: E402


# ---------------------------------------------------------------------------
# runs
# ---------------------------------------------------------------------------

def test_runs_finds_contiguous_blocks():
    assert ai.runs([0, 1, 2, 7, 8, 20]) == [(0, 3), (7, 2), (20, 1)]


def test_runs_empty():
    assert ai.runs([]) == []


def test_runs_single():
    assert ai.runs([5]) == [(5, 1)]


# ---------------------------------------------------------------------------
# row length: R = gap + L - 1
# ---------------------------------------------------------------------------

def _grid_edit(row_len, n_rows, c0, c1):
    """Indices of an edit covering columns c0..c1 of every row."""
    return [r * row_len + c for r in range(n_rows) for c in range(c0, c1 + 1)]


@pytest.mark.parametrize("row_len,c0,c1", [
    (128, 40, 100),
    (128, 0, 63),
    (64, 10, 40),
    (192, 50, 150),
])
def test_row_length_is_recovered_exactly(row_len, c0, c1):
    idxs = _grid_edit(row_len, 5, c0, c1)
    got = ai.row_lengths(idxs)
    assert got, "a multi-row region edit must yield candidates"
    assert set(got) == {row_len}, f"expected {row_len}, got {set(got)}"


def test_row_length_ignores_short_runs():
    """Two adjacent single-cell changes imply 'row length 2' -- that is noise.

    Without the MIN_RUN floor, any scattered pair of changes manufactures a
    tiny row length that then outvotes the real one.
    """
    idxs = [0, 5, 10, 15, 20]          # all runs length 1
    assert ai.row_lengths(idxs) == []


def test_row_length_needs_two_runs():
    assert ai.row_lengths(list(range(0, 50))) == []


def test_full_table_edit_yields_no_geometry():
    """A global edit changes everything, so there are no gaps to measure.

    This is why a uniform -1 degree pair cannot reveal row length and a
    REGION edit is required.
    """
    assert ai.row_lengths(list(range(625))) == []


# ---------------------------------------------------------------------------
# lane period
# ---------------------------------------------------------------------------

def test_lane_period_uses_gcd_not_smallest_residue():
    """50, 54, 58, 62 has period 4, not 2.

    Every one of those shares a residue mod 2, so a 'smallest p with one
    shared residue' rule answers 2 -- splitting one 4-field record into two
    imaginary halves.
    """
    assert ai.lane_period([50, 54, 58, 62, 66]) == 4


def test_lane_period_detects_every_other_cell():
    assert ai.lane_period([0, 2, 4, 6, 8]) == 2


def test_lane_period_none_when_contiguous():
    assert ai.lane_period([0, 1, 2, 3, 4]) is None


def test_lane_period_none_when_irregular():
    assert ai.lane_period([0, 3, 4, 9, 11]) is None


def test_lane_period_needs_enough_points():
    assert ai.lane_period([0, 4]) is None


def test_lane_period_rejects_absurd_periods():
    """A period wider than any plausible record is not a record period."""
    assert ai.lane_period([0, 1000, 2000, 3000]) is None


# ---------------------------------------------------------------------------
# the module must not assert an axis map
# ---------------------------------------------------------------------------

def test_module_never_writes_band_axes():
    """BAND_AXES stays empty; a fabricated axis map mis-scopes safety checks.

    Checks for an ASSIGNMENT, not a mention -- the docstring legitimately
    explains why it refuses to write one, and a substring test on the whole
    file just flags its own documentation.
    """
    import ast
    tree = ast.parse((SRC / "axis_infer.py").read_text())
    for node in ast.walk(tree):
        targets = []
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
            targets = [node.target]
        for t in targets:
            name = getattr(t, "id", None) or getattr(
                getattr(t, "value", None), "id", None)
            assert name != "BAND_AXES", \
                "axis_infer must not assign BAND_AXES -- geometry only"


def test_dyno_band_axes_is_still_empty():
    """Guard the existing invariant from the other side."""
    import dyno_bridge
    assert not dyno_bridge.BAND_AXES, \
        "BAND_AXES must stay empty until a TMax ground-truth read confirms it"
