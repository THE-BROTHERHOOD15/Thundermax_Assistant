"""Tests for doctor -- the stack self-check and healer.

The num_ctx lint carries the most weight here, because it exists to stop a
bug that has already been found FOUR separate times (chat path, hermes-rag,
lead enrichment, and the adversarial vetting reviewer). A lint that cannot
fail is worse than no lint: it reports "ok" forever and everyone stops
looking. So the first thing tested is that it actually catches a violation.
"""
import json
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))

import doctor  # noqa: E402

CALL = ('import json, urllib.request\n'
        'def go():\n'
        '    body = {"model": "m", "messages": [], "stream": True%s}\n'
        '    urllib.request.urlopen("http://x/api/chat",\n'
        '                           json.dumps(body).encode())\n')


def _repo(tmp_path, name, text):
    (tmp_path / "src").mkdir(exist_ok=True)
    (tmp_path / "src" / name).write_text(text)
    return tmp_path


# ---------------------------------------------------------------------------
# the lint
# ---------------------------------------------------------------------------

def test_lint_catches_a_missing_num_ctx(tmp_path):
    r = doctor.check_num_ctx(_repo(tmp_path, "bad.py", CALL % ""))
    assert r["status"] == doctor.FAIL
    assert "bad.py" in r["detail"]


def test_lint_passes_when_options_present(tmp_path):
    r = doctor.check_num_ctx(
        _repo(tmp_path, "good.py", CALL % ', "options": {"num_ctx": 16384}'))
    assert r["status"] == doctor.OK


def test_lint_ignores_files_that_never_call_ollama(tmp_path):
    r = doctor.check_num_ctx(
        _repo(tmp_path, "unrelated.py", "x = {'messages': [1, 2]}\n"))
    assert r["status"] == doctor.OK, "only Ollama call sites are in scope"


def test_lint_flags_a_prompt_style_body_too(tmp_path):
    text = ('import json, urllib.request\n'
            'b = {"model": "m", "prompt": "hi"}\n'
            'urllib.request.urlopen("http://x/api/generate", b)\n')
    r = doctor.check_num_ctx(_repo(tmp_path, "gen.py", text))
    assert r["status"] == doctor.FAIL


def test_lint_reports_unparseable_source(tmp_path):
    r = doctor.check_num_ctx(_repo(tmp_path, "broken.py", "def (\n"))
    assert r["status"] == doctor.FAIL
    assert "unparseable" in r["detail"]


def test_the_real_repo_passes_its_own_lint():
    """Regression guard: nobody reintroduces the truncation bug."""
    assert doctor.check_num_ctx()["status"] == doctor.OK


# ---------------------------------------------------------------------------
# invariants
# ---------------------------------------------------------------------------

def test_band_axes_must_stay_empty():
    assert doctor.check_band_axes()["status"] == doctor.OK


def test_bands_do_not_partially_overlap():
    assert doctor.check_bands_disjoint()["status"] == doctor.OK


def test_corpus_is_retrievable():
    """Every note in docs/corpus must match DOC_GLOB or it is invisible."""
    assert doctor.check_corpus_retrievable()["status"] == doctor.OK


def test_dyno_selftest_still_passes():
    assert doctor.check_dyno_selftest()["status"] == doctor.OK


def test_missing_tune_folder_warns_not_crashes(tmp_path):
    r = doctor.check_tunes(tmp_path / "absent")
    assert r["status"] == doctor.WARN
    assert "is_dir" in r["detail"]


def test_wrong_size_tune_is_a_failure(tmp_path):
    f = tmp_path / "t.tbw"
    f.write_bytes(b"\x00" * 100)
    r = doctor.check_tunes(tmp_path)
    assert r["status"] == doctor.FAIL
    assert "t.tbw" in r["detail"]


def test_appledouble_sidecar_is_excluded_not_flagged(tmp_path):
    (tmp_path / "real.tbw").write_bytes(b"\x00" * doctor.EXPECTED_SIZE)
    (tmp_path / "._real.tbw").write_bytes(b"\x00" * 4096)
    r = doctor.check_tunes(tmp_path)
    assert r["status"] == doctor.OK
    assert "1 AppleDouble" in r["detail"]


# ---------------------------------------------------------------------------
# heal
# ---------------------------------------------------------------------------

def test_heal_resets_corrupt_watch_state(tmp_path, monkeypatch):
    monkeypatch.setattr(doctor, "REPO", tmp_path)
    (tmp_path / "data").mkdir()
    bad = tmp_path / "data" / "watch_state.json"
    bad.write_text("{not json")
    done = doctor.heal(log=lambda *_: None)
    assert any("watch_state" in d for d in done)
    state = json.loads(bad.read_text())
    assert state["seeded"] is False, "must re-seed quietly, not re-brief 153 tunes"


def test_heal_creates_missing_dirs(tmp_path, monkeypatch):
    monkeypatch.setattr(doctor, "REPO", tmp_path)
    doctor.heal(log=lambda *_: None)
    assert (tmp_path / "data").is_dir()
    assert (tmp_path / "reports" / "briefings").is_dir()


def test_heal_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setattr(doctor, "REPO", tmp_path)
    doctor.heal(log=lambda *_: None)
    assert doctor.heal(log=lambda *_: None) == [], "second run must be a no-op"


def test_heal_never_touches_a_tbw(tmp_path, monkeypatch):
    monkeypatch.setattr(doctor, "REPO", tmp_path)
    t = tmp_path / "keep.tbw"
    t.write_bytes(b"\xAB" * 32)
    doctor.heal(log=lambda *_: None)
    assert t.read_bytes() == b"\xAB" * 32
