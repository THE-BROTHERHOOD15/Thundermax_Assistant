"""Ingestion workflow. One bad file never stops the batch. Never writes .tbw."""

import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path

from kb.classify import classify_file, should_skip
from kb.chunk import chunk_for_type, estimate_tokens
from kb.claims import add_claim
from kb.extract import extract, sha256_file
from kb.names import archive_name, mtime_date, parse_archive_name, slug_field
from kb.paths import CONTROLLED_TAGS, ORIGINAL_SUBDIR, ensure_tree, kb_root
from kb.store import append_jsonl, connect, next_id, utcnow

SKIP_NAMES = {"FINAL_TUNE_DELIVERY_AIMODIFIED.zip", "ThunderMax_Unlocked_Tuner.zip"}

HOUSE_RULES_MD = """# Shop calibration rules (Tier 4)

Scope: 2023 Harley-Davidson Low Rider ST, Milwaukee-Eight 131ci, S&S 550 / CAM2,
6.3 injectors, 2-into-1 exhaust, ThunderMax TBW. Base-map HXSSEDCAAN061617.

These are validated shop rules, not generic internet advice.

## AutoTune gates

Enable AutoTune learning above 200°F CHT. Disable above 280°F. Cold-start and
heat-soak trims are garbage and must be blocked.

## Injector mismatch

Never flash a 5.5 injector / *55inj* map onto 6.3 injectors.
17AUG2025ATLRUNNINGGREATv6TODAY.tbw is SE8-517 history. v6 means version 6,
not 6.3 injectors. If TMax shows injector size 5.5, do not WRITE it onto 6.3s.

## Flash-first file

M8_131_550CAM_63inj.tbw. Confirm injector size 6.3, idle 1024, decel cut OFF,
AutoTune OFF. After WRITE: initialize if asked, hands off throttle; one start,
no throttle; both pipes hot in 30 seconds or shut off.

## CH-home heat-soak

Do not lock or trust AutoTune trims from the CH-home file named 330-345°F head
temp. 330–345°F CHT is above the 280°F disable gate.

## Decel pop protocol

Closed-throttle 4000→2000 rpm decels in 3rd/4th. Pops above 4k → +2% VE at
0–2% TPS, 3840–4608 rpm. Broad-range pops → −1° spark at 0–2% TPS, 2048–2816 rpm.

## Logging

Always log TPS, RPM, AFR target vs actual, CHT, trims. Every tune change gets
a validation ride.

## Observation vs interpretation

A raw log is evidence. A human note is an interpretation. A general tuning rule
is a conclusion. Do not treat every historical comment as a validated fact.
"""

RETRIEVAL_RULES = """# The AI's retrieval rules

Before answering a technical question, identify the current vehicle, engine,
hardware, fuel, module, firmware, operating conditions, and objective.

Retrieve by metadata before semantic similarity. Prefer same-configuration
validated evidence over general similarity. Do not treat a similar vehicle as
the same vehicle.

Every important conclusion must cite its source file and chunk. Distinguish
observed facts, inferred explanations, historical notes, and validated rules.

If sources conflict, show the conflict and explain which source has stronger
scope, evidence, recency, or validation.

If no sufficiently similar evidence exists, say so and recommend the smallest
safe next data-collection step.

Never use an unreadable or encrypted file as if its contents were known.
Never fabricate map values, decoded fields, sensor results, or validation.
"""

