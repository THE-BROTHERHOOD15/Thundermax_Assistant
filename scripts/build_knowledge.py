#!/usr/bin/env python3
"""Build the assistant's knowledge corpus from the mirrored NAS data.

Three sources -> docs/corpus/*.md (all named thundermax_* so the assistant's
doc glob picks them up):

  1. PDFs/CSVs/TXT from the NAS mirror        -> thundermax_nas_<slug>.md
  2. ThrottleLogic_ProjectMemory.json          -> thundermax_throttlelogic_project_memory.md
  3. The tune library itself: every .tbw is scanned, grouped by base-map ID,
     ordered by mtime, and consecutive versions are diffed + classified with
     the table map -> thundermax_tune_history.md (what changed, when, which
     tables - the shop's own tuning history as retrievable knowledge)

Idempotent: re-running overwrites its own outputs and nothing else.
Read-only on .tbw files.
"""
import csv
import io
import json
import re
import subprocess
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
import table_map
import thundermax_parser as tmx

CORPUS = ROOT / "docs" / "corpus"
MIRROR = Path.home() / "tmax-exchange"
DOCS_SRC = MIRROR / "docs-from-nas"
TUNES_SRC = MIRROR / "tunes-from-nas"

# known-fake artifacts from an old AI session - never ingest
SKIP_NAMES = {"FINAL_TUNE_DELIVERY_AIMODIFIED.zip", "ThunderMax_Unlocked_Tuner.zip"}


def slug(name):
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")[:80]


def write_doc(name, title, source, body):
    out = CORPUS / f"{name}.md"
    out.write_text(
        f"---\ntype: reference\ntitle: {title}\nsource: {source}\n---\n\n{body}\n"
    )
    return out


def convert_docs():
    n = 0
    for f in sorted(DOCS_SRC.rglob("*")):
        if not f.is_file() or f.name in SKIP_NAMES or f.name.startswith("._"):
            continue
        rel = f.relative_to(DOCS_SRC)
        if f.suffix.lower() == ".pdf":
            try:
                text = subprocess.run(
                    ["pdftotext", "-layout", str(f), "-"],
                    capture_output=True, text=True, timeout=120).stdout
            except (subprocess.SubprocessError, OSError):
                continue
            if len(text.strip()) < 100:  # image-only scan, nothing to index
                continue
            write_doc(f"thundermax_nas_{slug(f.stem)}", f.stem, str(rel), text[:60000])
            n += 1
        elif f.suffix.lower() == ".csv":
            rows = list(csv.reader(f.open(errors="replace")))
            if not rows:
                continue
            buf = io.StringIO()
            buf.write("| " + " | ".join(rows[0]) + " |\n")
            buf.write("|" + "---|" * len(rows[0]) + "\n")
            for r in rows[1:200]:
                buf.write("| " + " | ".join(r) + " |\n")
            write_doc(f"thundermax_nas_{slug(f.stem)}", f.stem, str(rel), buf.getvalue())
            n += 1
        elif f.suffix.lower() in (".txt", ".md") and f.stat().st_size > 100:
            write_doc(f"thundermax_nas_{slug(f.stem)}", f.stem, str(rel),
                      f.read_text(errors="replace")[:60000])
            n += 1
    return n


def convert_project_memory():
    src = DOCS_SRC / "throttle-logic" / "ThrottleLogic_ProjectMemory.json"
    if not src.exists():
        return 0
    d = json.loads(src.read_text())
    core = d.get("core_context", {})
    body = ["# ThrottleLogic project memory (recovered from the Oct 2025 AI sessions)",
            "", f"Project: {d.get('project', '?')}  ",
            f"Created: {d.get('date_created', '?')}", "",
            "## Key tuning strategies (validated shop history)"]
    for k, v in core.get("key_strategies", {}).items():
        body.append(f"- **{k.replace('_', ' ')}**: {v}")
    body.append("\n## Framework roles the old sessions used")
    for k, v in core.get("framework_roles", {}).items():
        body.append(f"- {k}: {v}")
    body.append("\n## Planned next steps at the time")
    for s in core.get("next_steps", []):
        body.append(f"- {s}")
    write_doc("thundermax_throttlelogic_project_memory",
              "ThrottleLogic Project Memory (Oct 2025)",
              "THROTTLE LOGIC/ThrottleLogic_ProjectMemory.json", "\n".join(body))
    return 1


def build_tune_history():
    tunes = []
    for f in sorted(TUNES_SRC.rglob("*.tbw")) + sorted(TUNES_SRC.rglob("*.TBW")):
        if f.name.startswith("._"):
            continue
        try:
            t = tmx.TbwFile(f)
        except (tmx.TbwError, OSError):
            continue
        if not t.size_ok:
            continue
        tunes.append((f.stat().st_mtime, f, t.base_map_id))

    by_map = defaultdict(list)
    for mt, f, mid in sorted(tunes):
        by_map[mid].append((mt, f))

    tables = table_map.load_map()
    body = ["# Tune history - the shop's own tuning timeline",
            "",
            "Derived automatically from the .tbw library: tunes grouped by "
            "base map, ordered by file date; consecutive versions diffed and "
            "classified with the reverse-engineered table map. Category "
            "confidence: high = trust it, medium = right area, low = "
            "autotune/churn. Raw values are device units, NOT AFR/degrees.",
            ""]
    n_pairs = 0
    for mid, chain in sorted(by_map.items(), key=lambda kv: kv[1][0][0]):
        body.append(f"\n## Base map `{mid}` - {len(chain)} tunes")
        prev = None
        for mt, f in chain:
            day = datetime.fromtimestamp(mt).date().isoformat()
            rel = f.relative_to(TUNES_SRC)
            if prev is None:
                body.append(f"- {day}: `{rel}` (first tune on this base map)")
            else:
                try:
                    rows = table_map.classify_diff(prev, f, tables)
                except ValueError:
                    body.append(f"- {day}: `{rel}` (size mismatch vs previous)")
                    prev = f
                    continue
                interesting = [r for r in rows
                               if r["category"] not in ("METADATA", "SHARED")]
                if not rows:
                    body.append(f"- {day}: `{rel}` - IDENTICAL to previous")
                elif not interesting:
                    body.append(f"- {day}: `{rel}` - checksum/metadata churn only")
                else:
                    cats = table_map.summarize(interesting)
                    parts = []
                    for cat, v in sorted(cats.items(), key=lambda kv: -kv[1]["bytes"]):
                        conf = ",".join(sorted(c for c in v["confidences"] if c != "-"))
                        parts.append(f"{cat} {v['bytes']}B ({conf or 'unmapped'})")
                    body.append(f"- {day}: `{rel}` - changed: " + "; ".join(parts))
                    n_pairs += 1
            prev = f
    body.append(f"\n\n{len(tunes)} valid tunes, {len(by_map)} base maps, "
                f"{n_pairs} classified transitions.")
    write_doc("thundermax_tune_history", "Tune history (auto-derived)",
              "auto-generated from tunes-from-nas", "\n".join(body))
    return len(tunes), len(by_map), n_pairs


if __name__ == "__main__":
    print(f"docs converted:      {convert_docs()}")
    print(f"project memory:      {convert_project_memory()}")
    t, m, p = build_tune_history()
    print(f"tune history:        {t} tunes, {m} base maps, {p} transitions")
    total = len(list(CORPUS.glob('*.md')))
    print(f"corpus total:        {total} docs in {CORPUS}")
