#!/usr/bin/env python3
"""tmax kb — three-layer knowledge base CLI. Never writes .tbw files."""

import argparse
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from kb.claims import add_claim, supersede_claim  # noqa: E402
from kb.eval_questions import QUESTIONS  # noqa: E402
from kb.ingest import init_kb, ingest_path, ingest_tree  # noqa: E402
from kb.paths import kb_db, kb_root  # noqa: E402
from kb.retrieve import retrieve_bundle  # noqa: E402
from kb.store import connect, status  # noqa: E402


def cmd_init(args):
    out = init_kb(root=args.root, seed=not args.no_seed)
    print(json.dumps(out, indent=2, default=str))
    print(f"KB root: {kb_root(args.root)}")
    return 0


def cmd_status(args):
    print(json.dumps(status(args.root), indent=2))
    return 0


def _extra(args):
    extra = {}
    for key in ("vehicle_key", "engine_key", "hardware_key", "project_key", "map_id"):
        val = getattr(args, key, None)
        if val:
            extra[key] = val
    if getattr(args, "validated", False):
        extra["validated"] = True
    if getattr(args, "tags", None):
        extra["tags"] = [t.strip() for t in args.tags.split(",") if t.strip()]
    return extra


def cmd_ingest(args):
    extra = _extra(args)
    src = Path(args.path)
    if src.is_dir() or (src.is_file() is False and "*" in args.path):
        report = ingest_tree(src, root=args.root, source=args.source,
                             extra=extra, embed=args.embed)
        slim = {k: report[k] for k in report if k != "results"}
        print(json.dumps(slim, indent=2))
        print(f"results: {len(report.get('results') or [])} files")
        return 0
    r = ingest_path(src, root=args.root, source=args.source, extra=extra, embed=args.embed)
    print(json.dumps(r, indent=2, default=str))
    return 0 if r.get("ok") else 1


def cmd_retrieve(args):
    profile = None
    prof_path = ROOT / "bike_profile.json"
    if prof_path.exists():
        profile = json.loads(prof_path.read_text())
        profile.setdefault("vehicle_key", "2023-fxlrst")
        profile.setdefault("engine_key", "131ci")
        profile.setdefault("hardware_key", "ss550-63inj-2into1")
    bundle = retrieve_bundle(args.question, profile=profile, root=args.root,
                             embed=not args.no_embed)
    if args.json:
        slim = dict(bundle)
        slim.pop("context", None)
        print(json.dumps(slim, indent=2, default=str))
        return 0
    print(bundle.get("context") or "(empty bundle)")
    if bundle.get("missing"):
        print("\nMISSING:", "; ".join(bundle["missing"]))
    if bundle.get("degraded"):
        print("DEGRADED:", "; ".join(bundle["degraded"]))
    return 0


def cmd_claim(args):
    if args.supersede:
        new_id = supersede_claim(
            args.supersede, args.text, args.reason or "corrected",
            evidence=args.evidence or "", confidence=args.confidence,
            root=args.root,
        )
        print(new_id)
        return 0
    cid = add_claim(
        root=args.root,
        claim=args.text,
        claim_type=args.type,
        status=args.status,
        confidence=args.confidence,
        scope={"vehicle": args.vehicle_key, "engine": args.engine_key,
               "hardware": args.hardware_key},
        conditions=args.conditions or "",
        evidence_summary=args.evidence or "",
        knowledge_tier=args.tier,
    )
    print(cid)
    return 0


def cmd_views(args):
    con = connect(args.root, create=False)
    names = [r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='view' ORDER BY name"
    )]
    if args.name:
        if args.name not in names:
            print(f"unknown view {args.name}. have: {', '.join(names)}", file=sys.stderr)
            return 1
        rows = [dict(r) for r in con.execute(f"SELECT * FROM {args.name} LIMIT 50")]
        print(json.dumps(rows, indent=2, default=str))
        return 0
    for n in names:
        c = con.execute(f"SELECT COUNT(*) FROM {n}").fetchone()[0]
        print(f"{n}\t{c}")
    con.close()
    return 0


def cmd_eval(args):
    profile = {
        "vehicle_key": "2023-fxlrst",
        "engine_key": "131ci",
        "hardware_key": "ss550-63inj-2into1",
        "label": "eval profile",
    }
    results = []
    for q in QUESTIONS:
        bundle = retrieve_bundle(q["question"], profile=profile, root=args.root,
                                 embed=not args.no_embed)
        kinds = {c.get("file_type") for c in bundle.get("citations") or []}
        has_claim = bool(bundle.get("claims"))
        top3 = bundle.get("citations") or []
        ok_cite = bool(top3) or has_claim or bool(bundle.get("missing"))
        results.append({
            "id": q["id"],
            "question": q["question"],
            "top_file_ids": [c.get("file_id") for c in top3[:3]],
            "kinds": sorted(k for k in kinds if k),
            "claims": [c.get("claim_id") for c in bundle.get("claims") or []],
            "missing": bundle.get("missing"),
            "ok_cite": ok_cite,
            "expected": q.get("expected"),
        })
    dest = kb_root(args.root) / "07_evaluations" / "retrieval_tests" / (
        datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + ".json"
    )
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(results, indent=2) + "\n")
    n_ok = sum(1 for r in results if r["ok_cite"])
    print(f"{n_ok}/{len(results)} questions produced citations, claims, or an explicit missing-evidence note")
    print(f"wrote {dest}")
    if args.verbose:
        print(json.dumps(results, indent=2))
    return 0