HOUSE_CLAIMS = (
    {
        "claim": "AutoTune learning gates: enable above 200F CHT, disable above 280F. Cold-start and heat-soak trims are not trusted VE.",
        "claim_type": "validated",
        "status": "validated",
        "confidence": "high",
        "conditions": "ThunderMax TBW, this shop, CHT in F",
        "evidence_summary": "Repeated shop history; wired as house rule.",
        "knowledge_tier": 4,
        "validation_tested": True,
        "tags": ["validated", "temperature-compensation"],
    },
    {
        "claim": "Never flash 17AUG2025ATLRUNNINGGREATv6TODAY.tbw or any *55inj*/5.5 injector map onto the current 6.3 injector bike. v6 means version 6, not 6.3 injectors.",
        "claim_type": "validated",
        "status": "validated",
        "confidence": "high",
        "conditions": "current hardware 6.3 injectors / S&S 550 / CAM2",
        "evidence_summary": "USB 2026-08-19 shop facts; brick-the-fueling risk.",
        "knowledge_tier": 4,
        "validation_tested": True,
        "tags": ["validated", "injectors", "firmware"],
    },
    {
        "claim": "Do not lock or trust AutoTune trims from the CH-home 330-345F head-temp file. Those trims were learned above the 280F disable gate (heat-soak).",
        "claim_type": "validated",
        "status": "validated",
        "confidence": "high",
        "conditions": "CHT 330-345F claimed on that filename",
        "evidence_summary": "Classified diff vs FuelMoto original is AutoTune/learned-VE only.",
        "knowledge_tier": 4,
        "validation_tested": True,
        "tags": ["validated", "needs-validation", "temperature-compensation"],
    },
    {
        "claim": "Decel pop protocol: closed-throttle 4000 to 2000 rpm in 3rd/4th. Pops above 4k -> +2% VE at 0-2% TPS, 3840-4608 rpm. Broad-range pops -> -1 deg spark at 0-2% TPS, 2048-2816 rpm.",
        "claim_type": "validated",
        "status": "validated",
        "confidence": "high",
        "conditions": "this bike, ThunderMax maps, validation ride required after change",
        "evidence_summary": "Shop protocol in COLLABORATION.md.",
        "knowledge_tier": 4,
        "validation_tested": True,
        "tags": ["validated", "deceleration", "fueling", "ignition"],
    },
)

DEFAULT_SCOPE = {
    "vehicle": "2023-fxlrst",
    "engine": "131ci",
    "hardware": "ss550-63inj-2into1",
}


def _copy_readonly(src, dest):
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        stem = dest.name
        suffix = dest.suffix
        base = dest.name[:-len(suffix)] if suffix and dest.name.endswith(suffix) else dest.name
        n = 2
        while dest.exists():
            dest = dest.with_name(f"{base}-n{n:02d}{suffix}")
            n += 1
            if n > 999:
                raise OSError(f"cannot unique-name {stem}")
    shutil.copy2(src, dest)
    os.chmod(dest, 0o444)
    return dest


def _infer_meta(path, file_type, extra):
    parsed = parse_archive_name(path.name)
    meta = {
        "vehicle": extra.get("vehicle") or (parsed or {}).get("vehicle") or extra.get("vehicle_key"),
        "engine": extra.get("engine") or (parsed or {}).get("engine") or extra.get("engine_key"),
        "project": extra.get("project") or (parsed or {}).get("project") or extra.get("project_key") or "unk",
        "event": extra.get("event") or (parsed or {}).get("event") or slug_field(path.stem)[:40],
        "date": extra.get("date") or (parsed or {}).get("date") or mtime_date(path),
    }
    return meta


def _embed_chunk(text, timeout=20):
    import urllib.request
    body = json.dumps({"model": "nomic-embed-text", "prompt": text[:8000]}).encode()
    req = urllib.request.Request(
        "http://127.0.0.1:11434/api/embeddings",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())["embedding"]


def _write_derivative(root, file_id, kind, text, ext="md"):
    rel = Path("02_normalized") / kind / f"{file_id}.{ext}"
    dest = root / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(text or "", encoding="utf-8")
    return str(rel)


