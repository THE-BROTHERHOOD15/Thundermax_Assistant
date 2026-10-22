"""Knowledge claims. Never silently overwrite; supersede with a new record."""

import json
from datetime import datetime, timezone

from kb.store import connect, next_id, utcnow


def add_claim(con=None, root=None, claim="", claim_type="observation",
              status="observed", confidence="medium", scope=None, conditions="",
              evidence_summary="", sources=None, knowledge_tier=3,
              validation_tested=False, validation_result=None):
    own = con is None
    if own:
        con = connect(root)
    try:
        claim_id = next_id(con, "claim")
        con.execute(
            """INSERT INTO knowledge_claims(
                 claim_id, claim, claim_type, status, confidence, scope_json,
                 conditions, evidence_summary, validation_tested, validation_result,
                 created_at, knowledge_tier)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                claim_id, claim, claim_type, status, confidence,
                json.dumps(scope or {}, ensure_ascii=False),
                conditions, evidence_summary,
                1 if validation_tested else 0, validation_result,
                utcnow(), knowledge_tier,
            ),
        )
        for src in sources or []:
            con.execute(
                """INSERT INTO knowledge_sources(claim_id, file_id, chunk_id, note)
                   VALUES (?,?,?,?)""",
                (claim_id, src.get("file_id"), src.get("chunk_id"), src.get("note")),
            )
        if own:
            con.commit()
        return claim_id
    finally:
        if own:
            con.close()


def supersede_claim(old_claim_id, new_claim, reason, evidence="",
                    confidence="medium", sources=None, root=None, con=None):
    """Mark an old note wrong without deleting it. Creates a corrected record."""
    own = con is None
    if own:
        con = connect(root)
    try:
        old = con.execute(
            "SELECT * FROM knowledge_claims WHERE claim_id=?", (old_claim_id,)
        ).fetchone()
        if not old:
            raise KeyError(f"unknown claim {old_claim_id}")
        new_id = add_claim(
            con=con,
            claim=new_claim,
            claim_type=old["claim_type"],
            status="validated" if old["status"] == "validated" else "inferred",
            confidence=confidence,
            scope=json.loads(old["scope_json"] or "{}"),
            conditions=old["conditions"],
            evidence_summary=evidence or reason,
            sources=sources,
            knowledge_tier=old["knowledge_tier"],
        )
        con.execute(
            """UPDATE knowledge_claims
               SET status='superseded', superseded_by=?, correction_reason=?
               WHERE claim_id=?""",
            (new_id, reason, old_claim_id),
        )
        con.execute(
            "UPDATE knowledge_claims SET supersedes=? WHERE claim_id=?",
            (old_claim_id, new_id),
        )
        if own:
            con.commit()
        return new_id
    finally:
        if own:
            con.close()


def claims_for_query(con, vehicle_key=None, engine_key=None, status=None, limit=20):
    sql = "SELECT * FROM knowledge_claims WHERE 1=1"
    args = []
    if status:
        sql += " AND status=?"
        args.append(status)
    if vehicle_key:
        sql += " AND scope_json LIKE ?"
        args.append(f"%{vehicle_key}%")
    if engine_key:
        sql += " AND scope_json LIKE ?"
        args.append(f"%{engine_key}%")
    sql += " ORDER BY knowledge_tier DESC, created_at DESC LIMIT ?"
    args.append(limit)
    return [dict(r) for r in con.execute(sql, args)]


def contradictions(con):
    return [dict(r) for r in con.execute(
        "SELECT * FROM knowledge_claims WHERE status IN ('disputed','superseded')"
    )]
