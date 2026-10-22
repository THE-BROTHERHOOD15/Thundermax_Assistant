#!/usr/bin/env python3
"""Read-only ride-log suggestions and SHA-256 tune notes.

Never writes a .tbw. Compare, curves, and undo stay in the Windows studio.
shortcut: column match is a header substring, not the studio's fuzzy detector.
Upgrade by swapping this parser for a private studio core if one is installed.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import sys
from pathlib import Path

MIN_N = 10
MAX_PCT = 15.0
DEADBAND = 1.0
ET_MIN_F = 160.0
DECEL_TPS = 1.0
DECEL_RATE = 3.0
IDLE_RPM = 1200.0
AFR_LO = 9.5
AFR_HI = 19.5
DEFAULT_TARGET = 13.2
NOTE_FIELDS = ("weather", "mods", "feel", "text")


def notes_path() -> Path:
    return Path.home() / "tmax-exchange" / "tune_notes.json"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load_notes(path: Path | None = None) -> dict:
    path = path or notes_path()
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def save_notes(db: dict, path: Path | None = None) -> None:
    path = path or notes_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(db, indent=2) + "\n", encoding="utf-8")


def note_get(db: dict, sha: str):
    return db.get(sha.lower()) if sha else None


def note_set(db: dict, sha: str, note: dict, file_name: str) -> dict:
    key = sha.lower()
    prev = db.get(key) or {}
    nxt = {f: str(note.get(f) or "") for f in NOTE_FIELDS}
    names = list(prev.get("files") or [])
    if file_name and file_name not in names:
        names.append(file_name)
    nxt["files"] = names
    if all(not nxt[f].strip() for f in NOTE_FIELDS):
        db.pop(key, None)
    else:
        db[key] = nxt
    return db


def _col(header: list[str], pred) -> int:
    for i, name in enumerate(header):
        if pred(name.lower().strip()):
            return i
    return -1


def parse_log(text: str) -> dict | None:
    rows = list(csv.reader(io.StringIO(text.replace("\r\n", "\n"))))
    rows = [r for r in rows if any(c.strip() for c in r)]
    if len(rows) < 2:
        return None
    header = [c.strip() for c in rows[0]]
    data = []
    for raw in rows[1:]:
        nums = []
        ok = False
        for cell in raw:
            try:
                nums.append(float(cell.strip()))
                ok = True
            except ValueError:
                nums.append(float("nan"))
        if ok:
            data.append(nums)
    if not data:
        return None
    return {"header": header, "rows": data}


def suggest_log(text: str) -> dict:
    log = parse_log(text)
    if not log:
        return {"error": "no rows", "suggestions": []}
    header = log["header"]
    cols = {
        "rpm": _col(header, lambda h: "rpm" in h),
        "tps": _col(header, lambda h: "tps" in h or "throttle" in h),
        "afr": _col(header, lambda h: ("afr" in h or "lambda" in h) and "target" not in h and "rear" not in h),
        "tgt": _col(header, lambda h: "target" in h and ("afr" in h or "lambda" in h)),
        "et": _col(header, lambda h: "temp" in h or h in {"et", "cht", "ect"}),
    }
    if cols["rpm"] < 0 or cols["tps"] < 0 or cols["afr"] < 0:
        return {"error": "need RPM, TPS, and AFR columns", "suggestions": []}
    tps_name = header[cols["tps"]].lower()
    tps_vals = [r[cols["tps"]] if cols["tps"] < len(r) else float("nan") for r in log["rows"]]
    finite_tps = [v for v in tps_vals if v == v]
    tps_is_pct = "%" in tps_name or "percent" in tps_name or (finite_tps and max(finite_tps) > 89.5)
    cells: dict[tuple[int, int], dict] = {}
    skipped = {"warmup": 0, "decel": 0, "range": 0, "nodata": 0}
    used = 0
    prev_t = None
    for row in log["rows"]:
        def cell(i):
            if i < 0 or i >= len(row):
                return float("nan")
            return row[i]

        rpm, tps, afr = cell(cols["rpm"]), cell(cols["tps"]), cell(cols["afr"])
        if rpm != rpm or tps != tps:
            skipped["nodata"] += 1
            continue
        if tps_is_pct:
            tps = tps * 88.68 / 100
        et = cell(cols["et"])
        if et == et and et < ET_MIN_F:
            skipped["warmup"] += 1
            prev_t = tps
            continue
        d_t = (tps - prev_t) if prev_t is not None else 0.0
        prev_t = tps
        if (tps < DECEL_TPS and rpm > IDLE_RPM) or d_t < -DECEL_RATE:
            skipped["decel"] += 1
            continue
        if afr != afr or afr < AFR_LO or afr > AFR_HI:
            skipped["range"] += 1
            continue
        tgt = cell(cols["tgt"])
        if tgt != tgt or tgt <= 0:
            tgt = DEFAULT_TARGET
        page = max(0, min(31, round(rpm / 256)))
        col = max(0, min(63, round(tps * 63 / 88.68)))
        bucket = cells.setdefault(page, {}).setdefault(col, {"n": 0, "se": 0.0, "sa": 0.0, "st": 0.0})
        bucket["n"] += 1
        bucket["se"] += (afr / tgt - 1) * 100
        bucket["sa"] += afr
        bucket["st"] += tgt
        used += 1
    suggestions = []
    for page, cols_ in sorted(cells.items()):
        for col, b in sorted(cols_.items()):
            if b["n"] < MIN_N:
                continue
            err = b["se"] / b["n"]
            if abs(err) < DEADBAND:
                continue
            pct = max(-MAX_PCT, min(MAX_PCT, err))
            suggestions.append({
                "rpm": page * 256,
                "tps": round(col * 88.68 / 63, 3),
                "n": b["n"],
                "afr": round(b["sa"] / b["n"], 2),
                "tgt": round(b["st"] / b["n"], 2),
                "err": round(err, 2),
                "pct": round(pct, 1),
            })
    return {"error": None, "used": used, "skipped": skipped, "suggestions": suggestions}


def format_suggest(result: dict) -> str:
    if result.get("error"):
        return result["error"] + "\n"
    lines = [
        f"# log suggest — proposal only, .tbw not written",
        f"used {result['used']}  skipped {result['skipped']}",
    ]
    if not result["suggestions"]:
        lines.append("no cell cleared 10 samples and a 1% change")
    for s in result["suggestions"]:
        lines.append(
            f"rpm {s['rpm']} tps {s['tps']} n={s['n']} "
            f"afr {s['afr']} tgt {s['tgt']} err {s['err']:+.2f}%  fuel {s['pct']:+.1f}%"
        )
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Read-only log suggestions and tune notes")
    sub = p.add_subparsers(dest="cmd", required=True)
    lg = sub.add_parser("log", help="suggest fuel percent from a text ride log")
    lg.add_argument("file")
    ng = sub.add_parser("notes")
    nsub = ng.add_subparsers(dest="notes_cmd", required=True)
    g = nsub.add_parser("get")
    g.add_argument("file")
    s = nsub.add_parser("set")
    s.add_argument("file")
    for field in NOTE_FIELDS:
        s.add_argument(f"--{field}", default="")
    args = p.parse_args(argv)
    if args.cmd == "log":
        text = Path(args.file).read_text(encoding="utf-8", errors="replace")
        sys.stdout.write(format_suggest(suggest_log(text)))
        return 0
    path = Path(args.file)
    sha = sha256_file(path)
    db = load_notes()
    if args.notes_cmd == "get":
        hit = note_get(db, sha)
        sys.stdout.write(json.dumps({"sha256": sha, "note": hit}, indent=2) + "\n")
        return 0
    note_set(db, sha, {f: getattr(args, f) for f in NOTE_FIELDS}, path.name)
    save_notes(db)
    sys.stdout.write(json.dumps({"sha256": sha, "note": note_get(load_notes(), sha)}, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