def ingest_path(path, root=None, source="user_archive", extra=None, embed=False,
                con=None, copy_original=True):
    """Ingest one file. Returns a result dict. Never modifies the source file."""
    path = Path(path)
    extra = dict(extra or {})
    base = kb_root(root)
    ensure_tree(base)
    own = con is None
    if own:
        con = connect(base)
    result = {
        "source_path": str(path),
        "ok": False,
        "file_id": None,
        "status": None,
        "reason": None,
    }
    try:
        if should_skip(path) or path.name in SKIP_NAMES:
            result["status"] = "skip"
            result["reason"] = "skipped sidecar or denylisted name"
            return result
        if not path.is_file():
            result["status"] = "skip"
            result["reason"] = "not a file"
            return result

        digest = sha256_file(path)
        existing = con.execute(
            "SELECT file_id, archive_path FROM files WHERE sha256=?", (digest,)
        ).fetchone()
        file_type, mime, sniff = classify_file(path)
        meta = _infer_meta(path, file_type, extra)
        vehicle = extra.get("vehicle_key") or meta["vehicle"]
        engine = extra.get("engine_key") or meta["engine"]
        hardware = extra.get("hardware_key")
        project = extra.get("project_key") or meta["project"]

        if existing:
            file_id = next_id(con, "file")
            qdir = base / "08_quarantine" / "duplicates"
            pointer = qdir / f"{file_id}.json"
            pointer.parent.mkdir(parents=True, exist_ok=True)
            pointer.write_text(json.dumps({
                "file_id": file_id,
                "duplicate_of": existing["file_id"],
                "source_path": str(path),
                "sha256": digest,
            }, indent=2) + "\n")
            con.execute(
                """INSERT INTO files(
                     file_id, original_path, source_path, archive_path, sha256,
                     file_type, parser_status, source, created_at, ingested_at,
                     mime, size_bytes, vehicle_key, engine_key, hardware_key,
                     project_key, duplicate_of, quarantine_reason, knowledge_tier,
                     metadata_json)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    file_id, str(path), str(path), existing["archive_path"], digest,
                    file_type, "duplicate", source, utcnow(), utcnow(),
                    mime, path.stat().st_size, vehicle, engine, hardware,
                    project, existing["file_id"],
                    "exact sha256 duplicate; original preserved",
                    0, json.dumps({"sniff": sniff}),
                ),
            )
            append_jsonl(base / "manifests" / "file_manifest.jsonl", {
                "file_id": file_id, "sha256": digest, "parser_status": "duplicate",
                "duplicate_of": existing["file_id"], "source_path": str(path),
            })
            result.update(ok=True, file_id=file_id, status="duplicate",
                          reason=f"duplicate of {existing['file_id']}")
            if own:
                con.commit()
            return result

        extracted = extract(path, file_type)
        parser_status = extracted.get("status") or "unsupported"
        reason = extracted.get("reason")
        text = extracted.get("text")
        map_id = extracted.get("map_id") or extra.get("map_id")

        file_id = next_id(con, "file")
        ext = path.suffix.lstrip(".").lower() or "bin"
        stored_name = archive_name(
            meta["date"], project, vehicle, engine,
            f"{meta['event']}-{file_id}",
            file_type, extra.get("version") or "v01", ext,
        )
        dest_rel = None
        archive_path = None
        if parser_status in {"corrupt", "unsupported", "suspicious"}:
            qkind = "corrupt" if parser_status == "corrupt" else (
                "suspicious" if parser_status == "suspicious" else "unsupported"
            )
            dest = base / "08_quarantine" / qkind / stored_name
            if copy_original:
                _copy_readonly(path, dest)
                dest_rel = str(dest.relative_to(base))
                archive_path = dest_rel
            note = base / "08_quarantine" / qkind / (stored_name + ".why.json")
            note.write_text(json.dumps({
                "file_id": file_id,
                "reason": reason,
                "source_path": str(path),
                "required": "authorized ThunderMax export or supported parser",
            }, indent=2) + "\n")
        else:
            sub = ORIGINAL_SUBDIR.get(file_type, "01_originals/other")
            dest = base / sub / stored_name
            if copy_original:
                _copy_readonly(path, dest)
                dest_rel = str(dest.relative_to(base))
                archive_path = dest_rel

        deriv_kind = "extracted_text"
        if file_type == "datalog":
            deriv_kind = "log_tables"
        elif file_type in {"tbw", "map"}:
            deriv_kind = "map_tables"
        elif file_type in {"screenshot", "image"}:
            deriv_kind = "ocr_text" if parser_status == "parsed" else "image_descriptions"
        derivative_path = None
        if text:
            derivative_path = _write_derivative(base, file_id, deriv_kind, text)

        tier = 1 if parser_status == "parsed" else 0
        if file_type == "tbw":
            tier = 0
        validated = 1 if extra.get("validated") else 0

        con.execute(
            """INSERT INTO files(
                 file_id, original_path, source_path, archive_path, derivative_path,
                 sha256, file_type, parser_status, source, created_at, ingested_at,
                 mime, size_bytes, vehicle_key, engine_key, hardware_key, fuel_key,
                 tmax_key, project_key, map_id, quality_confidence, validated,
                 knowledge_tier, quarantine_reason, metadata_json)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                file_id, archive_path or str(path), str(path), archive_path,
                derivative_path, digest, file_type, parser_status, source,
                utcnow(), utcnow(), mime, path.stat().st_size,
                vehicle, engine, hardware, extra.get("fuel_key"),
                extra.get("tmax_key"), project, map_id,
                extra.get("confidence") or "medium", validated, tier, reason,
                json.dumps({
                    "sniff": sniff,
                    "extract": {k: extracted.get(k) for k in ("map_id", "size", "valid", "rows")
                                if k in extracted},
                }, ensure_ascii=False),
            ),
        )

        tags = list(extra.get("tags") or [])
        if file_type == "datalog":
            tags.append("fueling")
        if extra.get("validated"):
            tags.append("validated")
        else:
            tags.append("needs-validation")
        for tag in tags:
            tag = slug_field(tag)
            if tag not in CONTROLLED_TAGS:
                con.execute("INSERT OR IGNORE INTO tags(tag) VALUES (?)", (tag,))
            con.execute(
                "INSERT OR IGNORE INTO file_tags(file_id, tag) VALUES (?,?)",
                (file_id, tag),
            )

        if file_type == "tbw" and map_id:
            con.execute(
                """INSERT INTO maps(file_id, map_id, table_name, summary, parser_status)
                   VALUES (?,?,?,?,?)""",
                (file_id, map_id, "tbw-header", text, parser_status),
            )
        if file_type == "datalog":
            for seg in extracted.get("segments") or []:
                con.execute(
                    """INSERT INTO logs(file_id, segment, row_start, row_end, summary)
                       VALUES (?,?,?,?,?)""",
                    (file_id, seg.get("segment"), seg.get("row_start"),
                     seg.get("row_end"), seg.get("summary")),
                )

        tag_blob = " ".join(tags)
        chunks = []
        if text and parser_status in {"parsed", "needs_review"}:
            chunks = chunk_for_type(
                file_type, text,
                segments=extracted.get("segments"),
                date=meta["date"], vehicle=vehicle, project=project,
            )
        chunk_ids = []
        for ch in chunks:
            cid = next_id(con, "chunk")
            chunk_ids.append(cid)
            body = ch["text"]
            con.execute(
                """INSERT INTO chunks(
                     chunk_id, file_id, derivative_path, chunk_kind, heading, text,
                     token_estimate, row_start, row_end, vehicle_key, project_key,
                     date, metadata_json)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    cid, file_id, derivative_path, ch.get("kind"), ch.get("heading"),
                    body, estimate_tokens(body), ch.get("row_start"), ch.get("row_end"),
                    vehicle, project, meta["date"],
                    json.dumps({"source_chain": {
                        "file_id": file_id,
                        "archive_path": archive_path,
                        "derivative_path": derivative_path,
                    }}),
                ),
            )
            con.execute(
                "INSERT INTO chunks_fts(chunk_id, file_id, heading, text, tags) VALUES (?,?,?,?,?)",
                (cid, file_id, ch.get("heading") or "", body, tag_blob),
            )
            if embed:
                try:
                    vec = _embed_chunk(body)
                    con.execute(
                        """INSERT INTO embedding_records(chunk_id, model, dim, vector, created_at)
                           VALUES (?,?,?,?,?)""",
                        (cid, "nomic-embed-text", len(vec), json.dumps(vec), utcnow()),
                    )
                except Exception as e:
                    result.setdefault("embed_errors", []).append(str(e))

        rec = {
            "file_id": file_id,
            "original_path": archive_path,
            "source_path": str(path),
            "sha256": digest,
            "file_type": file_type,
            "parser_status": parser_status,
            "source": source,
            "created_at": utcnow(),
            "vehicle": {"key": vehicle, "engine_displacement": engine},
            "thundermax": {"map_id": map_id},
            "quality": {
                "confidence": extra.get("confidence") or "medium",
                "validated": bool(validated),
            },
            "tags": tags,
            "chunks": chunk_ids,
            "quarantine_reason": reason,
        }
        append_jsonl(base / "manifests" / "file_manifest.jsonl", rec)
        result.update(ok=True, file_id=file_id, status=parser_status, reason=reason,
                      chunks=len(chunk_ids), archive_path=archive_path)
        if own:
            con.commit()
        return result
    except Exception as e:
        result["status"] = "error"
        result["reason"] = str(e)
        if own:
            con.rollback()
        return result
    finally:
        if own:
            con.close()


def ingest_tree(src, root=None, source="user_archive", extra=None, embed=False,
                glob="**/*"):
    src = Path(src)
    base = kb_root(root)
    ensure_tree(base)
    con = connect(base)
    run_id = next_id(con, "run")
    started = utcnow()
    files = [src] if src.is_file() else sorted(p for p in src.glob(glob) if p.is_file())
    seen = ok = quar = dup = 0
    results = []
    for p in files:
        if should_skip(p) or p.name in SKIP_NAMES:
            continue
        seen += 1
        r = ingest_path(p, root=base, source=source, extra=extra, embed=embed, con=con)
        results.append(r)
        if r.get("status") == "duplicate":
            dup += 1
            ok += 1
        elif r.get("status") in {"corrupt", "unsupported", "suspicious", "error"}:
            quar += 1
            if r.get("ok"):
                ok += 1
        elif r.get("ok"):
            ok += 1
        else:
            quar += 1
    finished = utcnow()
    con.execute(
        """INSERT INTO import_runs(
             run_id, started_at, finished_at, source, files_seen, files_ok,
             files_quarantine, files_duplicate, notes)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (run_id, started, finished, str(src), seen, ok, quar, dup,
         json.dumps({"embed": embed})),
    )
    con.commit()
    con.close()
    report = {
        "run_id": run_id,
        "source": str(src),
        "files_seen": seen,
        "files_ok": ok,
        "files_quarantine": quar,
        "files_duplicate": dup,
        "started_at": started,
        "finished_at": finished,
        "results": results,
    }
    append_jsonl(base / "manifests" / "import_runs.jsonl", {
        k: report[k] for k in report if k != "results"
    })
    (base / "05_analysis" / "validation_reports" / f"{run_id}.json").write_text(
        json.dumps(report, indent=2, default=str) + "\n"
    )
    return report


