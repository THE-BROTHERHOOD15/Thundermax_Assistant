"""Hybrid retrieval: metadata filter → FTS5 → semantic → rerank → evidence bundle."""

import json
import math
import re
import sqlite3
from pathlib import Path

from kb.paths import kb_root
from kb.store import connect, status as kb_status

STOP = frozenset("""
the and for was are with that this what when where how why our your you did
does not can could should would will about say said from have has had get
its than then them they there their find show which all this these those
onto current bike onto the onto
""".split())

MAP_ID_RE = re.compile(r"\b[A-Z0-9]{12,16}\b")
FAULT_RE = re.compile(r"\bP[0-9]{4}\b", re.I)


def _profile_config(profile):
    profile = profile or {}
    return {
        "vehicle": profile.get("model") or profile.get("vehicle_key") or "2023-fxlrst",
        "vehicle_key": profile.get("vehicle_key") or "2023-fxlrst",
        "engine": profile.get("engine") or "131ci",
        "engine_key": profile.get("engine_key") or "131ci",
        "hardware_key": profile.get("hardware_key") or "ss550-63inj-2into1",
        "injectors": profile.get("injectors"),
        "exhaust": profile.get("exhaust"),
        "cam": profile.get("cam"),
        "ecm": profile.get("ecm"),
        "base_map_ids": profile.get("base_map_ids") or [],
        "setup_key": profile.get("setup_key"),
        "label": profile.get("label"),
    }


def parse_filters(question, profile=None):
    q = question or ""
    ql = q.lower()
    cfg = _profile_config(profile)
    filt = {}
    if re.search(r"\b(this\b.{0,20}\b(bike|vehicle)|my (bike|setup)|current (bike|setup|hardware)|exact vehicle)\b", ql):
        filt["vehicle_key"] = cfg["vehicle_key"]
        filt["engine_key"] = cfg["engine_key"]
        filt["hardware_key"] = cfg["hardware_key"]
    if "fxlrst" in ql or "low rider st" in ql or "lowrider st" in ql:
        filt["vehicle_key"] = "2023-fxlrst"
    if re.search(r"\b114ci\b", ql):
        filt["engine_key"] = "114ci"
    if re.search(r"\b131ci\b", ql) or "131 ci" in ql:
        filt["engine_key"] = "131ci"
    if "2-into-1" in ql or "2into1" in ql or "2 into 1" in ql:
        filt["hardware_like"] = "2-into-1"
    if "cold start" in ql or "cold-start" in ql:
        filt["tag"] = "cold-start"
    if "validated" in ql:
        filt["validated"] = 1
    mids = MAP_ID_RE.findall(q)
    if mids:
        filt["map_id"] = mids[0]
    faults = FAULT_RE.findall(q)
    if faults:
        filt["fault"] = faults[0].upper()
    return filt, cfg


def fts_query(question):
    terms = re.findall(r"[A-Za-z0-9][A-Za-z0-9._-]{1,}", question)
    exact, words = [], []
    for t in terms:
        if t.lower() in STOP:
            continue
        if MAP_ID_RE.fullmatch(t) or FAULT_RE.fullmatch(t):
            exact.append('"' + t.replace('"', "") + '"')
            continue
        safe = re.sub(r"[^\w.-]", "", t.lower())
        if len(safe) < 3:
            continue
        words.append('"' + safe.replace('"', "") + '"')
        if len(words) >= 10:
            break
    if exact:
        extra = " OR ".join(words[:6])
        return " AND ".join(exact) + (f" AND ({extra})" if extra else "")
    if not words:
        return None
    return " OR ".join(words[:8])


def _embed(text, timeout=15):
    import urllib.request
    body = json.dumps({"model": "nomic-embed-text", "prompt": text[:8000]}).encode()
    req = urllib.request.Request(
        "http://127.0.0.1:11434/api/embeddings",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())["embedding"]


