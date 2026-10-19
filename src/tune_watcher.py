#!/usr/bin/env python3
"""Watch the tune folder and brief Joshua when a new .tbw appears.

WHY THIS EXISTS
---------------
Everything else in this project is reactive: it answers when asked. This is the
piece that speaks first. You save a tune in TMax Tuner, it lands on the NAS,
and within a poll cycle you get a briefing that already did the diff, the
classification, the setup safety check and the AutoTune-feedback read -- before
you have decided whether to flash it.

DESIGN CONSTRAINTS, ALL LEARNED THE HARD WAY
--------------------------------------------
* POLLING, NOT INOTIFY. The tunes live on a CIFS/SMB mount. inotify does not
  fire reliably for changes made by another host on a network share, so a
  watcher built on it looks like it is working and silently sees nothing.

* SETTLE BEFORE READING. A file being copied over SMB is visible at the wrong
  size for a while. Briefing on a half-written file produces a confident,
  wrong report about a "corrupt tune". A candidate must hold the exact
  expected size AND an unchanged (size, mtime) across two consecutive polls
  before it is touched.

* THE FIRST RUN SEEDS, IT DOES NOT BRIEF. Pointed at the NAS folder for the
  first time it would otherwise emit 153 briefings. First sight of a folder
  records state and stays quiet; `--brief-existing` overrides.

* NAS DOWN IS NORMAL, NOT FATAL. `tmax-web.service` deliberately does not
  depend on the NAS so it comes up in the garage when the NAS is off. This
  follows the same rule: an unreachable folder logs once and keeps polling.

* NO LLM ON THE DEFAULT PATH. The briefing is pure parser + table map +
  guardrails + dyno -- deterministic, and it still works when Ollama is down
  or its GPU is busy with something else. `--llm` opts into a narrated summary.

NEVER WRITES `.tbw`. It reads them and writes markdown briefings elsewhere.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

SRC = Path(__file__).resolve().parent
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import dyno_bridge                      # noqa: E402
import learned_feedback as lf           # noqa: E402
import table_map                        # noqa: E402
from thundermax_parser import TbwFile    # noqa: E402

REPO = SRC.parent
EXPECTED_SIZE = 214470
DEFAULT_FOLDER = "/mnt/nas/ADMIN/LOCAL NAS/THROTTLE LOGIC"
STATE_PATH = REPO / "data" / "watch_state.json"
BRIEF_DIR = REPO / "reports" / "briefings"
PROFILE_PATH = SRC / "bike_profile.json"
DEFAULT_INTERVAL = 60


# ---------------------------------------------------------------------------
# state
# ---------------------------------------------------------------------------

def load_state(path=STATE_PATH):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return {"seeded": False, "seen": {}, "pending": {}}


def save_state(state, path=STATE_PATH):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True))
    tmp.replace(path)                    # atomic: a killed watcher never
                                         # leaves a half-written state file


def scan_folder(folder):
    """{name: {size, mtime}} for plausible tune files, or None if the folder
    is not readable right now.

    THE is_dir() CHECK IS LOAD-BEARING. `Path.glob()` on a directory that
    does not exist does NOT raise -- it yields nothing. Without this check an
    unmounted NAS returns `{}`, which the caller cannot distinguish from "the
    folder is there and empty". It would clear `seen` as though all 153 tunes
    had been deleted, and then brief every one of them again the moment the
    mount came back. Distinguishing "gone" from "empty" is the whole contract
    of this function.
    """
    folder = Path(folder)
    try:
        if not folder.is_dir():
            return None
        entries = list(folder.glob("*.tbw"))
    except OSError:
        return None
    out = {}
    for p in entries:
        if p.name.startswith("._"):      # macOS AppleDouble sidecar, 4096 B
            continue                     # -- crashes a naive parser
        try:
            st = p.stat()
        except OSError:
            continue
        out[p.name] = {"size": st.st_size, "mtime": st.st_mtime}
    return out


# ---------------------------------------------------------------------------
# safety: does this tune even belong on this bike?
# ---------------------------------------------------------------------------

def load_profile(path=PROFILE_PATH):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return {}


def setup_check(name, base_map_id, profile):
    """Hardware-mismatch findings for a newly seen tune.

    This is the highest-value thing the briefing does, because it is the one
    failure that strands the bike rather than merely riding badly. The rules
    come from bike_profile.json, which records that the current hardware is
    6.3 injectors on base map HXSSEDCAAN061617, and that two specific traps
    have already nearly bitten:

      * a `*55inj*` map is for 5.5 g/s injectors and must never be flashed
        onto the 6.3s;
      * `...v6...` in a filename means VERSION 6, not 6.3 injectors -- that
        file is SE8-517 history from a different build.

    Findings are advisory: this watcher never blocks anything, it cannot, it
    only reads. But an unrecognised base map is worth saying out loud before
    a flash rather than after.
    """
    findings = []
    low = name.lower()
    ids = profile.get("base_map_ids") or []
    inj = (profile.get("injectors") or {}).get("flow_gps")

    if "55inj" in low.replace("_", "").replace("-", "").replace(" ", ""):
        findings.append((
            "critical",
            f"filename marks this a 5.5 g/s injector map, but this bike runs "
            f"{inj} g/s injectors. Per bike_profile.json: never flash a "
            f"*55inj* map onto these."))

    if "v6" in low and "6.3" not in low:
        findings.append((
            "note",
            "filename contains 'v6'. In this shop's history that means "
            "VERSION 6, not 6.3 injectors -- the v6 file is SE8-517 lineage "
            "from a different build. Confirm before flashing."))

    if base_map_id and ids:
        if base_map_id in ids:
            findings.append((
                "ok", f"base map {base_map_id} is a known map for this setup."))
        else:
            findings.append((
                "warn",
                f"base map {base_map_id} is NOT in this bike's known list "
                f"({', '.join(ids)}). Either it is a new map you meant to "
                f"try, or this tune was built for a different bike."))
    return findings


# ---------------------------------------------------------------------------
# choosing what to diff against
# ---------------------------------------------------------------------------

def pick_baseline(folder, new_name, known_names):
    """The most sensible previous tune to diff the new one against.

    Prefers the newest earlier save in the SAME family (a G2 is best explained
    by G, not by an unrelated tune), and falls back to the most recently
    modified known tune. Returns (path, reason) or (None, reason).
    """
    folder = Path(folder)
    others = [n for n in known_names if n != new_name]
    if not others:
        return None, "no earlier tune on record to compare against"

    stems = [Path(n).stem for n in others + [new_name]]
    keys = lf.family_keys(stems)
    mine = keys.get(Path(new_name).stem)

    def mtime(n):
        try:
            return (folder / n).stat().st_mtime
        except OSError:
            return 0

    fam = [n for n in others if keys.get(Path(n).stem) == mine]
    if fam:
        pick = max(fam, key=mtime)
        return folder / pick, f"newest earlier save in the same family ({mine})"

    pick = max(others, key=mtime)
    return folder / pick, ("no same-family predecessor; using the most "
                           "recently modified tune on record")


# ---------------------------------------------------------------------------
# the briefing
# ---------------------------------------------------------------------------

def build_briefing(folder, name, profile=None, family_feedback=True):
    """Everything deterministic we can say about a newly appeared tune."""
    folder = Path(folder)
    path = folder / name
    profile = profile if profile is not None else load_profile()
    now = datetime.now(timezone.utc).astimezone()

    rep = {"name": name, "path": str(path), "at": now.isoformat(),
           "errors": [], "setup": [], "diff": None, "dyno": None,
           "feedback": None, "baseline": None, "baseline_reason": None}

    try:
        tbw = TbwFile(path)
    except Exception as e:                       # unreadable / vanished / junk
        rep["errors"].append(f"cannot read as a TBW file: {e}")
        return rep

    rep["base_map_id"] = tbw.base_map_id
    rep["size"] = len(tbw.data)
    rep["size_ok"] = tbw.size_ok
    rep["id_ok"] = tbw.id_ok
    rep["setup"] = setup_check(name, tbw.base_map_id, profile)

    known = [p.name for p in folder.glob("*.tbw")
             if not p.name.startswith("._")]
    base, why = pick_baseline(folder, name, known)
    rep["baseline_reason"] = why
    if base is None:
        return rep
    rep["baseline"] = base.name

    try:
        rows = table_map.classify_diff(str(base), str(path))
        rep["diff"] = {"regions": rows, "by_category": {
            k: {"regions": v["regions"], "bytes": v["bytes"],
                "confidences": sorted(c for c in v["confidences"] if c != "-")}
            for k, v in table_map.summarize(rows).items()}}
    except Exception as e:
        rep["errors"].append(f"diff failed: {e}")

    try:
        d = dyno_bridge.changes_from_diff(str(base), str(path))
        rep["dyno"] = {
            "usable": d["diff_summary"]["dyno_usable_changes"],
            "unmapped": len(d["unmapped"]),
            "excluded": len(d["excluded"]),
            "confidence": d["confidence"],
            "confidence_reason": d["confidence_reason"],
            "directional_only": d.get("directional_only"),
        }
    except Exception as e:
        rep["errors"].append(f"dyno read failed: {e}")

    # AutoTune feedback needs a family with enough saves to mean anything.
    if family_feedback:
        try:
            fams = lf.lineage(folder)
            stem = Path(name).stem.lower()
            fam = next((v for v in fams.values()
                        if any(i["path"].stem.lower() == stem for i in v)), None)
            if fam and len(fam) >= lf.MIN_TREND_STEPS + 1:
                rb = lf.region_bias([i["path"] for i in fam])
                rep["feedback"] = {
                    "saves": len(fam),
                    "bands": {n: {"established": len(r["established"]),
                                  "suggestive": len(r["suggestive"]),
                                  "top": (r["established"] + r["suggestive"])[:3]}
                              for n, r in rb.items()},
                }
            elif fam:
                rep["feedback"] = {
                    "saves": len(fam),
                    "too_short": (f"family has {len(fam)} saves; needs at least "
                                  f"{lf.MIN_TREND_STEPS + 1} before a direction "
                                  f"can be called a trend")}
        except Exception as e:
            rep["errors"].append(f"learned-feedback read failed: {e}")

    return rep


def render_briefing(rep):
    """Markdown. Also the notification body, trimmed."""
    L = []
    a = L.append
    a(f"# New tune: {rep['name']}")
    a("")
    a(f"- **Seen:** {rep['at']}")
    a(f"- **Base map:** `{rep.get('base_map_id', '?')}`")
    a(f"- **Size:** {rep.get('size', '?')} bytes "
      f"({'ok' if rep.get('size_ok') else 'UNEXPECTED'})")
    if rep["baseline"]:
        a(f"- **Compared against:** `{rep['baseline']}` — {rep['baseline_reason']}")
    else:
        a(f"- **Compared against:** nothing — {rep['baseline_reason']}")
    a("")

    if rep["setup"]:
        a("## Setup check")
        a("")
        for sev, msg in rep["setup"]:
            mark = {"critical": "🛑 **CRITICAL**", "warn": "⚠️ **WARN**",
                    "note": "📌 note", "ok": "✅ ok"}.get(sev, sev)
            a(f"- {mark} — {msg}")
        a("")

    if rep["diff"]:
        cats = rep["diff"]["by_category"]
        a("## What changed")
        a("")
        if not cats:
            a("No byte differences — this file is identical to the baseline.")
        else:
            a("| category | changed bytes | regions | confidence |")
            a("|---|---:|---:|---|")
            for c, v in sorted(cats.items(), key=lambda kv: -kv[1]["bytes"]):
                a(f"| {c} | {v['bytes']} | {v['regions']} | "
                  f"{', '.join(v['confidences']) or 'unmapped'} |")
        a("")

    if rep["dyno"]:
        d = rep["dyno"]
        a("## What the dyno can simulate")
        a("")
        a(f"- usable changes: **{d['usable']}**, unmapped: {d['unmapped']}, "
          f"excluded as churn: {d['excluded']}")
        a(f"- confidence: **{d['confidence']}** — {d['confidence_reason']}")
        if not d["usable"]:
            a("- No change here has a confirmed engineering scale, so the dyno "
              "can report DIRECTION only. This is the ground-truth gap, not a "
              "bug — see `docs/GROUND_TRUTH_EXPERIMENT.md`.")
        a("")

    fb = rep.get("feedback")
    if fb:
        a("## What the bike has been learning")
        a("")
        if fb.get("too_short"):
            a(f"- {fb['too_short']}")
        else:
            a(f"- family of **{fb['saves']}** saves")
            for band, r in fb["bands"].items():
                a(f"- `{band}`: {r['established']} established, "
                  f"{r['suggestive']} suggestive")
                for w in r["top"]:
                    a(f"    - 0x{w['start_offset']:05X}-0x{w['end_offset']:05X} "
                      f"{w['direction']} ({w['share']*100:.0f}% of moves, "
                      f"{w['steps_same_way']}/{w['steps_moved']} saves) "
                      f"— {w['verdict']}")
        a("")

    if rep["errors"]:
        a("## Problems reading this tune")
        a("")
        for e in rep["errors"]:
            a(f"- {e}")
        a("")

    a("---")
    a("")
    a("Nothing here has been flashed or modified. This watcher only reads "
      "`.tbw` files. Any change still goes through the validation-ride "
      "protocol before it is trusted.")
    return "\n".join(L)


def notify_line(rep):
    """One-line push summary -- the bit that reaches a phone."""
    bits = [f"new tune {rep['name']}"]
    crit = [m for s, m in rep["setup"] if s == "critical"]
    warn = [m for s, m in rep["setup"] if s == "warn"]
    if crit:
        bits.append(f"CRITICAL: {crit[0]}")
    elif warn:
        bits.append(f"warn: {warn[0]}")
    if rep.get("diff"):
        cats = rep["diff"]["by_category"]
        top = sorted(cats.items(), key=lambda kv: -kv[1]["bytes"])[:3]
        if top:
            bits.append("changed: " + ", ".join(
                f"{c} {v['bytes']}B" for c, v in top))
        else:
            bits.append("identical to baseline")
    if rep.get("baseline"):
        bits.append(f"vs {rep['baseline']}")
    return " | ".join(bits)


# ---------------------------------------------------------------------------
# notification
# ---------------------------------------------------------------------------

def notify(text, mode="bridge", command=None, log=print):
    """Push a line to Joshua. Never fatal -- a briefing on disk is the record;
    notification is best-effort on top of it."""
    if mode == "none":
        return False
    # `check=False` plus an unread returncode would make this function report
    # success for a notifier that failed every time -- the briefing would be on
    # disk and Joshua would never hear about it, with nothing in the log
    # saying so. The exit code is the only evidence available, so read it.
    try:
        if mode == "command" and command:
            r = subprocess.run(command, shell=True, input=text, text=True,
                               timeout=30, check=False)
            if r.returncode != 0:
                log(f"notify command exited {r.returncode}; "
                    f"briefing is still on disk")
            return r.returncode == 0
        if mode == "bridge":
            exe = shutil.which("agent-bridge") or str(
                Path.home() / ".local/bin/agent-bridge")
            if not Path(exe).exists():
                log("notify: agent-bridge not found; briefing written to disk")
                return False
            env = {**os.environ, "AGENT_BRIDGE_WHOAMI": "claude"}
            r = subprocess.run([exe, "call", "say", text], env=env,
                               timeout=30, check=False,
                               stdout=subprocess.DEVNULL,
                               stderr=subprocess.PIPE, text=True)
            if r.returncode != 0:
                log(f"notify: agent-bridge exited {r.returncode} "
                    f"({(r.stderr or '').strip()[:120]}); "
                    f"briefing is still on disk")
            return r.returncode == 0
    except Exception as e:
        log(f"notify failed ({e}); briefing is still on disk")
    return False


# ---------------------------------------------------------------------------
# the loop
# ---------------------------------------------------------------------------

def handle_new(folder, name, profile, brief_dir, notify_mode, notify_cmd,
               log=print):
    rep = build_briefing(folder, name, profile)
    md = render_briefing(rep)
    brief_dir = Path(brief_dir)
    brief_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    safe = "".join(c if c.isalnum() or c in "-_." else "_"
                   for c in Path(name).stem)[:80]
    out = brief_dir / f"{stamp}_{safe}.md"
    out.write_text(md)
    log(f"briefed {name} -> {out}")
    notify(f"[tmax] {notify_line(rep)} | {out}", notify_mode, notify_cmd, log)
    return rep, out


def poll_once(folder, state, profile, brief_dir, notify_mode, notify_cmd,
              brief_existing=False, log=print):
    """One pass. Returns the number of tunes briefed."""
    current = scan_folder(folder)
    if current is None:
        if not state.get("_folder_missing"):
            log(f"folder unreachable: {folder} (will keep polling)")
            state["_folder_missing"] = True
        return 0
    if state.get("_folder_missing"):
        log(f"folder is back: {folder}")
        state["_folder_missing"] = False

    seen = state.setdefault("seen", {})
    pending = state.setdefault("pending", {})

    if not state.get("seeded") and not brief_existing:
        state["seen"] = current
        state["seeded"] = True
        log(f"seeded {len(current)} existing tunes without briefing "
            f"(use --brief-existing to change that)")
        return 0
    state["seeded"] = True

    briefed = 0
    for name, meta in current.items():
        if name in seen:
            continue

        # Settle check: an SMB copy in flight shows a moving size. Only act
        # once the file is the exact expected size AND has not changed since
        # the previous poll.
        prev = pending.get(name)
        if meta["size"] != EXPECTED_SIZE:
            pending[name] = meta
            continue
        if prev is None or prev.get("size") != meta["size"] or \
                prev.get("mtime") != meta["mtime"]:
            pending[name] = meta
            log(f"waiting for {name} to settle "
                f"({meta['size']} bytes)")
            continue

        pending.pop(name, None)
        try:
            handle_new(folder, name, profile, brief_dir,
                       notify_mode, notify_cmd, log)
            briefed += 1
        except Exception as e:
            log(f"briefing {name} failed: {e}")
        seen[name] = meta

    # Forget files that vanished, so a re-copy briefs again.
    for gone in [n for n in list(seen) if n not in current]:
        seen.pop(gone, None)
    for gone in [n for n in list(pending) if n not in current]:
        pending.pop(gone, None)
    return briefed


def watch(folder, interval=DEFAULT_INTERVAL, state_path=STATE_PATH,
          brief_dir=BRIEF_DIR, notify_mode="bridge", notify_cmd=None,
          brief_existing=False, once=False, log=print):
    profile = load_profile()
    state = load_state(state_path)
    log(f"watching {folder} every {interval}s "
        f"(briefings -> {brief_dir}, notify={notify_mode})")
    while True:
        try:
            poll_once(folder, state, profile, brief_dir, notify_mode,
                      notify_cmd, brief_existing, log)
            save_state(state, state_path)
        except Exception as e:                   # never let the loop die
            log(f"poll error: {e}")
        if once:
            return state
        time.sleep(interval)


def main(argv=None):
    p = argparse.ArgumentParser(
        description="Watch a tune folder and brief on new .tbw files")
    p.add_argument("folder", nargs="?", default=os.environ.get(
        "TMAX_TUNES_DIR", DEFAULT_FOLDER))
    p.add_argument("--interval", type=int, default=DEFAULT_INTERVAL)
    p.add_argument("--state", default=str(STATE_PATH))
    p.add_argument("--brief-dir", default=str(BRIEF_DIR))
    p.add_argument("--notify", choices=["bridge", "command", "none"],
                   default="bridge")
    p.add_argument("--notify-command",
                   help="shell command; the summary arrives on stdin")
    p.add_argument("--brief-existing", action="store_true",
                   help="also brief tunes already present on first run")
    p.add_argument("--once", action="store_true", help="one pass, then exit")
    p.add_argument("--brief", metavar="NAME",
                   help="brief one named tune now and exit (no state change)")
    a = p.parse_args(argv)

    if a.brief:
        rep = build_briefing(a.folder, a.brief)
        print(render_briefing(rep))
        return 0

    watch(a.folder, a.interval, Path(a.state), Path(a.brief_dir),
          a.notify, a.notify_command, a.brief_existing, a.once)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nstopped.")
        sys.exit(0)