def _seed_entities(con, base):
    con.execute(
        """INSERT OR IGNORE INTO vehicles(vehicle_key, year, make, model, engine_displacement)
           VALUES ('2023-fxlrst', 2023, 'Harley-Davidson', 'Low Rider ST', '131ci')"""
    )
    con.execute(
        """INSERT OR IGNORE INTO engines(engine_key, family, displacement, camshaft)
           VALUES ('131ci', 'Milwaukee-Eight', '131ci', 'S&S 550 / CAM2')"""
    )
    con.execute(
        """INSERT OR IGNORE INTO hardware_configs(
             hardware_key, intake, exhaust, camshaft, injectors)
           VALUES ('ss550-63inj-2into1', NULL, '2-into-1', 'S&S 550 / CAM2', '6.3')"""
    )
    con.execute(
        """INSERT OR IGNORE INTO thundermax_configs(tmax_key, module, map_id)
           VALUES ('tbw-hxssedcaan061617', 'ThunderMax TBW', 'HXSSEDCAAN061617')"""
    )
    dest = Path(base) / "03_entities" / "vehicles" / "2023-fxlrst.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(
        json.dumps({
            "vehicle_key": "2023-fxlrst",
            "year": 2023,
            "make": "Harley-Davidson",
            "model": "Low Rider ST",
            "engine_displacement": "131ci",
        }, indent=2) + "\n"
    )