def _cosine(a, b):
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def _metadata_candidates(con, filt, limit=80):
    sql = "SELECT file_id, vehicle_key, engine_key, hardware_key, validated, map_id, file_type, parser_status, quality_confidence FROM files WHERE parser_status NOT IN ('duplicate','corrupt')"
    args = []
    if filt.get("vehicle_key"):
        sql += " AND (vehicle_key=? OR vehicle_key IS NULL)"
        args.append(filt["vehicle_key"])
    if filt.get("engine_key"):
        sql += " AND (engine_key=? OR engine_key IS NULL)"
        args.append(filt["engine_key"])
    if filt.get("hardware_key"):
        sql += " AND (hardware_key=? OR hardware_key IS NULL)"
        args.append(filt["hardware_key"])
    if filt.get("validated") == 1:
        sql += " AND validated=1"
    if filt.get("map_id"):
        sql += " AND map_id=?"
        args.append(filt["map_id"])
    if filt.get("tag"):
        sql += " AND file_id IN (SELECT file_id FROM file_tags WHERE tag=?)"
        args.append(filt["tag"])
    sql += " LIMIT ?"
    args.append(limit)
    return {r["file_id"]: dict(r) for r in con.execute(sql, args)}


def _fts_hits(con, query, limit=40):
    if not query:
        return []
    try:
        rows = con.execute(
            "SELECT chunk_id, file_id, heading, text, bm25(chunks_fts) AS rank "
            "FROM chunks_fts WHERE chunks_fts MATCH ? ORDER BY rank LIMIT ?",
            (query, limit),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    return [dict(r) for r in rows]


def _semantic_hits(con, question, limit=20):
    n = con.execute("SELECT COUNT(*) FROM embedding_records").fetchone()[0]
    if not n:
        return []
    try:
        qvec = _embed(question)
    except Exception:
        return []
    out = []
    for row in con.execute("SELECT chunk_id, vector FROM embedding_records"):
        try:
            vec = json.loads(row["vector"])
        except json.JSONDecodeError:
            continue
        out.append((row["chunk_id"], _cosine(qvec, vec)))
    out.sort(key=lambda x: x[1], reverse=True)
    return out[:limit]


def _rerank(chunk, file_row, fts_rank, sem, filt, cfg, tags):
    score = 0.0
    vk = (file_row or {}).get("vehicle_key")
    ek = (file_row or {}).get("engine_key")
    hk = (file_row or {}).get("hardware_key")
    same_vehicle = vk and vk == (filt.get("vehicle_key") or cfg.get("vehicle_key"))
    same_hw = hk and hk == (filt.get("hardware_key") or cfg.get("hardware_key"))
    same_engine = ek and ek == (filt.get("engine_key") or cfg.get("engine_key"))
    if same_vehicle and same_hw:
        score += 50
        bucket = 1
    elif same_engine and same_hw:
        score += 35
        bucket = 2
    elif same_engine:
        score += 20
        bucket = 2
    else:
        bucket = 5
    if (file_row or {}).get("validated"):
        score += 30
        if bucket > 3:
            bucket = 3
    if "validated" in tags:
        score += 8
    if "needs-validation" in tags:
        score -= 10
        if bucket < 6:
            bucket = max(bucket, 6)
    if "superseded" in tags or "disputed" in tags:
        score -= 20
    if fts_rank is not None:
        # bm25 is typically negative; less-negative is better
        score += max(0, 12 + (-fts_rank if fts_rank < 0 else fts_rank) * 0.2)
    if sem:
        score += 20 * sem
        if sem > 0.45 and bucket > 4:
            bucket = 4
    chain = {}
    try:
        chain = json.loads(chunk.get("metadata_json") or "{}").get("source_chain") or {}
    except json.JSONDecodeError:
        chain = {}
    if not (chain.get("file_id") and chain.get("archive_path") or chain.get("file_id")):
        score -= 15
    return score, bucket


def retrieve_bundle(question, profile=None, root=None, k=12, embed=True):
    """Return an evidence bundle for the language model."""
    missing = []
    degraded = []
    cfg = _profile_config(profile)
    filt, cfg = parse_filters(question, profile=profile)
    if not Path(kb_root(root) / "database" / "knowledge.sqlite").exists():
        return {
            "current_config": cfg,
            "filters": filt,
            "citations": [],
            "claims": [],
            "contradictions": [],
            "missing": ["knowledge base is not initialized (tmax kb init)"],
            "degraded": ["kb missing"],
            "context": "",
        }
    con = connect(root, create=False)
    try:
        files = _metadata_candidates(con, filt)
        if not files and (filt.get("vehicle_key") or filt.get("engine_key")):
            missing.append("no files matched the metadata filter; falling back to unfiltered search")
            files = _metadata_candidates(con, {}, limit=200)

        q = fts_query(question)
        fts = _fts_hits(con, q) if q else []
        if not fts:
            degraded.append("FTS5 returned no hits")

        sem_map = {}
        if embed:
            try:
                for cid, sc in _semantic_hits(con, question):
                    sem_map[cid] = sc
            except Exception as e:
                degraded.append(f"semantic search unavailable ({e.__class__.__name__})")

        by_chunk = {}
        for hit in fts:
            by_chunk[hit["chunk_id"]] = {"fts": hit, "sem": sem_map.get(hit["chunk_id"], 0.0)}
        for cid, sc in sem_map.items():
            by_chunk.setdefault(cid, {"fts": None, "sem": sc})

        scored = []
        for cid, legs in by_chunk.items():
            row = con.execute("SELECT * FROM chunks WHERE chunk_id=?", (cid,)).fetchone()
            if not row:
                continue
            chunk = dict(row)
            if files and chunk["file_id"] not in files and filt:
                # metadata filter: keep only matching files, unless no filter was applied
                if filt.get("vehicle_key") or filt.get("map_id") or filt.get("validated"):
                    continue
            full = con.execute(
                "SELECT * FROM files WHERE file_id=?", (chunk["file_id"],)
            ).fetchone()
            frow = dict(full) if full else (files.get(chunk["file_id"]) or {})
            ql = question.lower()
            if (frow or {}).get("parser_status") in {"unsupported", "corrupt", "suspicious"}:
                if not any(w in ql for w in ("unsupported", "unreadable", "quarantine", "encrypted")):
                    continue
            tags = [r[0] for r in con.execute(
                "SELECT tag FROM file_tags WHERE file_id=?", (chunk["file_id"],)
            )]
            fts_rank = None
            if legs["fts"]:
                fts_rank = legs["fts"].get("rank")
            score, bucket = _rerank(chunk, frow, fts_rank, legs["sem"], filt, cfg, tags)
            chain = {}
            try:
                chain = json.loads(chunk.get("metadata_json") or "{}").get("source_chain") or {}
            except json.JSONDecodeError:
                pass
            scored.append({
                "score": score,
                "bucket": bucket,
                "chunk_id": cid,
                "file_id": chunk["file_id"],
                "heading": chunk.get("heading"),
                "text": chunk["text"],
                "file_type": frow.get("file_type"),
                "vehicle_key": frow.get("vehicle_key"),
                "engine_key": frow.get("engine_key"),
                "hardware_key": frow.get("hardware_key"),
                "validated": bool(frow.get("validated")),
                "map_id": frow.get("map_id"),
                "archive_path": chain.get("archive_path") or frow.get("archive_path"),
                "derivative_path": chain.get("derivative_path") or frow.get("derivative_path"),
                "parser_status": frow.get("parser_status"),
                "knowledge_tier": frow.get("knowledge_tier"),
                "tags": tags,
                "retriever": "kb-hybrid",
            })
        scored.sort(key=lambda r: (-r["score"], r["bucket"]))
        top = scored[:k]
        citations = []
        for i, h in enumerate(top, start=1):
            citations.append({"n": i, **h, "source": h["file_id"]})

        claims = []
        for r in con.execute(
            "SELECT * FROM knowledge_claims ORDER BY knowledge_tier DESC, created_at DESC LIMIT 30"
        ):
            d = dict(r)
            scope = {}
            try:
                scope = json.loads(d.get("scope_json") or "{}")
            except json.JSONDecodeError:
                pass
            blob = (d["claim"] + " " + (d.get("conditions") or "") + " " + (d.get("evidence_summary") or "")).lower()
            qwords = {w for w in re.findall(r"[a-z0-9]{3,}", question.lower()) if w not in STOP}
            hits = sum(1 for w in qwords if w in blob)
            if hits >= 1:
                srcs = [dict(s) for s in con.execute(
                    "SELECT file_id, chunk_id, note FROM knowledge_sources WHERE claim_id=?",
                    (d["claim_id"],)
                )]
                d["sources"] = srcs
                d["scope"] = scope
                claims.append(d)
        claims = claims[:8]

        contr = [dict(r) for r in con.execute(
            "SELECT claim_id, claim, status, correction_reason, superseded_by "
            "FROM knowledge_claims WHERE status IN ('disputed','superseded')"
        )]

        if not citations and not claims:
            missing.append("no sufficiently similar evidence in the knowledge base")
        if not any(c.get("validated") for c in citations) and "validated" in question.lower():
            missing.append("no validated files matched this question")
        if filt.get("fault") and not any(filt["fault"].lower() in (c["text"] or "").lower() for c in citations):
            missing.append(f"no evidence involving fault code {filt['fault']}")

        ctx_parts = []
        if cfg.get("label"):
            ctx_parts.append(f"Current configuration: {cfg['label']}")
        for c in citations:
            chain = f"{c['file_id']} / {c['chunk_id']}"
            if c.get("archive_path"):
                chain += f" / {c['archive_path']}"
            flag = []
            if c.get("validated"):
                flag.append("validated")
            else:
                flag.append("unvalidated")
            if c.get("knowledge_tier") is not None:
                flag.append(f"tier {c['knowledge_tier']}")
            ctx_parts.append(
                f"[{c['n']}] ({c['file_id']} chunk {c['chunk_id']}; {', '.join(flag)})\n"
                f"source-chain: {chain}\n{c['text']}"
            )
        if claims:
            ctx_parts.append("Claims:")
            for cl in claims:
                ctx_parts.append(
                    f"- {cl['claim_id']} [{cl['status']}/{cl['confidence']}/tier {cl['knowledge_tier']}]: "
                    f"{cl['claim']}"
                )
        if contr:
            ctx_parts.append("Contradictions / superseded:")
            for cl in contr[:6]:
                ctx_parts.append(
                    f"- {cl['claim_id']} {cl['status']}: {cl['claim']} "
                    f"({cl.get('correction_reason') or ''})"
                )
        if missing:
            ctx_parts.append("Missing information: " + "; ".join(missing))

        return {
            "current_config": cfg,
            "filters": filt,
            "citations": citations,
            "claims": claims,
            "contradictions": contr,
            "missing": missing,
            "degraded": degraded,
            "context": "\n\n".join(ctx_parts),
            "kb": kb_status(root),
        }
    finally:
        con.close()


def retrieve_context(question, profile=None, root=None, budget=24000, embed=True):
    bundle = retrieve_bundle(question, profile=profile, root=root, embed=embed)
    ctx = bundle.get("context") or ""
    if len(ctx) > budget:
        ctx = ctx[:budget]
    return ctx


def citations_for_ui(question, profile=None, root=None):
    bundle = retrieve_bundle(question, profile=profile, root=root)
    out = []
    for c in bundle.get("citations") or []:
        out.append({
            "text": c["text"],
            "source": c.get("file_id") or c.get("source"),
            "path": c.get("archive_path") or "",
            "retriever": "kb-hybrid",
            "score": c.get("score"),
            "chunk_id": c.get("chunk_id"),
            "file_id": c.get("file_id"),
        })
    return out, bundle