def cmd_embed(args):
    """Backfill nomic-embed-text vectors for chunks that have none."""
    from kb.ingest import _embed_chunk
    from kb.store import utcnow
    con = connect(args.root)
    rows = con.execute(
        "SELECT c.chunk_id, c.text FROM chunks c "
        "LEFT JOIN embedding_records e ON e.chunk_id = c.chunk_id "
        "WHERE e.chunk_id IS NULL"
    ).fetchall()
    ok = fail = 0
    for row in rows:
        try:
            vec = _embed_chunk(row["text"])
            con.execute(
                "INSERT INTO embedding_records(chunk_id, model, dim, vector, created_at) "
                "VALUES (?,?,?,?,?)",
                (row["chunk_id"], "nomic-embed-text", len(vec), json.dumps(vec), utcnow()),
            )
            ok += 1
            if ok % 25 == 0:
                con.commit()
                print(f"embedded {ok}/{len(rows)}", flush=True)
        except Exception as e:
            fail += 1
            if fail <= 5:
                print(f"embed fail {row['chunk_id']}: {e}", file=sys.stderr)
    con.commit()
    con.close()
    print(json.dumps({"embedded": ok, "failed": fail, "considered": len(rows)}))
    return 0 if fail == 0 or ok else 1


def cmd_backup(args):
    root = kb_root(args.root)
    db = kb_db(args.root)
    dest_dir = root / "database" / "backups"
    dest_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    if db.exists():
        shutil.copy2(db, dest_dir / f"knowledge-{stamp}.sqlite")
    man = root / "manifests" / "file_manifest.jsonl"
    if man.exists():
        shutil.copy2(man, dest_dir / f"file_manifest-{stamp}.jsonl")
    print(f"backup -> {dest_dir / ('knowledge-' + stamp + '.sqlite')}")
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(prog="tmax kb", description="ThunderMax three-layer knowledge base")
    p.add_argument("--root", default=None, help="KB root (default: $TMAX_KB_ROOT or ~/thundermax-kb)")
    sub = p.add_subparsers(dest="cmd", required=True)

    pi = sub.add_parser("init", help="create directory tree + sqlite + seed house rules")
    pi.add_argument("--no-seed", action="store_true")
    pi.set_defaults(func=cmd_init)

    ps = sub.add_parser("status", help="counts")
    ps.set_defaults(func=cmd_status)

    pg = sub.add_parser("ingest", help="ingest a file or folder (copy; never writes .tbw)")
    pg.add_argument("path")
    pg.add_argument("--source", default="user_archive")
    pg.add_argument("--embed", action="store_true", help="also embed with nomic-embed-text")
    pg.add_argument("--vehicle-key")
    pg.add_argument("--engine-key")
    pg.add_argument("--hardware-key")
    pg.add_argument("--project-key")
    pg.add_argument("--map-id")
    pg.add_argument("--validated", action="store_true")
    pg.add_argument("--tags", help="comma-separated controlled tags")
    pg.set_defaults(func=cmd_ingest)

    pr = sub.add_parser("retrieve", help="hybrid retrieve an evidence bundle")
    pr.add_argument("question")
    pr.add_argument("--json", action="store_true")
    pr.add_argument("--no-embed", action="store_true")
    pr.set_defaults(func=cmd_retrieve)

    pc = sub.add_parser("claim", help="add or supersede a knowledge claim")
    pc.add_argument("--text", required=True)
    pc.add_argument("--type", default="observation")
    pc.add_argument("--status", default="observed")
    pc.add_argument("--confidence", default="medium")
    pc.add_argument("--conditions")
    pc.add_argument("--evidence")
    pc.add_argument("--tier", type=int, default=3)
    pc.add_argument("--vehicle-key")
    pc.add_argument("--engine-key")
    pc.add_argument("--hardware-key")
    pc.add_argument("--supersede", help="existing claim_id to mark superseded")
    pc.add_argument("--reason")
    pc.set_defaults(func=cmd_claim)

    pv = sub.add_parser("views", help="list or dump a saved SQL view")
    pv.add_argument("name", nargs="?")
    pv.set_defaults(func=cmd_views)

    pe = sub.add_parser("eval", help="run the 25-question retrieval suite")
    pe.add_argument("--no-embed", action="store_true")
    pe.add_argument("--verbose", action="store_true")
    pe.set_defaults(func=cmd_eval)

    pe2 = sub.add_parser("embed", help="backfill nomic-embed-text for chunks missing vectors")
    pe2.set_defaults(func=cmd_embed)

    pb = sub.add_parser("backup", help="snapshot sqlite + manifests")
    pb.set_defaults(func=cmd_backup)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
