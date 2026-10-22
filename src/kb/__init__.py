"""ThunderMax three-layer knowledge base.

Filesystem originals stay immutable. SQLite organizes exact lookup (FTS5).
Optional nomic-embed-text vectors add meaning-based retrieval. A retrieved
answer must be able to point:

    answer → knowledge claim → source chunk → normalized derivative → original file

If that chain is missing, confidence drops. Never writes .tbw files.
"""

from kb.paths import TREE, kb_root
from kb.store import connect, status
from kb.ingest import ingest_path, ingest_tree, init_kb
from kb.retrieve import retrieve_bundle, retrieve_context
from kb.claims import add_claim, supersede_claim

__all__ = [
    "TREE",
    "add_claim",
    "connect",
    "init_kb",
    "ingest_path",
    "ingest_tree",
    "kb_root",
    "retrieve_bundle",
    "retrieve_context",
    "status",
    "supersede_claim",
]
