PRAGMA foreign_keys=OFF;

CREATE TABLE acw_chunk_ledger_new (
  id TEXT PRIMARY KEY,
  source_id TEXT NOT NULL REFERENCES documents(id),
  source_version_id TEXT NOT NULL REFERENCES acw_source_versions(id),
  content_hash TEXT NOT NULL,
  document_chunk_id TEXT,
  ordinal INTEGER NOT NULL,
  disposition TEXT NOT NULL CHECK (disposition IN
    ('pending','placed','duplicate','irrelevant','exclusion_pending','conflicted_pending',
     'failed','failed_final','superseded')),
  disposition_reason TEXT,
  duplicate_of_block_id TEXT,
  attempts INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT NOT NULL,
  UNIQUE(source_id, source_version_id, content_hash, ordinal)
);

INSERT INTO acw_chunk_ledger_new
SELECT id, source_id, source_version_id, content_hash, document_chunk_id, ordinal,
       disposition, disposition_reason, duplicate_of_block_id, attempts, updated_at
FROM acw_chunk_ledger;

DROP TABLE acw_chunk_ledger;
ALTER TABLE acw_chunk_ledger_new RENAME TO acw_chunk_ledger;

CREATE TABLE acw_review_rows_new (
  id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES acw_runs(id),
  page_id TEXT REFERENCES acw_pages(id),
  row_kind TEXT NOT NULL CHECK (row_kind IN ('conflict','needs_review','taxonomy_merge','exclusion')),
  existing_block_id TEXT,
  candidate_json TEXT,
  conflict_type TEXT,
  recommendation TEXT NOT NULL,
  recommendation_basis TEXT,
  decision TEXT CHECK (decision IN ('accept_new','keep_existing','merge','mark_conflicted',
    'deprecate_existing','reject_new','delete_duplicate','needs_more_info',
    'approve_exclusion','reinstate')),
  notes TEXT, applied_at TEXT
);

INSERT INTO acw_review_rows_new
SELECT id, run_id, page_id, row_kind, existing_block_id, candidate_json, conflict_type,
       recommendation, recommendation_basis, decision, notes, applied_at
FROM acw_review_rows;

DROP TABLE acw_review_rows;
ALTER TABLE acw_review_rows_new RENAME TO acw_review_rows;

PRAGMA foreign_keys=ON;