def _write_eval_questions(base):
    from kb.eval_questions import QUESTIONS
    dest = base / "07_evaluations" / "questions" / "suite-001.json"
    dest.write_text(json.dumps(QUESTIONS, indent=2) + "\n")
    expect = base / "07_evaluations" / "expected_answers" / "suite-001.json"
    expect.write_text(json.dumps([
        {"id": q["id"], "must_cite_kind": q.get("must_cite_kind"),
         "notes": q.get("expected")}
        for q in QUESTIONS
    ], indent=2) + "\n")


README_TEXT = """# ThunderMax knowledge base

Three layers:

1. Immutable file archive (`01_originals/`) — never edit these files.
2. Structured metadata database (`database/knowledge.sqlite`) — exact lookup.
3. Hybrid retrieval index (FTS5 + optional nomic-embed-text).

Use the filesystem for truth, SQLite for organization, embeddings for meaning.

## Commands

```
tmax kb init
tmax kb status
tmax kb ingest PATH [--embed]
tmax kb retrieve "question"
tmax kb claim --text "..." --type observation
tmax kb views
tmax kb eval
tmax kb backup
```

Data root: `~/thundermax-kb` (override with `TMAX_KB_ROOT`).

## Absolute rules

- Never edit files in `01_originals/`.
- Never overwrite knowledge; supersede with a corrected record.
- Separate observation, interpretation, and conclusion.
- Keep unsupported files visible in `08_quarantine/` with a reason.
- Never fabricate map values from an unreadable encrypted binary.
- Never write `.tbw` files.

See `docs/THUNDERMAX_KB.md` in thundermax-assistant for the full design.
"""


