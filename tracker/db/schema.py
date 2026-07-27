"""
The canonical SQLite schema (architecture.md §4.2).

Two properties are deliberate and load-bearing:

**Every table is STRICT.** SQLite's default type affinity would silently accept a
string where a token count belongs; the parsers handle two agents with divergent
field names, and a typo there should fail at insert rather than surface months
later as a cost that cannot be explained.

**Every shippable table carries `shipped_at`.** The Postgres shipper is deferred
(architecture.md §4.3), but adding the column later would mean a migration over a
table with the entire ingest history in it. The column costs nothing now and turns
the shipper into `SELECT ... WHERE shipped_at IS NULL` with no schema change.
"""

from __future__ import annotations

import sqlite3

SCHEMA_VERSION = 1

#: Applied to every connection. WAL lets a `tracker report` read while a collect
#: writes; NORMAL is the right durability trade for data that can be re-derived
#: from the transcripts if a crash loses the last few commits.
CONNECTION_PRAGMAS = (
    "PRAGMA journal_mode = WAL",
    "PRAGMA synchronous = NORMAL",
    "PRAGMA foreign_keys = ON",
)

DDL = """
CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
) STRICT;

-- One row per Message (CONTEXT.md). The grain is the provider's message id,
-- globally scoped -- see ADR-0005. `dedup_key` as PRIMARY KEY is what makes
-- re-ingest, rotation re-reads and future Postgres replays all no-ops.
CREATE TABLE IF NOT EXISTS messages (
  dedup_key             TEXT PRIMARY KEY,
  agent                 TEXT NOT NULL CHECK (agent IN ('claude-code','pi')),
  kind                  TEXT NOT NULL DEFAULT 'assistant'
                             CHECK (kind IN ('assistant','compaction')),

  session_id            TEXT NOT NULL,
  agent_run_id          TEXT,     -- Claude agentId / pi subagent dir. NULL = main thread
  subagent_type         TEXT,     -- Claude attributionAgent or .meta.json agentType
  is_sidechain          INTEGER NOT NULL DEFAULT 0 CHECK (is_sidechain IN (0,1)),
  parent_session_id     TEXT,
  spawn_depth           INTEGER,

  source_file           TEXT NOT NULL,
  source_entry_id       TEXT,     -- first-seen transcript line id (Claude uuid / pi entry id)
  provider_msg_id       TEXT,     -- Claude message.id -- the dedup grain (ADR-0005)
  request_id            TEXT,
  parent_entry_id       TEXT,

  cwd                   TEXT,
  project_slug          TEXT,
  git_branch            TEXT,
  agent_version         TEXT,

  ts                    TEXT NOT NULL,     -- ISO-8601 UTC
  ts_epoch_ms           INTEGER NOT NULL,

  model                 TEXT,
  response_model        TEXT,
  provider              TEXT,
  api                   TEXT,
  stop_reason           TEXT,

  input_tokens          INTEGER NOT NULL DEFAULT 0,
  output_tokens         INTEGER NOT NULL DEFAULT 0,
  -- pi only, and a SUBSET of output_tokens, never an addend in a cost formula.
  reasoning_tokens      INTEGER NOT NULL DEFAULT 0,
  cache_read_tokens     INTEGER NOT NULL DEFAULT 0,
  -- 5m and 1h are priced differently and must not be merged (architecture.md §3.2).
  cache_write_5m_tokens INTEGER NOT NULL DEFAULT 0,
  cache_write_1h_tokens INTEGER NOT NULL DEFAULT 0,
  cache_write_tokens    INTEGER NOT NULL DEFAULT 0,
  total_tokens          INTEGER NOT NULL DEFAULT 0,
  context_tokens        INTEGER,

  retry_count           INTEGER NOT NULL DEFAULT 0,   -- len(usage.iterations) - 1
  latency_ms            INTEGER,                      -- derive/latency.py post-pass
  cache_miss_reason     TEXT,

  reported_cost_usd     REAL,     -- pi writes this; Claude Code has no cost field
  derived_cost_usd      REAL,     -- tokens x pricing table; NULL when model unknown
  cost_usd              REAL,     -- COALESCE(reported, derived); NULL propagates
  cost_source           TEXT CHECK (cost_source IN
                             ('reported','derived','zero-rated','unknown')),
  pricing_version       TEXT,

  ingested_at           TEXT NOT NULL,
  shipped_at            TEXT
) STRICT;

CREATE INDEX IF NOT EXISTS idx_messages_ts        ON messages(ts_epoch_ms);
CREATE INDEX IF NOT EXISTS idx_messages_agent_ts  ON messages(agent, ts_epoch_ms);
CREATE INDEX IF NOT EXISTS idx_messages_model_ts  ON messages(model, ts_epoch_ms);
CREATE INDEX IF NOT EXISTS idx_messages_session   ON messages(agent, session_id, ts_epoch_ms);
CREATE INDEX IF NOT EXISTS idx_messages_msgid     ON messages(provider_msg_id);
CREATE INDEX IF NOT EXISTS idx_messages_unshipped ON messages(shipped_at)
       WHERE shipped_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_messages_unpriced  ON messages(model)
       WHERE cost_usd IS NULL;

-- Rollup, fully recomputed from `messages` for touched sessions each run. Never
-- incremented in place: a recompute is idempotent, a counter is not.
CREATE TABLE IF NOT EXISTS sessions (
  agent                  TEXT NOT NULL,
  session_id             TEXT NOT NULL,
  cwd                    TEXT,
  project_slug           TEXT,
  git_branch             TEXT,
  started_at             TEXT,
  ended_at               TEXT,
  duration_ms            INTEGER,
  message_count          INTEGER NOT NULL DEFAULT 0,
  compaction_count       INTEGER NOT NULL DEFAULT 0,
  subagent_message_count INTEGER NOT NULL DEFAULT 0,
  models_used            TEXT,      -- JSON array
  model_switch_count     INTEGER NOT NULL DEFAULT 0,
  input_tokens           INTEGER NOT NULL DEFAULT 0,
  output_tokens          INTEGER NOT NULL DEFAULT 0,
  cache_read_tokens      INTEGER NOT NULL DEFAULT 0,
  cache_write_tokens     INTEGER NOT NULL DEFAULT 0,
  total_tokens           INTEGER NOT NULL DEFAULT 0,
  cost_usd               REAL,
  -- SUM() skips NULLs, so a partly-unpriced session would otherwise look cheap
  -- rather than incomplete. Reports footnote on this flag.
  has_unknown_pricing    INTEGER NOT NULL DEFAULT 0 CHECK (has_unknown_pricing IN (0,1)),
  tool_call_count        INTEGER NOT NULL DEFAULT 0,
  tool_error_count       INTEGER NOT NULL DEFAULT 0,
  updated_at             TEXT NOT NULL,
  shipped_at             TEXT,
  PRIMARY KEY (agent, session_id)
) STRICT;

CREATE INDEX IF NOT EXISTS idx_sessions_started ON sessions(started_at);

-- The call line and its later result line merge onto one row, so `is_error` and
-- `duration_ms` stay NULL until the result is ingested (possibly a run later).
CREATE TABLE IF NOT EXISTS tool_calls (
  dedup_key          TEXT PRIMARY KEY,
  agent              TEXT NOT NULL CHECK (agent IN ('claude-code','pi')),
  session_id         TEXT NOT NULL,
  agent_run_id       TEXT,
  tool_use_id        TEXT NOT NULL,
  tool_name          TEXT,
  project_slug       TEXT,
  ts                 TEXT,
  ts_epoch_ms        INTEGER,
  result_ts_epoch_ms INTEGER,
  duration_ms        INTEGER,
  is_error           INTEGER CHECK (is_error IN (0,1)),
  source_file        TEXT NOT NULL,
  ingested_at        TEXT NOT NULL,
  shipped_at         TEXT
) STRICT;

CREATE INDEX IF NOT EXISTS idx_tool_calls_name ON tool_calls(tool_name, ts_epoch_ms);
CREATE INDEX IF NOT EXISTS idx_tool_calls_sess ON tool_calls(agent, session_id);

-- Task text is hashed, never stored: architecture.md §8 forbids persisting
-- conversation content, and a subagent task prompt is exactly that.
CREATE TABLE IF NOT EXISTS subagent_runs (
  dedup_key         TEXT PRIMARY KEY,
  agent             TEXT NOT NULL CHECK (agent IN ('claude-code','pi')),
  source            TEXT NOT NULL CHECK (source IN
                      ('pi-run-history','claude-subagent-transcript','pi-subagent-session')),
  parent_session_id TEXT,
  agent_run_id      TEXT,
  agent_type        TEXT,     -- 'Explore', 'test-designer' -- config-derived, safe
  tool_use_id       TEXT,
  spawn_depth       INTEGER,
  task_sha256       TEXT,     -- 16 hex of the task text. NEVER the text.
  task_len          INTEGER,
  started_at        TEXT,
  ended_at          TEXT,
  duration_ms       INTEGER,
  status            TEXT,
  exit_code         INTEGER,
  model             TEXT,
  message_count     INTEGER,
  total_tokens      INTEGER,
  cost_usd          REAL,
  source_file       TEXT NOT NULL,
  ingested_at       TEXT NOT NULL,
  shipped_at        TEXT
) STRICT;

CREATE INDEX IF NOT EXISTS idx_subagent_runs_parent ON subagent_runs(parent_session_id);

-- Watermarks (architecture.md §4.1). `byte_offset` always points just past a
-- complete newline, because the collector reads files the agents are appending to.
CREATE TABLE IF NOT EXISTS ingest_files (
  path              TEXT PRIMARY KEY,
  agent             TEXT NOT NULL,
  source_kind       TEXT NOT NULL,
  inode             INTEGER,
  size_bytes        INTEGER NOT NULL DEFAULT 0,
  mtime_ns          INTEGER,
  byte_offset       INTEGER NOT NULL DEFAULT 0,
  lines_ingested    INTEGER NOT NULL DEFAULT 0,
  lines_skipped     INTEGER NOT NULL DEFAULT 0,
  first_ingested_at TEXT,
  last_ingested_at  TEXT,
  last_error        TEXT
) STRICT;

-- Vendored rate table (ADR-0004). Rates are per single token, not per million.
CREATE TABLE IF NOT EXISTS pricing (
  pricing_version              TEXT NOT NULL,
  model                        TEXT NOT NULL,
  provider                     TEXT NOT NULL DEFAULT '',
  input_usd_per_token          REAL,
  output_usd_per_token         REAL,
  cache_read_usd_per_token     REAL,
  cache_write_5m_usd_per_token REAL,
  cache_write_1h_usd_per_token REAL,
  cache_write_1h_source        TEXT,
  upstream_key                 TEXT,
  PRIMARY KEY (pricing_version, model, provider)
) STRICT;

CREATE TABLE IF NOT EXISTS pricing_versions (
  pricing_version TEXT PRIMARY KEY,
  fetched_at      TEXT NOT NULL,
  source_url      TEXT,
  upstream_sha256 TEXT,
  model_count     INTEGER,
  alias_count     INTEGER
) STRICT;

-- User-declared windows (ADR-0003). Ships in increment 1 despite fingerprints
-- being deferred, because experiment intent is not recoverable after the fact.
CREATE TABLE IF NOT EXISTS experiments (
  name       TEXT PRIMARY KEY,
  started_at TEXT NOT NULL,
  ended_at   TEXT,               -- NULL = still open
  note       TEXT,
  created_at TEXT NOT NULL,
  shipped_at TEXT
) STRICT;

-- Per-run telemetry: what a Collector Run actually did, including the unknown
-- models it refused to guess a price for.
CREATE TABLE IF NOT EXISTS ingest_runs (
  run_id            TEXT PRIMARY KEY,
  started_at        TEXT NOT NULL,
  finished_at       TEXT,
  files_scanned     INTEGER NOT NULL DEFAULT 0,
  files_read        INTEGER NOT NULL DEFAULT 0,
  files_rotated     INTEGER NOT NULL DEFAULT 0,
  bytes_read        INTEGER NOT NULL DEFAULT 0,
  lines_read        INTEGER NOT NULL DEFAULT 0,
  lines_skipped     INTEGER NOT NULL DEFAULT 0,
  messages_upserted INTEGER NOT NULL DEFAULT 0,
  tool_calls_upserted INTEGER NOT NULL DEFAULT 0,
  subagent_runs_upserted INTEGER NOT NULL DEFAULT 0,
  unknown_models    TEXT,        -- JSON array
  partial           INTEGER NOT NULL DEFAULT 0 CHECK (partial IN (0,1)),
  duration_ms       INTEGER
) STRICT;
"""

#: Tables the deferred Postgres shipper will replicate (architecture.md §4.3).
SHIPPABLE_TABLES = (
    "messages",
    "sessions",
    "tool_calls",
    "subagent_runs",
    "experiments",
)


def migrate(conn: sqlite3.Connection) -> int:
    """Create the schema if absent and return the resulting schema version.

    Idempotent: every statement is `IF NOT EXISTS`, so this runs on every
    connection open rather than being gated behind a version check.
    """
    conn.executescript(DDL)
    conn.execute(
        "INSERT INTO meta (key, value) VALUES ('schema_version', ?) "
        "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
        (str(SCHEMA_VERSION),),
    )
    conn.commit()
    return SCHEMA_VERSION
