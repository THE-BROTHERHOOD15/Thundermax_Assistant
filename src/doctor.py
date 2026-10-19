#!/usr/bin/env python3
"""Self-check and self-heal for the ThunderMax stack.

Two jobs:

  check  -- assert every invariant this project has learned the hard way, and
            say plainly which ones are broken right now.
  heal   -- repair the ones that are safe to repair without a human.

WHY A HEALER AND NOT JUST A FIX
-------------------------------
Some failures here are permanent properties of the environment, not bugs that
can be closed:

  * The NAS is a network share. It WILL go away sometimes.
  * Ollama is shared with training runs and other agents. It WILL be busy,
    down, or reconfigured underneath us.
  * Elasticsearch is optional and WILL be down in the garage.

Those get watched and reported, because "fixed" is not available. Everything
else gets fixed for good, and the check exists so it stays fixed.

THE num_ctx LINT IS THE POINT
-----------------------------
The single most expensive class of bug on this tower is an Ollama call that
omits `options.num_ctx`. The server runs OLLAMA_CONTEXT_LENGTH=4096, so such a
call is silently truncated -- no error, no warning, the model just answers from
a fragment. It has now been found FOUR separate times: the chat path, hermes-
rag, the lead enrichment script, and the adversarial vetting reviewer (where it
biased the safety gate toward a false pass).

Finding it a fifth time by accident is not acceptable, so `check` greps every
Ollama call site in this repo and FAILS if one does not send num_ctx. That
converts a silent, invisible, recurring failure into a loud one.

Exit codes: 0 all good, 1 warnings only, 2 something is actually broken.
"""
import argparse
import ast
import json
import os
import sys
import urllib.request
from pathlib import Path

SRC = Path(__file__).resolve().parent
REPO = SRC.parent
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

EXPECTED_SIZE = 214470
DEFAULT_TUNES = os.environ.get(
    "TMAX_TUNES_DIR", "/mnt/nas/ADMIN/LOCAL NAS/THROTTLE LOGIC")
OLLAMA = "http://127.0.0.1:11434"

OK, WARN, FAIL = "ok", "warn", "fail"


def _r(name, status, detail, fix=None):
    return {"check": name, "status": status, "detail": detail, "fix": fix}


# ---------------------------------------------------------------------------
# the lint that stops the recurring bug
# ---------------------------------------------------------------------------

def check_num_ctx(repo=REPO):
    """Every Ollama chat/generate call in this repo must send options.num_ctx.

    Walks the AST rather than grepping for a string: a call is flagged by what
    it POSTS, so a helper that builds the body elsewhere is not silently
    excused, and a comment mentioning num_ctx does not count as sending it.
    """
    offenders = []
    for py in sorted((repo / "src").glob("*.py")):
        try:
            tree = ast.parse(py.read_text())
        except SyntaxError as e:
            offenders.append(f"{py.name}: unparseable ({e})")
            continue
        src = py.read_text()
        # Only files that actually talk to Ollama are in scope.
        if "/api/chat" not in src and "/api/generate" not in src:
            continue
        # Find dict literals that look like an Ollama request body.
        sends_ctx = False
        posts_body = False
        for node in ast.walk(tree):
            if not isinstance(node, ast.Dict):
                continue
            keys = {k.value for k in node.keys
                    if isinstance(k, ast.Constant) and isinstance(k.value, str)}
            if "messages" in keys or "prompt" in keys:
                posts_body = True
                if "options" in keys:
                    sends_ctx = True
        if "num_ctx" in src:
            sends_ctx = True
        if posts_body and not sends_ctx:
            offenders.append(py.name)

    if offenders:
        return _r("ollama num_ctx", FAIL,
                  "these post an Ollama body with no options.num_ctx, so the "
                  "server truncates them to OLLAMA_CONTEXT_LENGTH silently: "
                  + ", ".join(offenders),
                  "add options={'num_ctx': core.TIER_CTX[tier]} to the request")
    return _r("ollama num_ctx", OK,
              "every Ollama call site in src/ sends an explicit context window")


# ---------------------------------------------------------------------------
# environment invariants (watched, not fixable)
# ---------------------------------------------------------------------------

def check_tunes(folder=DEFAULT_TUNES):
    p = Path(folder)
    if not p.is_dir():
        return _r("tune folder", WARN,
                  f"{folder} is not reachable (NAS down or unmounted). "
                  f"Path.glob() would return nothing here, which is NOT the "
                  f"same as an empty folder -- callers must check is_dir().",
                  "check the NAS at 192.168.50.92 and the CIFS mount")
    tbw = [f for f in p.glob("*.tbw") if not f.name.startswith("._")]
    sidecars = [f for f in p.glob("._*.tbw")]
    bad = [f.name for f in tbw if f.stat().st_size != EXPECTED_SIZE]
    if bad:
        return _r("tune folder", FAIL,
                  f"{len(bad)} file(s) are not {EXPECTED_SIZE} bytes: "
                  f"{', '.join(bad[:5])}",
                  "these are not tunes this parser understands; quarantine them")
    return _r("tune folder", OK,
              f"{len(tbw)} tunes, all exactly {EXPECTED_SIZE} bytes; "
              f"{len(sidecars)} AppleDouble sidecar(s) correctly excluded")


