ALTER TABLE document_chunks ADD COLUMN content_hash TEXT;
ALTER TABLE document_chunks ADD COLUMN source_version_id TEXT;

CREATE TABLE acw_schema_version (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);

CREATE TABLE acw_source_versions (
  id TEXT PRIMARY KEY,
  source_id TEXT NOT NULL REFERENCES documents(id),
  version_hash TEXT NOT NULL,
  seen_at TEXT NOT NULL,
  source_date TEXT,
  source_date_origin TEXT CHECK (source_date_origin IN ('content','user','unknown')),
  UNIQUE(source_id, version_hash)
);

CREATE TABLE acw_chunk_ledger (
  id TEXT PRIMARY KEY,
  source_id TEXT NOT NULL REFERENCES documents(id),
  source_version_id TEXT NOT NULL REFERENCES acw_source_versions(id),
  content_hash TEXT NOT NULL,
  document_chunk_id TEXT,
  ordinal INTEGER NOT NULL,
  disposition TEXT NOT NULL CHECK (disposition IN
    ('pending','placed','duplicate','irrelevant','conflicted_pending',
     'failed','failed_final','superseded')),
  disposition_reason TEXT,
  duplicate_of_block_id TEXT,
  attempts INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT NOT NULL,
  UNIQUE(source_id, source_version_id, content_hash, ordinal)
);

CREATE TABLE acw_pages (
  id TEXT PRIMARY KEY,
  path TEXT NOT NULL UNIQUE,
  title TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'active',
  domain TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);
CREATE TABLE acw_page_aliases (page_id TEXT NOT NULL REFERENCES acw_pages(id),
  alias TEXT NOT NULL, UNIQUE(page_id, alias));

CREATE TABLE acw_blocks (
  id TEXT PRIMARY KEY,
  page_id TEXT NOT NULL REFERENCES acw_pages(id),
  key TEXT NOT NULL,
  type TEXT NOT NULL CHECK (type IN ('fact','rule','flow','api','requirement','faq',
    'term','troubleshooting','issue','decision','note')),
  status TEXT NOT NULL CHECK (status IN
    ('current','needs_review','conflicted','deprecated','rejected','deleted')),
  needs_review_reason TEXT,
  source_id TEXT NOT NULL REFERENCES documents(id),
  source_path TEXT NOT NULL,
  source_date TEXT NOT NULL DEFAULT 'unknown',
  content_hash TEXT NOT NULL,
  user_edited INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE acw_block_chunks (block_id TEXT NOT NULL REFERENCES acw_blocks(id),
  chunk_id TEXT NOT NULL REFERENCES acw_chunk_ledger(id), UNIQUE(block_id, chunk_id));
CREATE TABLE acw_key_aliases (page_id TEXT NOT NULL, key TEXT NOT NULL, alias_of TEXT NOT NULL,
  UNIQUE(page_id, key));

CREATE TABLE acw_runs (id TEXT PRIMARY KEY, started_at TEXT NOT NULL, finished_at TEXT,
  status TEXT NOT NULL CHECK (status IN ('running','completed','aborted')),
  stats_json TEXT NOT NULL DEFAULT '{}');

CREATE TABLE acw_review_rows (
  id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES acw_runs(id),
  page_id TEXT NOT NULL REFERENCES acw_pages(id),
  row_kind TEXT NOT NULL CHECK (row_kind IN ('conflict','needs_review','taxonomy_merge')),
  existing_block_id TEXT,
  candidate_json TEXT,
  conflict_type TEXT,
  recommendation TEXT NOT NULL,
  recommendation_basis TEXT,
  decision TEXT CHECK (decision IN ('accept_new','keep_existing','merge','mark_conflicted',
    'deprecate_existing','reject_new','delete_duplicate','needs_more_info')),
  notes TEXT, applied_at TEXT
);

CREATE TABLE acw_events (id TEXT PRIMARY KEY, ts TEXT NOT NULL, actor TEXT NOT NULL,
  kind TEXT NOT NULL, payload_json TEXT NOT NULL DEFAULT '{}');
