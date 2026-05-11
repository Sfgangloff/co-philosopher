-- cophilo schema, v1
-- Applied idempotently by db.models.init_db().

PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS schema_version (
  version INTEGER PRIMARY KEY,
  applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS documents (
  id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL CHECK(kind IN ('article','note')),
  title TEXT,
  source_path TEXT NOT NULL UNIQUE,
  normalized_path TEXT,
  language TEXT,
  status TEXT NOT NULL DEFAULT 'ingested'
    CHECK(status IN ('ingested','extracted','annotated','error')),
  ingested_at TEXT NOT NULL,
  metadata_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_documents_kind ON documents(kind);
CREATE INDEX IF NOT EXISTS idx_documents_status ON documents(status);

CREATE TABLE IF NOT EXISTS passages (
  id INTEGER PRIMARY KEY,
  document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  ord INTEGER NOT NULL,
  char_start INTEGER NOT NULL,
  char_end INTEGER NOT NULL,
  section_path TEXT,
  text TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_passages_document ON passages(document_id, ord);

CREATE TABLE IF NOT EXISTS concepts (
  id INTEGER PRIMARY KEY,
  slug TEXT NOT NULL UNIQUE,
  canonical_label_en TEXT,
  canonical_label_fr TEXT,
  description TEXT,
  kind TEXT NOT NULL DEFAULT 'mine'
    CHECK(kind IN ('mine','created','external')),
  status TEXT NOT NULL DEFAULT 'draft'
    CHECK(status IN ('draft','confirmed','merged')),
  merged_into INTEGER REFERENCES concepts(id),
  created_at TEXT NOT NULL,
  confirmed_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_concepts_status ON concepts(status);

CREATE TABLE IF NOT EXISTS concept_aliases (
  concept_id INTEGER NOT NULL REFERENCES concepts(id) ON DELETE CASCADE,
  alias TEXT NOT NULL,
  language TEXT NOT NULL,
  PRIMARY KEY (concept_id, alias, language)
);

CREATE TABLE IF NOT EXISTS concept_mentions (
  id INTEGER PRIMARY KEY,
  concept_id INTEGER NOT NULL REFERENCES concepts(id) ON DELETE CASCADE,
  passage_id INTEGER NOT NULL REFERENCES passages(id) ON DELETE CASCADE,
  span_start INTEGER,
  span_end INTEGER,
  role TEXT,
  confidence REAL
);

CREATE INDEX IF NOT EXISTS idx_mentions_concept ON concept_mentions(concept_id);
CREATE INDEX IF NOT EXISTS idx_mentions_passage ON concept_mentions(passage_id);

CREATE TABLE IF NOT EXISTS questions (
  id INTEGER PRIMARY KEY,
  label TEXT NOT NULL,
  description TEXT,
  status TEXT NOT NULL DEFAULT 'open'
    CHECK(status IN ('open','answered','dormant')),
  first_raised_passage_id INTEGER REFERENCES passages(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS question_mentions (
  question_id INTEGER NOT NULL REFERENCES questions(id) ON DELETE CASCADE,
  passage_id INTEGER NOT NULL REFERENCES passages(id) ON DELETE CASCADE,
  role TEXT,
  PRIMARY KEY (question_id, passage_id)
);

CREATE TABLE IF NOT EXISTS claims (
  id INTEGER PRIMARY KEY,
  passage_id INTEGER NOT NULL REFERENCES passages(id) ON DELETE CASCADE,
  statement TEXT NOT NULL,
  polarity TEXT,
  metadata_json TEXT
);

CREATE TABLE IF NOT EXISTS claim_concepts (
  claim_id INTEGER NOT NULL REFERENCES claims(id) ON DELETE CASCADE,
  concept_id INTEGER NOT NULL REFERENCES concepts(id) ON DELETE CASCADE,
  PRIMARY KEY (claim_id, concept_id)
);

CREATE TABLE IF NOT EXISTS relations (
  id INTEGER PRIMARY KEY,
  src_concept_id INTEGER NOT NULL REFERENCES concepts(id) ON DELETE CASCADE,
  dst_concept_id INTEGER NOT NULL REFERENCES concepts(id) ON DELETE CASCADE,
  kind TEXT NOT NULL,
  weight REAL,
  source_passage_id INTEGER REFERENCES passages(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_relations_src ON relations(src_concept_id);
CREATE INDEX IF NOT EXISTS idx_relations_dst ON relations(dst_concept_id);

CREATE TABLE IF NOT EXISTS external_authors (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  normalized_name TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS concept_external_authors (
  concept_id INTEGER NOT NULL REFERENCES concepts(id) ON DELETE CASCADE,
  author_id INTEGER NOT NULL REFERENCES external_authors(id) ON DELETE CASCADE,
  PRIMARY KEY (concept_id, author_id)
);

CREATE TABLE IF NOT EXISTS embeddings (
  ref_kind TEXT NOT NULL,
  ref_id INTEGER NOT NULL,
  vector BLOB NOT NULL,
  model TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY (ref_kind, ref_id, model)
);

CREATE TABLE IF NOT EXISTS review_queue (
  id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL
    CHECK(kind IN ('new_concept','merge','relabel','drift','unfit_passage','split')),
  payload_json TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending'
    CHECK(status IN ('pending','accepted','rejected','deferred')),
  created_at TEXT NOT NULL,
  decided_at TEXT,
  notes TEXT
);

CREATE INDEX IF NOT EXISTS idx_review_status ON review_queue(status, created_at);

CREATE TABLE IF NOT EXISTS bibliography (
  id INTEGER PRIMARY KEY,
  source TEXT NOT NULL,
  external_id TEXT,
  title TEXT NOT NULL,
  authors TEXT,
  journal TEXT,
  year INTEGER,
  abstract TEXT,
  doi TEXT,
  quality_score REAL,
  fetched_at TEXT NOT NULL,
  UNIQUE (source, external_id)
);

CREATE TABLE IF NOT EXISTS bibliography_links (
  id INTEGER PRIMARY KEY,
  bibliography_id INTEGER NOT NULL REFERENCES bibliography(id) ON DELETE CASCADE,
  target_kind TEXT NOT NULL CHECK(target_kind IN ('concept','article_draft','note_cluster')),
  target_id INTEGER NOT NULL,
  rationale TEXT
);
