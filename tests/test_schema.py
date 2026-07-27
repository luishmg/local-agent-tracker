"""Schema shape, idempotency of `migrate()`, and the upsert conflict rules."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from tracker.db.schema import SCHEMA_VERSION, SHIPPABLE_TABLES, migrate
from tracker.db.store import (
    connect,
    get_meta,
    open_db,
    set_meta,
    table_names,
    transaction,
    upsert_messages,
    upsert_subagent_runs,
    upsert_tool_calls,
)

EXPECTED_TABLES = {
    "meta",
    "messages",
    "sessions",
    "tool_calls",
    "subagent_runs",
    "ingest_files",
    "pricing",
    "pricing_versions",
    "experiments",
    "ingest_runs",
}


@pytest.fixture
def conn(tmp_path: Path):
    with open_db(tmp_path / "t.db") as c:
        yield c


def _message_row(**overrides):
    row = {
        "dedup_key": "cc:msg_1|req_1",
        "agent": "claude-code",
        "kind": "assistant",
        "session_id": "sess-a",
        "source_file": "/x.jsonl",
        "ts": "2026-07-01T10:00:00Z",
        "ts_epoch_ms": 1_782_000_000_000,
        "is_sidechain": 0,
        "input_tokens": 100,
        "output_tokens": 50,
        "reasoning_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_5m_tokens": 0,
        "cache_write_1h_tokens": 0,
        "cache_write_tokens": 0,
        "total_tokens": 150,
        "retry_count": 0,
        "ingested_at": "2026-07-26T00:00:00Z",
    }
    row.update(overrides)
    return row


def test_all_tables_created(conn: sqlite3.Connection) -> None:
    assert EXPECTED_TABLES <= table_names(conn)


def test_schema_version_recorded(conn: sqlite3.Connection) -> None:
    assert get_meta(conn, "schema_version") == str(SCHEMA_VERSION)


def test_migrate_is_idempotent(conn: sqlite3.Connection) -> None:
    before = table_names(conn)
    for _ in range(3):
        assert migrate(conn) == SCHEMA_VERSION
    assert table_names(conn) == before


def test_reopening_preserves_data(tmp_path: Path) -> None:
    """migrate() runs on every connect, so it must never clobber existing rows."""
    db = tmp_path / "t.db"
    with open_db(db) as c:
        set_meta(c, "canary", "kept")
        c.commit()
    with open_db(db) as c:
        assert get_meta(c, "canary") == "kept"


def test_connect_without_create_requires_existing_db(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="tracker db init"):
        connect(tmp_path / "absent.db", create=False)


def test_tables_are_strict(conn: sqlite3.Connection) -> None:
    """STRICT is what turns a parser typo into an insert-time error instead of a
    cost that cannot be explained months later."""
    with pytest.raises(sqlite3.IntegrityError):
        upsert_messages(conn, [_message_row(input_tokens="not-a-number")])


def test_agent_check_constraint_rejects_unknown_agents(conn: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        upsert_messages(conn, [_message_row(agent="cursor")])


def test_cost_source_check_constraint(conn: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        upsert_messages(conn, [_message_row(cost_source="guessed")])


def test_every_shippable_table_has_shipped_at(conn: sqlite3.Connection) -> None:
    """The Postgres shipper is deferred, but adding this column later would mean
    migrating a table holding the whole ingest history (architecture.md §4.3)."""
    for table in SHIPPABLE_TABLES:
        cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        assert "shipped_at" in cols, f"{table} is missing the shipper seam"


def test_message_upsert_is_first_write_wins(conn: sqlite3.Connection) -> None:
    """ADR-0005: a Message copied into a resumed Session keeps its original
    attribution, so re-running the collector cannot reshuffle Session ownership."""
    assert upsert_messages(conn, [_message_row(session_id="first")]) == 1
    assert upsert_messages(conn, [_message_row(session_id="second", input_tokens=999)]) == 0

    rows = conn.execute("SELECT session_id, input_tokens FROM messages").fetchall()
    assert len(rows) == 1
    assert rows[0]["session_id"] == "first"
    assert rows[0]["input_tokens"] == 100


def test_tool_call_merges_result_line_onto_call_line(conn: sqlite3.Connection) -> None:
    """The call and its result are separate transcript lines, possibly ingested in
    different runs; each supplies its own half of the row."""
    base = {
        "dedup_key": "tc:claude-code|sess-a|toolu_1",
        "agent": "claude-code",
        "session_id": "sess-a",
        "tool_use_id": "toolu_1",
        "source_file": "/x.jsonl",
        "ingested_at": "2026-07-26T00:00:00Z",
    }
    upsert_tool_calls(conn, [{**base, "tool_name": "Read", "ts_epoch_ms": 1000, "ts": "t"}])
    upsert_tool_calls(conn, [{**base, "result_ts_epoch_ms": 1250, "is_error": 1}])

    row = conn.execute("SELECT * FROM tool_calls").fetchone()
    assert row["tool_name"] == "Read"
    assert row["is_error"] == 1
    assert row["duration_ms"] == 250


def test_tool_call_merge_never_erases_with_null(conn: sqlite3.Connection) -> None:
    base = {
        "dedup_key": "tc:pi|sess-b|call_1",
        "agent": "pi",
        "session_id": "sess-b",
        "tool_use_id": "call_1",
        "source_file": "/y.jsonl",
        "ingested_at": "2026-07-26T00:00:00Z",
    }
    upsert_tool_calls(conn, [{**base, "tool_name": "Bash", "is_error": 0}])
    upsert_tool_calls(conn, [base])  # a re-parse carrying no new information

    row = conn.execute("SELECT * FROM tool_calls").fetchone()
    assert row["tool_name"] == "Bash"
    assert row["is_error"] == 0


def test_subagent_run_completes_from_second_source(conn: sqlite3.Connection) -> None:
    """A Claude subagent is described by both its .meta.json and its transcript."""
    base = {
        "dedup_key": "sa:sess-a|agent-1",
        "agent": "claude-code",
        "source": "claude-subagent-transcript",
        "source_file": "/a.jsonl",
        "ingested_at": "2026-07-26T00:00:00Z",
    }
    upsert_subagent_runs(conn, [{**base, "agent_type": "Explore", "spawn_depth": 1}])
    upsert_subagent_runs(conn, [{**base, "total_tokens": 4242, "status": "ok"}])

    row = conn.execute("SELECT * FROM subagent_runs").fetchone()
    assert row["agent_type"] == "Explore"
    assert row["spawn_depth"] == 1
    assert row["total_tokens"] == 4242
    assert row["status"] == "ok"


def test_transaction_rolls_back_on_error(conn: sqlite3.Connection) -> None:
    """The collector ties a file's rows and its watermark to one transaction, so a
    crash must leave neither rather than rows without a resume point."""
    with pytest.raises(RuntimeError):
        with transaction(conn):
            upsert_messages(conn, [_message_row()])
            raise RuntimeError("crash mid-file")

    assert conn.execute("SELECT COUNT(*) AS n FROM messages").fetchone()["n"] == 0


def test_empty_batch_is_a_no_op(conn: sqlite3.Connection) -> None:
    assert upsert_messages(conn, []) == 0
    assert upsert_tool_calls(conn, []) == 0
    assert upsert_subagent_runs(conn, []) == 0
