"""
Connection management and idempotent upserts. No business logic lives here.

Conflict behaviour differs per table, and the difference is the interesting part:

- `messages` uses DO NOTHING. First write wins, per ADR-0005 -- a Message copied
  into a resumed Session keeps the attribution it was first ingested with, so a
  re-run cannot silently reshuffle which Session owns which spend.
- `tool_calls` uses DO UPDATE with COALESCE. A tool call and its result are two
  separate transcript lines, possibly ingested in different runs, and they merge
  onto one row -- but a later NULL must never erase an earlier value.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Generator, Iterable, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from tracker.db.schema import CONNECTION_PRAGMAS, migrate


def connect(db_path: Path, *, cache_kib: int = 20_000, create: bool = True) -> sqlite3.Connection:
    """Open a connection with the standard PRAGMAs and the schema applied."""
    if create:
        db_path.parent.mkdir(parents=True, exist_ok=True)
    elif not db_path.exists():
        raise FileNotFoundError(
            f"no tracker database at {db_path} -- run `tracker db init` first"
        )

    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    for pragma in CONNECTION_PRAGMAS:
        conn.execute(pragma)
    # Negative cache_size means KiB rather than pages -- a fixed memory ceiling,
    # which is what matters on a 7.7 GB laptop with earlyoom (architecture.md §1).
    conn.execute(f"PRAGMA cache_size = -{int(cache_kib)}")
    migrate(conn)
    return conn


@contextmanager
def open_db(db_path: Path, *, cache_kib: int = 20_000, create: bool = True) -> Generator[sqlite3.Connection]:
    conn = connect(db_path, cache_kib=cache_kib, create=create)
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def transaction(conn: sqlite3.Connection) -> Generator[sqlite3.Connection]:
    """One explicit transaction. The collector wraps a whole source file in this,
    together with that file's watermark update, so a crash leaves a consistent
    database and a correct resume point rather than rows without a watermark."""
    conn.execute("BEGIN")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def _insert_sql(table: str, columns: Sequence[str], conflict: str) -> str:
    placeholders = ", ".join("?" for _ in columns)
    return (
        f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders}) {conflict}"
    )


def _executemany(
    conn: sqlite3.Connection,
    sql: str,
    columns: Sequence[str],
    rows: Iterable[dict[str, Any]],
) -> int:
    """Run a batch and return the number of rows that actually landed.

    `cursor.rowcount` after `executemany` counts only rows the statement changed,
    so a fully-deduplicated batch reports 0 -- which is exactly the signal the
    idempotency test asserts on a second run.
    """
    payload = [tuple(row.get(col) for col in columns) for row in rows]
    if not payload:
        return 0
    cur = conn.executemany(sql, payload)
    return max(cur.rowcount, 0)


MESSAGE_COLUMNS: tuple[str, ...] = (
    "dedup_key", "agent", "kind", "session_id", "agent_run_id", "subagent_type",
    "is_sidechain", "parent_session_id", "spawn_depth", "source_file",
    "source_entry_id", "provider_msg_id", "request_id", "parent_entry_id",
    "cwd", "project_slug", "git_branch", "agent_version", "ts", "ts_epoch_ms",
    "model", "response_model", "provider", "api", "stop_reason",
    "input_tokens", "output_tokens", "reasoning_tokens", "cache_read_tokens",
    "cache_write_5m_tokens", "cache_write_1h_tokens", "cache_write_tokens",
    "total_tokens", "context_tokens", "retry_count", "latency_ms",
    "cache_miss_reason", "reported_cost_usd", "derived_cost_usd", "cost_usd",
    "cost_source", "pricing_version", "ingested_at",
)

TOOL_CALL_COLUMNS: tuple[str, ...] = (
    "dedup_key", "agent", "session_id", "agent_run_id", "tool_use_id", "tool_name",
    "project_slug", "ts", "ts_epoch_ms", "result_ts_epoch_ms", "duration_ms",
    "is_error", "source_file", "ingested_at",
)

SUBAGENT_RUN_COLUMNS: tuple[str, ...] = (
    "dedup_key", "agent", "source", "parent_session_id", "agent_run_id",
    "agent_type", "tool_use_id", "spawn_depth", "task_sha256", "task_len",
    "started_at", "ended_at", "duration_ms", "status", "exit_code", "model",
    "message_count", "total_tokens", "cost_usd", "source_file", "ingested_at",
)


def upsert_messages(conn: sqlite3.Connection, rows: Iterable[dict[str, Any]]) -> int:
    """Insert Messages, ignoring any whose dedup_key is already stored.

    DO NOTHING rather than DO UPDATE is the ADR-0005 'first write wins' rule.
    """
    sql = _insert_sql("messages", MESSAGE_COLUMNS, "ON CONFLICT (dedup_key) DO NOTHING")
    return _executemany(conn, sql, MESSAGE_COLUMNS, rows)


def upsert_tool_calls(conn: sqlite3.Connection, rows: Iterable[dict[str, Any]]) -> int:
    """Insert or merge tool activity.

    COALESCE(excluded.x, tool_calls.x) keeps whichever side has a value: the call
    line supplies `tool_name` and `ts`, the result line supplies `is_error` and
    `result_ts_epoch_ms`, and neither may blank out the other's contribution.
    """
    conflict = """
        ON CONFLICT (dedup_key) DO UPDATE SET
            tool_name          = COALESCE(excluded.tool_name, tool_calls.tool_name),
            ts                 = COALESCE(tool_calls.ts, excluded.ts),
            ts_epoch_ms        = COALESCE(tool_calls.ts_epoch_ms, excluded.ts_epoch_ms),
            result_ts_epoch_ms = COALESCE(excluded.result_ts_epoch_ms,
                                          tool_calls.result_ts_epoch_ms),
            is_error           = COALESCE(excluded.is_error, tool_calls.is_error),
            agent_run_id       = COALESCE(excluded.agent_run_id, tool_calls.agent_run_id),
            duration_ms        = CASE
                WHEN COALESCE(excluded.result_ts_epoch_ms, tool_calls.result_ts_epoch_ms)
                     IS NOT NULL
                 AND COALESCE(tool_calls.ts_epoch_ms, excluded.ts_epoch_ms) IS NOT NULL
                THEN COALESCE(excluded.result_ts_epoch_ms, tool_calls.result_ts_epoch_ms)
                     - COALESCE(tool_calls.ts_epoch_ms, excluded.ts_epoch_ms)
                ELSE tool_calls.duration_ms
            END
    """
    sql = _insert_sql("tool_calls", TOOL_CALL_COLUMNS, conflict)
    return _executemany(conn, sql, TOOL_CALL_COLUMNS, rows)


def upsert_subagent_runs(conn: sqlite3.Connection, rows: Iterable[dict[str, Any]]) -> int:
    """Insert subagent runs, filling in fields that arrive from a second source.

    A Claude subagent has two sources -- the `.meta.json` sibling and the
    transcript itself -- so later non-NULL values are allowed to complete the row.
    """
    conflict = """
        ON CONFLICT (dedup_key) DO UPDATE SET
            agent_type    = COALESCE(excluded.agent_type, subagent_runs.agent_type),
            tool_use_id   = COALESCE(excluded.tool_use_id, subagent_runs.tool_use_id),
            spawn_depth   = COALESCE(excluded.spawn_depth, subagent_runs.spawn_depth),
            task_sha256   = COALESCE(excluded.task_sha256, subagent_runs.task_sha256),
            task_len      = COALESCE(excluded.task_len, subagent_runs.task_len),
            ended_at      = COALESCE(excluded.ended_at, subagent_runs.ended_at),
            duration_ms   = COALESCE(excluded.duration_ms, subagent_runs.duration_ms),
            status        = COALESCE(excluded.status, subagent_runs.status),
            exit_code     = COALESCE(excluded.exit_code, subagent_runs.exit_code),
            model         = COALESCE(excluded.model, subagent_runs.model),
            message_count = COALESCE(excluded.message_count, subagent_runs.message_count),
            total_tokens  = COALESCE(excluded.total_tokens, subagent_runs.total_tokens),
            cost_usd      = COALESCE(excluded.cost_usd, subagent_runs.cost_usd)
    """
    sql = _insert_sql("subagent_runs", SUBAGENT_RUN_COLUMNS, conflict)
    return _executemany(conn, sql, SUBAGENT_RUN_COLUMNS, rows)


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?) "
        "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def table_names(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    return {r["name"] for r in rows}
