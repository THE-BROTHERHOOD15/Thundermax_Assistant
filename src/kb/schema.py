"""SQLite schema for the ThunderMax knowledge base."""

SCHEMA_SQL = r"""
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS id_counters (
  kind TEXT PRIMARY KEY,
  n INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS vehicles (
  id INTEGER PRIMARY KEY,
  vehicle_key TEXT UNIQUE NOT NULL,
  year INTEGER,
  make TEXT,
  model TEXT,
  engine_displacement TEXT,
  notes TEXT
);

CREATE TABLE IF NOT EXISTS engines (
  id INTEGER PRIMARY KEY,
  engine_key TEXT UNIQUE NOT NULL,
  family TEXT,
  displacement TEXT,
  camshaft TEXT,
  notes TEXT
);

CREATE TABLE IF NOT EXISTS hardware_configs (
  id INTEGER PRIMARY KEY,
  hardware_key TEXT UNIQUE NOT NULL,
  intake TEXT,
  exhaust TEXT,
  camshaft TEXT,
  injectors TEXT,
  notes TEXT
);

CREATE TABLE IF NOT EXISTS fuel_configs (
  id INTEGER PRIMARY KEY,
  fuel_key TEXT UNIQUE NOT NULL,
  type TEXT,
  octane INTEGER,
  ethanol_percent REAL
);

CREATE TABLE IF NOT EXISTS thundermax_configs (
  id INTEGER PRIMARY KEY,
  tmax_key TEXT UNIQUE NOT NULL,
  module TEXT,
  firmware TEXT,
  map_id TEXT,
  software_version TEXT
);

CREATE TABLE IF NOT EXISTS files (
  id INTEGER PRIMARY KEY,
  file_id TEXT UNIQUE NOT NULL,
  original_path TEXT NOT NULL,
  source_path TEXT,
  archive_path TEXT,
  derivative_path TEXT,
  sha256 TEXT NOT NULL,
  file_type TEXT NOT NULL,
  parser_status TEXT NOT NULL,
  source TEXT,
  created_at TEXT NOT NULL,
  ingested_at TEXT NOT NULL,
  mime TEXT,
  size_bytes INTEGER,
  vehicle_key TEXT,
  engine_key TEXT,
  hardware_key TEXT,
  fuel_key TEXT,
  tmax_key TEXT,
  project_key TEXT,
  map_id TEXT,
  quality_confidence TEXT DEFAULT 'medium',
  validated INTEGER DEFAULT 0,
  validation_reference TEXT,
  knowledge_tier INTEGER DEFAULT 0,
  duplicate_of TEXT,
  quarantine_reason TEXT,
  metadata_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_files_sha ON files(sha256);
CREATE INDEX IF NOT EXISTS idx_files_type ON files(file_type);
CREATE INDEX IF NOT EXISTS idx_files_vehicle ON files(vehicle_key);
CREATE INDEX IF NOT EXISTS idx_files_map ON files(map_id);
CREATE INDEX IF NOT EXISTS idx_files_status ON files(parser_status);

CREATE TABLE IF NOT EXISTS logs (
  id INTEGER PRIMARY KEY,
  file_id TEXT NOT NULL,
  segment TEXT,
  row_start INTEGER,
  row_end INTEGER,
  summary TEXT
);

CREATE TABLE IF NOT EXISTS maps (
  id INTEGER PRIMARY KEY,
  file_id TEXT NOT NULL,
  map_id TEXT,
  table_name TEXT,
  units TEXT,
  rpm_axis TEXT,
  load_axis TEXT,
  summary TEXT,
  normalized_path TEXT,
  parser_status TEXT
);

CREATE TABLE IF NOT EXISTS observations (
  id INTEGER PRIMARY KEY,
  observation_id TEXT UNIQUE NOT NULL,
  file_id TEXT,
  chunk_id TEXT,
  text TEXT NOT NULL,
  kind TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS knowledge_claims (
  id INTEGER PRIMARY KEY,
  claim_id TEXT UNIQUE NOT NULL,
  claim TEXT NOT NULL,
  claim_type TEXT NOT NULL,
  status TEXT NOT NULL,
  confidence TEXT,
  scope_json TEXT,
  conditions TEXT,
  evidence_summary TEXT,
  validation_tested INTEGER DEFAULT 0,
  validation_result TEXT,
  superseded_by TEXT,
  supersedes TEXT,
  correction_reason TEXT,
  created_at TEXT NOT NULL,
  knowledge_tier INTEGER DEFAULT 3
);

CREATE TABLE IF NOT EXISTS knowledge_sources (
  id INTEGER PRIMARY KEY,
  claim_id TEXT NOT NULL,
  file_id TEXT,
  chunk_id TEXT,
  note TEXT
);

CREATE TABLE IF NOT EXISTS chunks (
  id INTEGER PRIMARY KEY,
  chunk_id TEXT UNIQUE NOT NULL,
  file_id TEXT NOT NULL,
  derivative_path TEXT,
  chunk_kind TEXT,
  heading TEXT,
  text TEXT NOT NULL,
  token_estimate INTEGER,
  row_start INTEGER,
  row_end INTEGER,
  vehicle_key TEXT,
  project_key TEXT,
  date TEXT,
  metadata_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_chunks_file ON chunks(file_id);

CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
  chunk_id UNINDEXED,
  file_id UNINDEXED,
  heading,
  text,
  tags,
  tokenize = 'porter unicode61'
);

CREATE TABLE IF NOT EXISTS embedding_records (
  id INTEGER PRIMARY KEY,
  chunk_id TEXT UNIQUE NOT NULL,
  model TEXT NOT NULL,
  dim INTEGER NOT NULL,
  vector TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS analysis_runs (
  id INTEGER PRIMARY KEY,
  run_id TEXT UNIQUE NOT NULL,
  kind TEXT,
  created_at TEXT,
  inputs_json TEXT,
  result_path TEXT,
  notes TEXT
);

CREATE TABLE IF NOT EXISTS change_manifests (
  id INTEGER PRIMARY KEY,
  manifest_id TEXT UNIQUE NOT NULL,
  from_file_id TEXT,
  to_file_id TEXT,
  summary TEXT,
  created_at TEXT
);

CREATE TABLE IF NOT EXISTS validation_runs (
  id INTEGER PRIMARY KEY,
  run_id TEXT UNIQUE NOT NULL,
  file_id TEXT,
  claim_id TEXT,
  result TEXT,
  created_at TEXT,
  notes TEXT
);

CREATE TABLE IF NOT EXISTS tags (
  tag TEXT PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS file_tags (
  file_id TEXT NOT NULL,
  tag TEXT NOT NULL,
  PRIMARY KEY (file_id, tag)
);

CREATE TABLE IF NOT EXISTS import_runs (
  id INTEGER PRIMARY KEY,
  run_id TEXT UNIQUE NOT NULL,
  started_at TEXT,
  finished_at TEXT,
  source TEXT,
  files_seen INTEGER,
  files_ok INTEGER,
  files_quarantine INTEGER,
  files_duplicate INTEGER,
  notes TEXT
);

CREATE VIEW IF NOT EXISTS v_same_bike_engine_exhaust AS
SELECT f.* FROM files f
WHERE f.parser_status NOT IN ('duplicate', 'corrupt')
  AND f.vehicle_key IS NOT NULL
  AND f.engine_key IS NOT NULL
  AND f.hardware_key IS NOT NULL;

CREATE VIEW IF NOT EXISTS v_validated_maps AS
SELECT f.* FROM files f
WHERE f.file_type IN ('tbw', 'map', 'official_export')
  AND f.validated = 1
  AND f.parser_status = 'parsed';

CREATE VIEW IF NOT EXISTS v_logs_lean_rich AS
SELECT f.*, t.tag FROM files f
JOIN file_tags t ON t.file_id = f.file_id
WHERE f.file_type = 'datalog' AND t.tag IN ('fueling');

CREATE VIEW IF NOT EXISTS v_cold_start AS
SELECT f.* FROM files f
JOIN file_tags t ON t.file_id = f.file_id
WHERE t.tag = 'cold-start';

CREATE VIEW IF NOT EXISTS v_ignition_changes AS
SELECT f.* FROM files f
JOIN file_tags t ON t.file_id = f.file_id
WHERE t.tag = 'ignition';

CREATE VIEW IF NOT EXISTS v_by_project AS
SELECT project_key, COUNT(*) AS n FROM files
GROUP BY project_key;

CREATE VIEW IF NOT EXISTS v_quarantine AS
SELECT * FROM files
WHERE parser_status IN ('corrupt', 'unsupported', 'duplicate', 'suspicious')
   OR quarantine_reason IS NOT NULL;

CREATE VIEW IF NOT EXISTS v_claims_awaiting_validation AS
SELECT * FROM knowledge_claims
WHERE status IN ('observed', 'inferred')
  AND IFNULL(validation_tested, 0) = 0;

CREATE VIEW IF NOT EXISTS v_contradictions AS
SELECT * FROM knowledge_claims WHERE status IN ('disputed', 'superseded');

CREATE VIEW IF NOT EXISTS v_superseded_maps AS
SELECT f.* FROM files f
JOIN file_tags t ON t.file_id = f.file_id
WHERE t.tag = 'superseded';
"""
