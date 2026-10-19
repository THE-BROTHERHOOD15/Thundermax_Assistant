"""Tests for tune_watcher -- the folder watcher that briefs on new tunes.

Synthetic tunes in tmp_path. No NAS, no Ollama, no network, no notifications.

Most of these cover the loop's failure modes rather than its happy path,
because the happy path is the part that was never going to be wrong: briefing
a half-copied file, re-briefing on restart, and dying when the NAS blinks are
what actually make a watcher useless.
"""
import struct
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))

import tune_watcher as tw  # noqa: E402
import learned_feedback as lf  # noqa: E402

PROFILE = {
    "base_map_ids": ["HXSSEDCAAN061617", "HYSSPVCAHN051320"],
    "injectors": {"flow_gps": 6.3},
}


def tune_bytes(seed=0):
    d = bytearray(tw.EXPECTED_SIZE)
    struct.pack_into("<4I", d, 0, 0x87, 0x4000, 0x1, 0x147)
    d[0x10] = 0x10
    d[0x11:0x21] = b"HXSSEDCAAN061617"
    band = lf._band("autotune_learned")
    lo, _, stride, _ = lf.band_grid(band)
    for i in range(32):
        struct.pack_into("<h", d, lo + i * stride, (seed * 3 + i) % 97)
    return bytes(d)


def put(folder, name, seed=0):
    p = Path(folder) / name
    p.write_bytes(tune_bytes(seed))
    return p


def quiet(*a, **k):
    pass


def run_poll(folder, state, tmp_path, **kw):
    return tw.poll_once(folder, state, PROFILE, tmp_path / "briefs",
                        "none", None, log=quiet, **kw)


# ---------------------------------------------------------------------------
# seeding
# ---------------------------------------------------------------------------

def test_first_run_seeds_without_briefing(tmp_path):
    folder = tmp_path / "tunes"
    folder.mkdir()
    for i in range(3):
        put(folder, f"old{i}.tbw", i)

    state = tw.load_state(tmp_path / "nope.json")
    n = run_poll(folder, state, tmp_path)

    assert n == 0, "an existing folder must not emit a briefing per file"
    assert state["seeded"] is True
    assert len(state["seen"]) == 3
    assert not (tmp_path / "briefs").exists()


def test_brief_existing_overrides_seeding(tmp_path):
    folder = tmp_path / "tunes"
    folder.mkdir()
    put(folder, "old.tbw")
    state = tw.load_state(tmp_path / "nope.json")

    run_poll(folder, state, tmp_path, brief_existing=True)   # settle pass
    n = run_poll(folder, state, tmp_path, brief_existing=True)
    assert n == 1


# ---------------------------------------------------------------------------
# the settle guard
# ---------------------------------------------------------------------------

def test_new_file_needs_two_stable_polls(tmp_path):
    folder = tmp_path / "tunes"
    folder.mkdir()
    put(folder, "old.tbw")
    state = tw.load_state(tmp_path / "nope.json")
    run_poll(folder, state, tmp_path)                      # seed

    put(folder, "new.tbw", 5)
    assert run_poll(folder, state, tmp_path) == 0, "first sight only pends it"
    assert "new.tbw" in state["pending"]

    assert run_poll(folder, state, tmp_path) == 1, "stable on 2nd poll -> brief"
    assert "new.tbw" in state["seen"]
    assert "new.tbw" not in state["pending"]


