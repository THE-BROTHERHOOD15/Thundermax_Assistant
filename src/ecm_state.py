#!/usr/bin/env python3
"""ECM pointer + bag/stage helpers for the ThunderMax tune loop.

Never writes the *contents* of a .tbw (no hex edits). Copies files only.
Flash/WRITE to the module is TMax Tuner on windows11 after Joshua says flash.

    tmax ecm show
    tmax ecm bag FILE.tbw
    tmax ecm complaint "launch bog from a stop"
    tmax ecm compare FILE.tbw [--vs on_ecm|last_known_good|last_pull]
    tmax ecm set-on-ecm FILE.tbw --source claimed|pull|flash
    tmax ecm set-good FILE.tbw
    tmax ecm set-pull FILE.tbw
    tmax ecm stage-flash FILE.tbw
    tmax ecm record-flash FILE.tbw [--also-good]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import thundermax_parser as tmx

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_STATE = ROOT / "data" / "ecm_state.json"
EXCHANGE = Path.home() / "tmax-exchange"
NAS_ROOT = Path("/mnt/nas/ADMIN/LOCAL NAS/THROTTLE LOGIC")
LIVE_BASE = "HXSSEDCAAN061617"
BLOCKED_MAP_IDS = frozenset({"ZZSSQXETDN100720"})
NOW = lambda: datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class EcmError(Exception):
    pass


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load_state(path: Path = DEFAULT_STATE) -> dict:
    if not path.exists():
        raise EcmError(f"missing ECM pointer: {path}")
    return json.loads(path.read_text())


def save_state(state: dict, path: Path = DEFAULT_STATE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    state["updated_at"] = NOW()
    text = json.dumps(state, indent=2) + "\n"
    path.write_text(text)
    if path.resolve() != DEFAULT_STATE.resolve():
        return
    mirror = EXCHANGE / "CURRENT_ECM.json"
    try:
        EXCHANGE.mkdir(parents=True, exist_ok=True)
        mirror.write_text(text)
    except OSError as e:
        print(f"warn: could not mirror {mirror}: {e}", file=sys.stderr)


def describe(path: Path):
    p = path.expanduser().resolve()
    if not p.is_file():
        raise EcmError(f"not a file: {p}")
    tbw = tmx.TbwFile(p)
    rec = {
        "path": str(p),
        "name": p.name,
        "sha256": sha256_of(p),
        "base_map_id": tbw.base_map_id,
        "size": len(tbw.data),
        "header": [f"0x{w:X}" for w in tbw.header],
        "valid": bool(tbw.valid),
        "as_of": NOW(),
    }
    return rec, tbw


def _blocked(tbw: tmx.TbwFile, name: str) -> str | None:
    if tbw.base_map_id in BLOCKED_MAP_IDS:
        return (
            f"blocked base-map {tbw.base_map_id} (SE8-517 / 17AUG history). "
            "Do not flash onto 6.3 injectors."
        )
    lower = name.lower()
    if "55inj" in lower or "5.5" in lower.replace(" ", ""):
        return f"blocked filename looks 5.5-injector: {name}"
    return None


def _history(state: dict, action: str, rec: dict) -> None:
    hist = state.setdefault("history", [])
    hist.append({"at": NOW(), "action": action, "name": rec.get("name"), "sha256": rec.get("sha256")})
    state["history"] = hist[-50:]


def cmd_show(state: dict) -> int:
    def line(label, rec):
        if not rec:
            print(f"{label}: (unset)")
            return
        conf = rec.get("confidence") or rec.get("source") or "?"
        print(f"{label}:")
        print(f"  file:       {rec.get('name')}")
        print(f"  path:       {rec.get('path')}")
        print(f"  sha256:     {rec.get('sha256')}")
        print(f"  base map:   {rec.get('base_map_id')}")
        print(f"  size:       {rec.get('size')}")
        print(f"  source:     {rec.get('source', '')}  confidence={conf}")
        print(f"  as of:      {rec.get('as_of')}")
        if rec.get("note"):
            print(f"  note:       {rec['note']}")

    print(f"updated: {state.get('updated_at')}")
    print(f"live base-map (6.3 / CAM2): {LIVE_BASE}")
    c = state.get("complaint")
    if c:
        print(f"complaint: {c.get('text')}  (opened {c.get('opened_at')})")
    else:
        print("complaint: (none — ask Joshua why we are touching the map)")
    print()
    line("ON ECM (claimed)", state.get("on_ecm"))
    print()
    line("last known good", state.get("last_known_good"))
    print()
    line("last pull / stick", state.get("last_pull"))
    print()
    line("last bagged original", state.get("last_bagged"))
    print()
    line("staged for flash", state.get("staged_flash"))
    on = state.get("on_ecm") or {}
    if on.get("confidence") != "operator-confirmed-flash" and on.get("source") != "flash":
        print()
        print("NOTE: on_ecm is NOT a live module read. Do not treat it as verified ECM contents.")
    return 0


def cmd_complaint(state: dict, text: str, path: Path) -> int:
    text = (text or "").strip()
    if not text:
        raise EcmError("complaint text is empty")
    state["complaint"] = {"text": text, "opened_at": NOW()}
    save_state(state, path)
    print(f"complaint set: {text}")
    return 0


def _copy_noclobber(src: Path, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        if sha256_of(dest) == sha256_of(src):
            return dest
        raise EcmError(f"refusing to overwrite {dest} (different content)")
    shutil.copy2(src, dest)
    return dest


def _nas_copy(src: Path, dest_dir: Path) -> Path | None:
    dest_dir = dest_dir if dest_dir.is_dir() else NAS_ROOT
    dest = dest_dir / src.name
    if dest.exists():
        try:
            if sha256_of(dest) == sha256_of(src):
                print(f"NAS already has identical {dest}")
                return dest
        except OSError:
            pass
        print(f"NAS: left existing file alone: {dest}")
        return dest
    cmd = ["sudo", "-n", "cp", "-n", str(src), str(dest)]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True)
    except FileNotFoundError:
        print("NAS copy skipped: sudo not available", file=sys.stderr)
        return None
    if r.returncode != 0:
        print(f"NAS copy failed ({r.returncode}): {r.stderr.strip() or r.stdout.strip()}", file=sys.stderr)
        return None
    print(f"NAS bagged: {dest}")
    return dest


def cmd_bag(state: dict, src: Path, state_path: Path, nas: bool) -> int:
    rec, tbw = describe(src)
    block = _blocked(tbw, src.name)
    if block:
        print(f"warn: {block}", file=sys.stderr)
    local_dir = EXCHANGE / "bagged"
    local = _copy_noclobber(src.resolve(), local_dir / src.name)
    rec["path"] = str(local)
    rec["source"] = "bag"
    rec["confidence"] = "file-copy"
    nas_path = None
    if nas:
        home = NAS_ROOT
        src_res = src.resolve()
        try:
            src_res.relative_to(NAS_ROOT)
            home = src_res.parent
        except ValueError:
            home = NAS_ROOT
        nas_path = _nas_copy(src.resolve(), home)
        rec["nas_path"] = str(nas_path) if nas_path else None
    rec["note"] = "original frozen; never overwrite"
    state["last_bagged"] = rec
    _history(state, "bag", rec)
    save_state(state, state_path)
    print(f"bagged local: {local}")
    print(f"sha256: {rec['sha256']}")
    print(f"base map: {rec['base_map_id']}")
    return 0


def _slot_path(state: dict, vs: str) -> Path:
    if vs in ("on_ecm", "last_known_good", "last_pull", "last_bagged"):
        rec = state.get(vs) or {}
        p = rec.get("path")
        if not p:
            raise EcmError(f"{vs} has no path in ECM pointer")
        return Path(p)
    return Path(vs).expanduser()


def cmd_compare(state: dict, new: Path, vs: str) -> int:
    baseline = _slot_path(state, vs)
    if not baseline.is_file():
        raise EcmError(f"baseline missing: {baseline}")
    a = tmx.TbwFile(baseline)
    b = tmx.TbwFile(new)
    print(f"# compare vs {vs}")
    print(f"baseline: {baseline}")
    print(f"new:      {new}")
    tmx.compare(a, b, out=sys.stdout)
    return 0


def _set_slot(state: dict, slot: str, src: Path, source: str, confidence: str, state_path: Path) -> int:
    rec, tbw = describe(src)
    block = _blocked(tbw, src.name)
    if block and slot in ("on_ecm", "staged_flash"):
        raise EcmError(block)
    if tbw.base_map_id != LIVE_BASE and slot in ("on_ecm", "staged_flash"):
        print(
            f"warn: base-map {tbw.base_map_id} is not live 6.3/CAM2 {LIVE_BASE}",
            file=sys.stderr,
        )
    rec["source"] = source
    rec["confidence"] = confidence
    state[slot] = rec
    _history(state, f"set-{slot}", rec)
    save_state(state, state_path)
    print(f"{slot} <- {rec['name']}  {rec['sha256'][:16]}…  {rec['base_map_id']}")
    print(f"confidence={confidence} source={source}")
    return 0


def cmd_stage_flash(state: dict, src: Path, state_path: Path) -> int:
    rec, tbw = describe(src)
    block = _blocked(tbw, src.name)
    if block:
        raise EcmError(block)
    if tbw.base_map_id != LIVE_BASE:
        raise EcmError(
            f"refusing to stage {tbw.base_map_id}; live hardware is {LIVE_BASE} / 6.3 injectors"
        )
    dest = EXCHANGE / "to-windows" / src.name
    _copy_noclobber(src.resolve(), dest)
    rec["path"] = str(dest)
    rec["source"] = "stage-flash"
    rec["confidence"] = "staged-not-flashed"
    rec["note"] = (
        "NOT on the ECM. Open in TMax. Confirm injector size 6.3, idle 1024, "
        "decel cut OFF, AutoTune OFF. WRITE only after Joshua says flash. "
        "After WRITE: initialize if asked, hands off throttle; one start, no throttle; "
        "both pipes hot in 30s or shut off."
    )
    state["staged_flash"] = rec
    _history(state, "stage-flash", rec)
    save_state(state, state_path)
    print(f"staged (not flashed): {dest}")
    print(rec["note"])
    return 0


def cmd_record_flash(state: dict, src: Path, also_good: bool, state_path: Path) -> int:
    rec, tbw = describe(src)
    block = _blocked(tbw, src.name)
    if block:
        raise EcmError(block)
    rec["source"] = "flash"
    rec["confidence"] = "operator-confirmed-flash"
    rec["note"] = "Joshua confirmed WRITE. Not a live re-read of the module."
    state["on_ecm"] = rec
    state["staged_flash"] = None
    if also_good:
        good = dict(rec)
        good["source"] = "flash"
        good["confidence"] = "operator-confirmed-flash"
        state["last_known_good"] = good
    _history(state, "record-flash", rec)
    save_state(state, state_path)
    print(f"on_ecm now {rec['name']} (operator-confirmed-flash)")
    if also_good:
        print("last_known_good updated to the same file")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="tmax ecm", description=__doc__)
    p.add_argument("--state", default=str(DEFAULT_STATE), help="pointer JSON path")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("show", help="print on_ecm / last good / last pull / complaint")
    b = sub.add_parser("bag", help="freeze a copy of the original (local + NAS)")
    b.add_argument("file")
    b.add_argument("--no-nas", action="store_true")
    c = sub.add_parser("complaint", help="why we are touching the map")
    c.add_argument("text")
    cmp_ = sub.add_parser("compare", help="diff a file against a pointer slot")
    cmp_.add_argument("file")
    cmp_.add_argument("--vs", default="last_known_good",
                      help="on_ecm | last_known_good | last_pull | last_bagged | /path")
    s = sub.add_parser("set-on-ecm", help="claim what is on the module (does not flash)")
    s.add_argument("file")
    s.add_argument("--source", choices=("claimed", "pull", "flash"), default="claimed")
    g = sub.add_parser("set-good", help="set last-known-good pointer")
    g.add_argument("file")
    pl = sub.add_parser("set-pull", help="set last stick/pull pointer")
    pl.add_argument("file")
    st = sub.add_parser("stage-flash", help="copy to to-windows; still not a WRITE")
    st.add_argument("file")
    rf = sub.add_parser("record-flash", help="Joshua confirmed WRITE; update on_ecm")
    rf.add_argument("file")
    rf.add_argument("--also-good", action="store_true")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    state_path = Path(args.state)
    if args.cmd == "show" and not state_path.exists():
        print(f"error: no ECM pointer at {state_path}", file=sys.stderr)
        return 2
    state = load_state(state_path) if state_path.exists() else {
        "schema": 1, "complaint": None, "on_ecm": None,
        "last_known_good": None, "last_pull": None, "last_bagged": None,
        "staged_flash": None, "history": [],
    }
    try:
        if args.cmd == "show":
            return cmd_show(state)
        if args.cmd == "complaint":
            return cmd_complaint(state, args.text, state_path)
        if args.cmd == "bag":
            return cmd_bag(state, Path(args.file), state_path, nas=not args.no_nas)
        if args.cmd == "compare":
            return cmd_compare(state, Path(args.file), args.vs)
        if args.cmd == "set-on-ecm":
            conf = {
                "claimed": "file-claimed",
                "pull": "verified-pull",
                "flash": "operator-confirmed-flash",
            }[args.source]
            return _set_slot(state, "on_ecm", Path(args.file), args.source, conf, state_path)
        if args.cmd == "set-good":
            return _set_slot(state, "last_known_good", Path(args.file), "claimed", "file-claimed", state_path)
        if args.cmd == "set-pull":
            return _set_slot(state, "last_pull", Path(args.file), "pull", "file-claimed", state_path)
        if args.cmd == "stage-flash":
            return cmd_stage_flash(state, Path(args.file), state_path)
        if args.cmd == "record-flash":
            return cmd_record_flash(state, Path(args.file), args.also_good, state_path)
    except (EcmError, tmx.TbwError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    sys.exit(main())
