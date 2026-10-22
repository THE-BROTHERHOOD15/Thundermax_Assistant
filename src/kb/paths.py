"""Live archive layout. Data lives outside the git repo by default."""

import os
from pathlib import Path

DEFAULT_ROOT = Path.home() / "thundermax-kb"

TREE = (
    "00_inbox/unprocessed",
    "00_inbox/needs_review",
    "01_originals/tbw",
    "01_originals/official_exports",
    "01_originals/maps",
    "01_originals/datalogs",
    "01_originals/manuals",
    "01_originals/screenshots",
    "01_originals/other",
    "02_normalized/map_tables",
    "02_normalized/log_tables",
    "02_normalized/extracted_text",
    "02_normalized/extracted_tables",
    "02_normalized/ocr_text",
    "02_normalized/image_descriptions",
    "03_entities/vehicles",
    "03_entities/engines",
    "03_entities/hardware",
    "03_entities/sensors",
    "03_entities/fuels",
    "03_entities/firmware",
    "03_entities/owners_or_projects",
    "04_knowledge/calibration_rules",
    "04_knowledge/verified_patterns",
    "04_knowledge/historical_notes",
    "04_knowledge/troubleshooting",
    "04_knowledge/glossary",
    "04_knowledge/contradictions",
    "05_analysis/comparisons",
    "05_analysis/tuning_sessions",
    "05_analysis/change_manifests",
    "05_analysis/validation_reports",
    "05_analysis/rollback_records",
    "06_retrieval/chunks",
    "06_retrieval/embeddings",
    "06_retrieval/indexes",
    "06_retrieval/exports",
    "07_evaluations/questions",
    "07_evaluations/expected_answers",
    "07_evaluations/retrieval_tests",
    "07_evaluations/regression_results",
    "08_quarantine/corrupt",
    "08_quarantine/unsupported",
    "08_quarantine/duplicates",
    "08_quarantine/suspicious",
    "database/backups",
    "manifests",
)

ORIGINAL_SUBDIR = {
    "tbw": "01_originals/tbw",
    "official_export": "01_originals/official_exports",
    "map": "01_originals/maps",
    "datalog": "01_originals/datalogs",
    "manual": "01_originals/manuals",
    "document": "01_originals/manuals",
    "screenshot": "01_originals/screenshots",
    "image": "01_originals/screenshots",
    "notes": "01_originals/other",
    "extracted_text": "01_originals/other",
    "other": "01_originals/other",
}

CONTROLLED_TAGS = (
    "baseline",
    "validated",
    "needs-validation",
    "superseded",
    "disputed",
    "cold-start",
    "hot-start",
    "idle",
    "cruise",
    "acceleration",
    "deceleration",
    "high-load",
    "fueling",
    "ignition",
    "temperature-compensation",
    "sensor",
    "fault-code",
    "intake",
    "exhaust",
    "camshaft",
    "injectors",
    "firmware",
    "map-comparison",
    "rollback",
)


def kb_root(root=None):
    if root is not None:
        return Path(root)
    env = os.environ.get("TMAX_KB_ROOT")
    return Path(env) if env else DEFAULT_ROOT


def kb_db(root=None):
    return kb_root(root) / "database" / "knowledge.sqlite"


def ensure_tree(root=None):
    base = kb_root(root)
    for rel in TREE:
        (base / rel).mkdir(parents=True, exist_ok=True)
    return base
