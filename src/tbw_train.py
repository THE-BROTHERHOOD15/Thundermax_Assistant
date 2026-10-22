#!/usr/bin/env python3
"""Throttle Logic training pipeline. Never writes .tbw files.

  tmax train brief|quiz|drill|ingest|seat|all
"""
from __future__ import annotations

import argparse
import json
import shutil
import struct
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
TRAIN = ROOT / "train"
sys.path.insert(0, str(SRC))

import table_map  # noqa: E402
import tbw_read  # noqa: E402
import thundermax_parser as tmx  # noqa: E402

LIVE = Path.home() / "tmax-exchange/tunes-from-usb-2026-08-19/TMAX_TUNES/M8_131_550CAM_63inj.tbw"
SKILL_NAMES = ("reading-tbw-8bit", "throttle-logic-train")
GROK_SKILLS = Path.home() / ".grok/skills"
CLAUDE_SKILLS = Path.home() / ".claude/skills"


def _plant(raw_values):
    data = bytearray([0x55] * tmx.EXPECTED_SIZE)
    struct.pack_into("<4I", data, 0, 0x87, 0x4000, 0x1, 0x147)
    data[0x10] = 16
    data[0x11:0x21] = b"HXSSEDCAAN061617"
    off = 0x1981
    for v in raw_values:
        struct.pack_into("<h", data, off, v)
        struct.pack_into("<h", data, off + 2, 0)
        off += 4
    struct.pack_into("<I", data, off, 32)
    return bytes(data)


def drill() -> dict:
    """Fail-closed machine checks. No NAS required except the optional live file."""
    checks = []

    def add(name, ok, detail):
        checks.append({"name": name, "ok": bool(ok), "detail": detail})

    tables = table_map.load_map()
    afr = tables.get("afr_scale_finding") or {}
    add("tables.json AFR scale 50", afr.get("raw_per_afr") == 50,
        f"raw_per_afr={afr.get('raw_per_afr')}")
    add("landmark 0b00100000", afr.get("landmark") == "0b00100000",
        f"landmark={afr.get('landmark')}")
    add("timing-limit 49", tables.get("timing_scale_finding", {}).get("raw_per_degree") == 49,
        str(tables.get("timing_scale_finding", {}).get("raw_per_degree")))

    import ppm_catalog
    house = ppm_catalog.lookup("HXSSEDCAAN061617")
    add("PPM catalog exact HXSSEDCAAN061617",
        house.get("match") == "exact" and (house.get("record") or {}).get("item_id") == 482,
        f"match={house.get('match')} item={(house.get('record') or {}).get('item_id')}")

    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "a.tbw"
        p.write_bytes(_plant([0, 720, 720, 670]))
        before = p.read_bytes()
        decoded = tbw_read.read_tune(p)
        afrs = [c["afr"] for c in decoded["afr"]["cells"] if c["kind"] == "afr"]
        add("decoder 720/50=14.4", afrs == [14.4, 14.4, 13.4], f"got {afrs}")
        add("never_write flag", decoded.get("never_write_tbw") is True, "")
        tbw_read.propose_from_read(decoded, "afr-audit")
        add("read does not mutate file", p.read_bytes() == before, "")
        q = Path(td) / "b.tbw"
        q.write_bytes(_plant([720, 720]))
        # landmark then garbage
        data = bytearray(q.read_bytes())
        # already has landmark after two cells; plant 800 after it
        struct.pack_into("<h", data, 0x1981 + 12, 800)
        q.write_bytes(data)
        afrs2 = [c["afr"] for c in tbw_read.read_tune(q)["afr"]["cells"] if c["kind"] == "afr"]
        add("landmark stops scan", afrs2 == [14.4, 14.4], f"got {afrs2}")

    if LIVE.is_file():
        live = tbw_read.read_tune(str(LIVE))
        s = live["afr"]["summary"]
        ok = s.get("n", 0) >= 50 and 11.5 <= s.get("min", 0) <= 13.0 and 13.5 <= s.get("max", 0) <= 15.5
        add("live M8_131_550CAM_63inj AFR window", ok,
            f"n={s.get('n')} min={s.get('min')} max={s.get('max')}")
        add("live landmark count", live["landmark"]["u32_count"] >= 1,
            f"count={live['landmark']['u32_count']}")
        c20 = live["commands"]["0x20"]
        c26 = live["commands"]["0x26"]
        c29 = live["commands"]["0x29"]
        add("live FID 0x29 AFR Target",
            c29.get("fid") == 0x29 and (c29.get("summary") or {}).get("n", 0) >= 50,
            f"n={(c29.get('summary') or {}).get('n')} trailers={c29.get('trailers')}")
        add("live FID 0x26 timing trailers",
            c26.get("fid") == 0x26 and len(c26.get("trailers") or []) >= 1,
            f"n={c26.get('n')} trailers={c26.get('trailers')}")
        add("live FID 0x20 Front Fuel/VE band",
            c20.get("fid") == 0x20 and (c20.get("n") or 0) >= 1,
            f"n={c20.get('n')} src={c20.get('source')} band={c20.get('band')}")
    else:
        add("live file (optional)", True, f"skipped, missing {LIVE}")

    for rel in ("train/CURRICULUM.md", "train/briefing.md", "train/drills.json",
                "src/tbw_read.py", "src/tbw_train.py"):
        add(f"exists {rel}", (ROOT / rel).is_file(), str(ROOT / rel))

    passed = all(c["ok"] for c in checks)
    return {"passed": passed, "checks": checks,
            "n_ok": sum(1 for c in checks if c["ok"]), "n": len(checks)}