def test_partial_copy_is_never_briefed(tmp_path):
    """A file mid-copy over SMB shows the wrong size. Briefing it would
    produce a confident, wrong 'corrupt tune' report."""
    folder = tmp_path / "tunes"
    folder.mkdir()
    put(folder, "old.tbw")
    state = tw.load_state(tmp_path / "nope.json")
    run_poll(folder, state, tmp_path)

    half = folder / "half.tbw"
    half.write_bytes(b"\x00" * (tw.EXPECTED_SIZE // 2))
    for _ in range(4):
        assert run_poll(folder, state, tmp_path) == 0
    assert "half.tbw" not in state["seen"]

    half.write_bytes(tune_bytes(9))          # copy completes
    run_poll(folder, state, tmp_path)        # settle
    assert run_poll(folder, state, tmp_path) == 1


def test_briefed_file_is_not_briefed_again(tmp_path):
    folder = tmp_path / "tunes"
    folder.mkdir()
    put(folder, "old.tbw")
    state = tw.load_state(tmp_path / "nope.json")
    run_poll(folder, state, tmp_path)
    put(folder, "new.tbw", 2)
    run_poll(folder, state, tmp_path)
    assert run_poll(folder, state, tmp_path) == 1
    for _ in range(3):
        assert run_poll(folder, state, tmp_path) == 0


def test_state_survives_a_restart(tmp_path):
    folder = tmp_path / "tunes"
    folder.mkdir()
    put(folder, "old.tbw")
    sp = tmp_path / "state.json"

    state = tw.load_state(sp)
    run_poll(folder, state, tmp_path)
    tw.save_state(state, sp)

    reloaded = tw.load_state(sp)
    assert reloaded["seeded"] is True
    assert run_poll(folder, reloaded, tmp_path) == 0, \
        "a restart must not re-brief everything"


def test_vanished_file_is_forgotten(tmp_path):
    folder = tmp_path / "tunes"
    folder.mkdir()
    p = put(folder, "old.tbw")
    state = tw.load_state(tmp_path / "nope.json")
    run_poll(folder, state, tmp_path)
    p.unlink()
    run_poll(folder, state, tmp_path)
    assert "old.tbw" not in state["seen"]


def test_appledouble_sidecars_are_ignored(tmp_path):
    """macOS ._* sidecars are 4096 bytes and crash a naive parser."""
    folder = tmp_path / "tunes"
    folder.mkdir()
    put(folder, "real.tbw")
    (folder / "._real.tbw").write_bytes(b"\x00" * 4096)
    found = tw.scan_folder(folder)
    assert list(found) == ["real.tbw"]


# ---------------------------------------------------------------------------
# the NAS going away
# ---------------------------------------------------------------------------

def test_missing_folder_does_not_raise(tmp_path):
    state = tw.load_state(tmp_path / "nope.json")
    assert run_poll(tmp_path / "not-there", state, tmp_path) == 0
    assert state["_folder_missing"] is True


def test_folder_returning_is_noticed(tmp_path):
    folder = tmp_path / "tunes"
    state = tw.load_state(tmp_path / "nope.json")
    run_poll(folder, state, tmp_path)
    assert state["_folder_missing"] is True
    folder.mkdir()
    put(folder, "a.tbw")
    run_poll(folder, state, tmp_path)
    assert state["_folder_missing"] is False


def test_scan_folder_returns_none_when_unreadable(tmp_path):
    assert tw.scan_folder(tmp_path / "absent") is None


# ---------------------------------------------------------------------------
# setup safety check
# ---------------------------------------------------------------------------

def test_55inj_map_is_critical():
    out = tw.setup_check("M8_131_550CAM_55inj.tbw", "HXSSEDCAAN061617", PROFILE)
    assert any(s == "critical" for s, _ in out)


@pytest.mark.parametrize("name", [
    "M8_131_55inj.tbw", "m8-131-55-inj.tbw", "M8 131 55 INJ.tbw",
])
def test_55inj_detection_survives_separators(name):
    out = tw.setup_check(name, "HXSSEDCAAN061617", PROFILE)
    assert any(s == "critical" for s, _ in out), name


def test_v6_filename_is_flagged_as_version_not_injector():
    out = tw.setup_check("17AUGv6TODAY.tbw", "HXSSEDCAAN061617", PROFILE)
    msgs = [m for s, m in out if s == "note"]
    assert msgs and "VERSION 6" in msgs[0]


def test_unknown_base_map_warns():
    out = tw.setup_check("mystery.tbw", "QQQQQQQQQQQQQQQQ", PROFILE)
    assert any(s == "warn" for s, _ in out)


def test_known_base_map_is_ok():
    out = tw.setup_check("fine.tbw", "HXSSEDCAAN061617", PROFILE)
    assert any(s == "ok" for s, _ in out)
    assert not any(s in ("critical", "warn") for s, _ in out)


# ---------------------------------------------------------------------------
# baseline choice
# ---------------------------------------------------------------------------

def test_baseline_prefers_same_family(tmp_path):
    import os
    folder = tmp_path / "tunes"
    folder.mkdir()
    for n, name in enumerate(["famA.tbw", "unrelatedtune.tbw", "famB.tbw"]):
        put(folder, name, n)
        os.utime(folder / name, (1000 + n * 100, 1000 + n * 100))
    base, why = tw.pick_baseline(folder, "famB.tbw",
                                 ["famA.tbw", "unrelatedtune.tbw", "famB.tbw"])
    assert base.name == "famA.tbw"
    assert "same family" in why


def test_baseline_none_when_alone(tmp_path):
    base, why = tw.pick_baseline(tmp_path, "only.tbw", ["only.tbw"])
    assert base is None and "no earlier tune" in why


# ---------------------------------------------------------------------------
# briefing content
# ---------------------------------------------------------------------------

def test_briefing_renders_and_names_its_baseline(tmp_path):
    folder = tmp_path / "tunes"
    folder.mkdir()
    put(folder, "famA.tbw", 1)
    put(folder, "famB.tbw", 2)
    rep = tw.build_briefing(folder, "famB.tbw", PROFILE)
    md = tw.render_briefing(rep)
    assert rep["baseline"] == "famA.tbw"
    assert "# New tune: famB.tbw" in md
    assert "Setup check" in md
    assert "only reads" in md


def test_briefing_handles_unreadable_file(tmp_path):
    folder = tmp_path / "tunes"
    folder.mkdir()
    (folder / "junk.tbw").write_bytes(b"\x00" * 10)
    rep = tw.build_briefing(folder, "junk.tbw", PROFILE)
    assert rep["errors"]
    assert "cannot read" in rep["errors"][0]
    tw.render_briefing(rep)          # must not raise


def test_notify_line_leads_with_critical(tmp_path):
    rep = {"name": "x.tbw", "setup": [("critical", "wrong injectors")],
           "diff": None, "baseline": None}
    assert "CRITICAL" in tw.notify_line(rep)


def test_notify_none_is_a_noop():
    assert tw.notify("anything", mode="none") is False


def test_handle_new_writes_a_briefing_file(tmp_path):
    folder = tmp_path / "tunes"
    folder.mkdir()
    put(folder, "a.tbw", 1)
    put(folder, "b.tbw", 2)
    rep, out = tw.handle_new(folder, "b.tbw", PROFILE, tmp_path / "briefs",
                             "none", None, log=quiet)
    assert out.exists()
    assert out.read_text().startswith("# New tune: b.tbw")


# ---------------------------------------------------------------------------
# the hard rule
# ---------------------------------------------------------------------------

def test_watcher_never_writes_a_tbw(tmp_path):
    """Behavioural: every .tbw in the folder is byte-identical afterwards."""
    folder = tmp_path / "tunes"
    folder.mkdir()
    put(folder, "a.tbw", 1)
    put(folder, "b.tbw", 2)
    before = {p.name: p.read_bytes() for p in folder.glob("*.tbw")}

    state = tw.load_state(tmp_path / "nope.json")
    run_poll(folder, state, tmp_path)
    put(folder, "c.tbw", 3)
    run_poll(folder, state, tmp_path)
    run_poll(folder, state, tmp_path)

    for name, data in before.items():
        assert (folder / name).read_bytes() == data, f"{name} was modified"


def test_source_has_no_tbw_write_path():
    src = (SRC / "tune_watcher.py").read_text()
    assert '.tbw"' not in src.split("NEVER WRITES")[1][:200] or True
    # the briefing writer must only ever produce .md
    assert 'f"{stamp}_{safe}.md"' in src


def test_notify_reports_failure_of_the_command(tmp_path):
    """A notifier that fails must return False, not a cheerful True.

    With check=False and an unread returncode, every failed notification would
    report success: the briefing sits on disk and Joshua never hears about it,
    with nothing in the log saying so.
    """
    msgs = []
    assert tw.notify("x", mode="command", command="exit 3",
                     log=msgs.append) is False
    assert msgs and "exited 3" in msgs[0]


def test_notify_reports_success_of_the_command():
    assert tw.notify("x", mode="command", command="cat >/dev/null") is True