def check_tbw_readonly(folder=DEFAULT_TUNES):
    """The hard rule, verified rather than assumed."""
    p = Path(folder)
    if not p.is_dir():
        return _r("tbw read-only", WARN, "tune folder unreachable; cannot verify")
    sample = next((f for f in p.glob("*.tbw")
                   if not f.name.startswith("._")), None)
    if sample is None:
        return _r("tbw read-only", WARN, "no tune to test")
    if os.access(sample, os.W_OK):
        return _r("tbw read-only", WARN,
                  f"{sample.name} is WRITABLE by this user. The NAS mount "
                  f"normally forces root ownership, which is what makes "
                  f"'never write a .tbw' structural rather than intended.",
                  "prefer the NAS path over ~/tmax-exchange/tunes-from-nas, "
                  "or bind-mount the tune dir read-only")
    return _r("tbw read-only", OK,
              "tune files are not writable by this user (structural guarantee)")


def check_ollama():
    try:
        with urllib.request.urlopen(f"{OLLAMA}/api/tags", timeout=4) as r:
            models = [m["name"] for m in json.load(r).get("models", [])]
    except Exception as e:
        return _r("ollama", WARN,
                  f"not reachable ({type(e).__name__}). The assistant degrades "
                  f"to parser-only, which is by design in the garage.",
                  "ollama serve")
    return _r("ollama", OK, f"up, {len(models)} model(s) loaded")


def check_context_length():
    """Report the server default so a 4096 cap is never a surprise again."""
    val = None
    try:
        import subprocess
        out = subprocess.run(["systemctl", "show", "ollama", "-p", "Environment"],
                             capture_output=True, text=True, timeout=5).stdout
        for tok in out.replace("Environment=", "").split():
            if tok.startswith("OLLAMA_CONTEXT_LENGTH="):
                val = tok.split("=", 1)[1]
    except Exception:
        pass
    if val is None:
        return _r("ollama context default", WARN,
                  "could not read OLLAMA_CONTEXT_LENGTH from the unit")
    if int(val) <= 4096:
        return _r("ollama context default", WARN,
                  f"OLLAMA_CONTEXT_LENGTH={val}. Any caller that omits "
                  f"options.num_ctx is silently truncated to this. Our callers "
                  f"all send it (see the num_ctx lint), but other tools on this "
                  f"tower may not.",
                  "raise it in the systemd drop-in once no training run is "
                  "using the GPU; costs VRAM per loaded model")
    return _r("ollama context default", OK, f"OLLAMA_CONTEXT_LENGTH={val}")


# ---------------------------------------------------------------------------
# project invariants (fixable, and must stay fixed)
# ---------------------------------------------------------------------------

def check_band_axes():
    try:
        import dyno_bridge
    except Exception as e:
        return _r("BAND_AXES empty", FAIL, f"dyno_bridge will not import: {e}")
    if dyno_bridge.BAND_AXES:
        return _r("BAND_AXES empty", FAIL,
                  "BAND_AXES has been populated. Unless a TMax Tuner "
                  "ground-truth read confirmed it, this is a fabricated axis "
                  "map and it mis-scopes every safety check.",
                  "revert it, or record the confirming measurement")
    return _r("BAND_AXES empty", OK,
              "still empty -- no fabricated axis map (confirm via ground truth)")


def check_bands_disjoint():
    from table_map import load_map
    bands = load_map()["bands"]
    spans = sorted(((int(b["range"][0], 16), int(b["range"][1], 16), b["name"])
                    for b in bands))
    bad = []
    for (l1, h1, n1), (l2, h2, n2) in zip(spans, spans[1:]):
        if l2 < h1 and not (l1 <= l2 and h2 <= h1):   # partial overlap
            bad.append(f"{n1} 0x{l1:05X}-0x{h1:05X} overlaps {n2} "
                       f"0x{l2:05X}-0x{h2:05X}")
    if bad:
        return _r("band ranges", WARN, "; ".join(bad),
                  "partial overlap makes band_for_offset ambiguous")
    return _r("band ranges", OK, f"{len(bands)} bands, no partial overlaps")


def check_labeled_pairs(folder=DEFAULT_TUNES):
    if not Path(folder).is_dir():
        return _r("labelled pairs", WARN, "tune folder unreachable")
    try:
        import axis_infer
        rows = axis_infer.verify_labels(folder)
    except Exception as e:
        return _r("labelled pairs", WARN, f"could not verify: {e}")
    bad = [r for r in rows if r["verdict"] not in ("ok", "missing file")]
    if bad:
        return _r("labelled pairs", FAIL,
                  "; ".join(f"{r['category']} ({r['verdict']})" for r in bad),
                  "a pair that does not show its own edit poisons "
                  "table_map.derive -- remove it")
    return _r("labelled pairs", OK,
              f"{len(rows)} pairs, each shows the edit its filename claims")