def brief_text() -> str:
    return (TRAIN / "briefing.md").read_text()


def quiz_text() -> str:
    data = json.loads((TRAIN / "drills.json").read_text())
    lines = [f"# {data['title']}", ""]
    for i, item in enumerate(data["quiz"], 1):
        lines.append(f"{i}. {item['q']}")
        lines.append(f"   expect includes: {', '.join(item['must_include'])}")
        if item.get("must_not"):
            lines.append(f"   must not say: {', '.join(item['must_not'])}")
        lines.append("")
    return "\n".join(lines)


def ingest() -> str:
    """Fold curriculum into the KB via tmax learn. Never writes .tbw."""
    sys.path.insert(0, str(SRC))
    import tune_assistant as ta
    body = (TRAIN / "CURRICULUM.md").read_text()
    path = ta.learn_write(
        "TBW methods curriculum 2026-09-16",
        body,
        source="tmax-train",
    )
    return str(path)


def seat_install() -> list[str]:
    """Copy training skills into Grok and Claude skill dirs. Source is GROK if present."""
    copied = []
    src_root = GROK_SKILLS
    for name in SKILL_NAMES:
        src = src_root / name
        if not (src / "SKILL.md").is_file():
            copied.append(f"skip {name}: no {src}/SKILL.md")
            continue
        for dest_root in (GROK_SKILLS, CLAUDE_SKILLS):
            dest = dest_root / name
            dest.mkdir(parents=True, exist_ok=True)
            if src.resolve() == dest.resolve():
                copied.append(f"ok {dest} (source)")
                continue
            shutil.copytree(src, dest, dirs_exist_ok=True)
            copied.append(f"copied {name} -> {dest}")
    return copied


def _print_drill(result, out=sys.stdout):
    for c in result["checks"]:
        flag = "PASS" if c["ok"] else "FAIL"
        extra = f"  {c['detail']}" if c["detail"] else ""
        out.write(f"{flag}  {c['name']}{extra}\n")
    out.write(f"\n{result['n_ok']}/{result['n']} checks passed\n")
    if not result["passed"]:
        out.write("RESULT: FAIL — seat is not trained until this is green\n")
    else:
        out.write("RESULT: PASS — methods drill green\n")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Throttle Logic TBW training pipeline")
    p.add_argument("cmd", nargs="?", default="brief",
                   choices=("brief", "quiz", "drill", "ingest", "seat", "all"))
    args = p.parse_args(argv)

    if args.cmd == "brief":
        sys.stdout.write(brief_text())
        if not brief_text().endswith("\n"):
            sys.stdout.write("\n")
        return 0
    if args.cmd == "quiz":
        sys.stdout.write(quiz_text())
        return 0
    if args.cmd == "drill":
        result = drill()
        _print_drill(result)
        return 0 if result["passed"] else 1
    if args.cmd == "ingest":
        path = ingest()
        print(f"ingested curriculum -> {path}")
        return 0
    if args.cmd == "seat":
        for line in seat_install():
            print(line)
        return 0
    if args.cmd == "all":
        print("=== drill ===")
        result = drill()
        _print_drill(result)
        print("\n=== brief ===")
        sys.stdout.write(brief_text())
        print("\n=== ingest ===")
        try:
            print(f"ingested curriculum -> {ingest()}")
        except Exception as e:
            print(f"ingest skipped: {e}")
        print("\n=== seat ===")
        for line in seat_install():
            print(line)
        return 0 if result["passed"] else 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