def init_kb(root=None, seed=True):
    base = ensure_tree(root)
    con = connect(base)
    _seed_entities(con, base)
    readme = base / "README.md"
    if not readme.exists():
        readme.write_text(README_TEXT)
    for rel in (
        "00_inbox/unprocessed",
        "08_quarantine/corrupt",
        "04_knowledge/glossary",
    ):
        keep = base / rel / ".keep"
        if not keep.exists():
            keep.write_text("")
    con.commit()
    con.close()

    if seed:
        extra = {
            "vehicle_key": "2023-fxlrst",
            "engine_key": "131ci",
            "hardware_key": "ss550-63inj-2into1",
            "project_key": "shop",
            "validated": True,
            "confidence": "high",
            "tags": ["validated"],
        }
        rules = base / "04_knowledge" / "calibration_rules" / (
            "2026-08-27__shop__2023-fxlrst__131ci__house-rules__notes__v01.md"
        )
        if not rules.exists():
            rules.write_text(HOUSE_RULES_MD)
        r1 = ingest_path(rules, root=base, source="shop_seed", extra=extra, copy_original=True)
        ret = base / "04_knowledge" / "calibration_rules" / (
            "2026-08-27__shop__2023-fxlrst__131ci__retrieval-rules__notes__v01.md"
        )
        if not ret.exists():
            ret.write_text(RETRIEVAL_RULES)
        r2 = ingest_path(ret, root=base, source="shop_seed", extra=extra, copy_original=True)
        con = connect(base)
        file_id = r1.get("file_id")
        chunk_row = con.execute(
            "SELECT chunk_id FROM chunks WHERE file_id=? LIMIT 1", (file_id,)
        ).fetchone() if file_id else None
        chunk_id = chunk_row["chunk_id"] if chunk_row else None
        existing_n = con.execute("SELECT COUNT(*) FROM knowledge_claims").fetchone()[0]
        if existing_n == 0:
            for spec in HOUSE_CLAIMS:
                add_claim(
                    con=con,
                    claim=spec["claim"],
                    claim_type=spec["claim_type"],
                    status=spec["status"],
                    confidence=spec["confidence"],
                    scope=DEFAULT_SCOPE,
                    conditions=spec["conditions"],
                    evidence_summary=spec["evidence_summary"],
                    sources=[{"file_id": file_id, "chunk_id": chunk_id,
                              "note": "house calibration rules"}],
                    knowledge_tier=spec["knowledge_tier"],
                    validation_tested=spec.get("validation_tested", False),
                    validation_result="shop-validated" if spec.get("validation_tested") else None,
                )
        con.commit()
        con.close()
        _write_eval_questions(base)
        return {"root": str(base), "house_rules": r1, "retrieval_rules": r2}
    return {"root": str(base)}