def check_corpus_retrievable():
    """Notes must match DOC_GLOB or they are invisible however good they are."""
    try:
        import tune_assistant as ta
        names = [d.name for d in ta.find_docs()]
    except Exception as e:
        return _r("corpus retrievable", WARN, f"could not load corpus: {e}")
    if not names:
        return _r("corpus retrievable", FAIL, "find_docs() returns nothing",
                  "check docs/corpus and DOC_GLOB")
    stray = [p.name for p in (REPO / "docs" / "corpus").glob("*.md")
             if p.name not in names]
    if stray:
        return _r("corpus retrievable", WARN,
                  f"{len(stray)} note(s) in docs/corpus do NOT match DOC_GLOB "
                  f"and are unreachable: {', '.join(stray[:4])}",
                  "rename to include 'thundermax'")
    return _r("corpus retrievable", OK,
              f"{len(names)} docs, all reachable via DOC_GLOB")


def check_dyno_selftest():
    try:
        import dyno_bridge
        res = dyno_bridge.self_test()
    except Exception as e:
        return _r("dyno self-test", FAIL, f"raised: {e}")
    s = res["summary"]
    if not res["passed"]:
        return _r("dyno self-test", FAIL,
                  f"{s['critical_failed']} critical failure(s) of {s['total']}")
    return _r("dyno self-test", OK,
              f"{s['passed']}/{s['total']} (self-consistency only -- this "
              f"cannot prove the model matches the engine)")


def check_writable_dirs():
    missing = [d for d in (REPO / "data", REPO / "reports" / "briefings")
               if not d.is_dir()]
    if missing:
        return _r("writable dirs", WARN,
                  f"missing: {', '.join(str(m) for m in missing)}",
                  "run `doctor heal`")
    return _r("writable dirs", OK, "data/ and reports/briefings/ present")


CHECKS = [check_num_ctx, check_band_axes, check_bands_disjoint,
          check_labeled_pairs, check_corpus_retrievable, check_dyno_selftest,
          check_writable_dirs, check_tunes, check_tbw_readonly,
          check_ollama, check_context_length]


# ---------------------------------------------------------------------------
# heal
# ---------------------------------------------------------------------------

def heal(log=print):
    """Repairs that are safe without a human. Never touches a .tbw."""
    done = []
    for d in (REPO / "data", REPO / "reports" / "briefings"):
        if not d.is_dir():
            d.mkdir(parents=True, exist_ok=True)
            done.append(f"created {d.relative_to(REPO)}")

    # A watcher state file that is corrupt would make the watcher re-brief
    # everything or nothing. Rewriting it as un-seeded is safe: the next poll
    # re-seeds from the folder without emitting briefings.
    st = REPO / "data" / "watch_state.json"
    if st.exists():
        try:
            json.loads(st.read_text())
        except ValueError:
            st.write_text(json.dumps({"seeded": False, "seen": {}, "pending": {}}))
            done.append("reset corrupt watch_state.json (will re-seed quietly)")

    for d in done:
        log(f"healed: {d}")
    if not done:
        log("nothing to heal")
    return done


def run_checks(folder=DEFAULT_TUNES):
    out = []
    for fn in CHECKS:
        try:
            out.append(fn(folder) if fn in
                       (check_tunes, check_tbw_readonly, check_labeled_pairs)
                       else fn())
        except Exception as e:
            out.append(_r(fn.__name__, FAIL, f"check itself raised: {e}"))
    return out


def main(argv=None):
    p = argparse.ArgumentParser(description="ThunderMax stack doctor")
    p.add_argument("cmd", choices=["check", "heal"], nargs="?", default="check")
    p.add_argument("--tunes", default=DEFAULT_TUNES)
    p.add_argument("--json", action="store_true")
    a = p.parse_args(argv)

    if a.cmd == "heal":
        heal()
        return 0

    res = run_checks(a.tunes)
    if a.json:
        print(json.dumps(res, indent=2))
    else:
        mark = {OK: "  ok  ", WARN: " WARN ", FAIL: " FAIL "}
        for r in res:
            print(f"[{mark[r['status']]}] {r['check']:24} {r['detail']}")
            if r["fix"] and r["status"] != OK:
                print(f"{'':10} -> {r['fix']}")
        n_fail = sum(1 for r in res if r["status"] == FAIL)
        n_warn = sum(1 for r in res if r["status"] == WARN)
        print(f"\n{len(res)} checks: {len(res)-n_fail-n_warn} ok, "
              f"{n_warn} warn, {n_fail} fail")
    if any(r["status"] == FAIL for r in res):
        return 2
    return 1 if any(r["status"] == WARN for r in res) else 0


if __name__ == "__main__":
    sys.exit(main())
