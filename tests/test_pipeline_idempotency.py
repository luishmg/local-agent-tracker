"""
End-to-end pipeline behaviour.

Three tests here are load-bearing beyond their own scope:

- `test_reingest_is_a_no_op` is architecture.md §8's central invariant made
  executable. Everything else — crash recovery, watermark resume, the future
  Postgres replay — rests on re-running the collector changing nothing.
- `test_no_message_content_reaches_the_database` is the §8 security invariant.
  It sweeps every TEXT column of every table for a canary, so it catches leaks
  through paths nobody thought to test individually.
- `test_content_block_fanout_is_billed_once_end_to_end` re-proves the ADR-0005
  collapse through the whole stack, not just the parser.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from tests.factories import (
    CANARY,
    claude_assistant_lines,
    claude_session_file,
    claude_subagent_file,
    claude_usage,
    claude_user_line,
    pi_assistant_entry,
    pi_claude_artifacts_file,
    pi_compaction_entry,
    pi_run_history_line,
    pi_session_file,
    pi_session_header,
    write_jsonl,
)
from tracker.config import Settings, get_settings
from tracker.db.store import open_db
from tracker.derive.latency import backfill_latency
from tracker.derive.rollups import rebuild_sessions
from tracker.ingest.pipeline import RunStats, collect
from tracker.pricing import get_pricing_table

NOW = "2026-07-26T00:00:00Z"


@pytest.fixture
def settings() -> Settings:
    return get_settings()


@pytest.fixture
def conn(settings: Settings):
    with open_db(settings.database_path) as c:
        yield c


def run(conn, settings: Settings, **kwargs) -> RunStats:
    return collect(conn, settings=settings, pricing=get_pricing_table(), **kwargs)


def scalar(conn: sqlite3.Connection, sql: str):
    row = conn.execute(sql).fetchone()
    return row[0] if row else None


@pytest.fixture
def populated(settings: Settings) -> Settings:
    """A source tree containing every shape the collector must handle."""
    claude_session_file(
        settings.claude_projects_dir, session_id="sess-1",
        entries=[
            *claude_assistant_lines(
                msg_id="msg_a", session_id="sess-1",
                blocks=("thinking", "text", "tool_use"), tool_names=("Read",),
                usage=claude_usage(input_tokens=1000, output_tokens=500),
            ),
            claude_user_line(session_id="sess-1", tool_use_id="toolu_msg_a_2", is_error=False),
            *claude_assistant_lines(
                msg_id="msg_b", session_id="sess-1", blocks=("text",),
                ts="2026-07-01T10:00:30.000Z",
                usage=claude_usage(input_tokens=2000, output_tokens=100),
            ),
        ],
    )
    claude_subagent_file(
        settings.claude_projects_dir, session_id="sess-1", agent_id="a4984de84563b051b"
    )
    pi_session_file(
        settings.pi_sessions_dir,
        entries=[
            pi_session_header(),
            pi_assistant_entry(entry_id="p1", cost_total=0.002),
            pi_compaction_entry(entry_id="c1", cost_total=0.04),
        ],
    )
    # Must be ignored: Claude-shaped SDK output living under pi's sessions tree.
    pi_claude_artifacts_file(settings.pi_sessions_dir)
    write_jsonl(settings.pi_run_history_path, [pi_run_history_line()])
    return settings


class TestIdempotency:
    def test_reingest_is_a_no_op(self, conn, populated: Settings) -> None:
        """architecture.md §8's invariant: re-running over unchanged data must
        change nothing. Everything downstream depends on it."""
        first = run(conn, populated)
        assert first.messages_upserted > 0

        counts_before = {
            t: scalar(conn, f"SELECT COUNT(*) FROM {t}")
            for t in ("messages", "tool_calls", "subagent_runs")
        }
        cost_before = scalar(conn, "SELECT COALESCE(SUM(cost_usd), 0) FROM messages")

        second = run(conn, populated)

        assert second.messages_upserted == 0
        assert second.tool_calls_upserted == 0
        assert second.subagent_runs_upserted == 0
        for table, expected in counts_before.items():
            assert scalar(conn, f"SELECT COUNT(*) FROM {table}") == expected
        assert scalar(conn, "SELECT COALESCE(SUM(cost_usd), 0) FROM messages") == cost_before

    def test_unchanged_files_are_never_reopened(self, conn, populated: Settings) -> None:
        """The fast path that makes a 5-minute timer viable."""
        run(conn, populated)
        second = run(conn, populated)

        assert second.files_read == 0
        assert second.files_skipped == second.files_scanned
        assert second.bytes_read == 0

    def test_appending_ingests_only_the_new_lines(self, conn, populated: Settings) -> None:
        run(conn, populated)
        before = scalar(conn, "SELECT COUNT(*) FROM messages")

        path = populated.claude_projects_dir / "-home-user-Projects-demo" / "sess-1.jsonl"
        with path.open("a", encoding="utf-8") as fh:
            for line in claude_assistant_lines(
                msg_id="msg_c", session_id="sess-1", blocks=("text",),
                ts="2026-07-01T10:01:00.000Z",
            ):
                fh.write(json.dumps(line) + "\n")

        third = run(conn, populated)
        assert third.messages_upserted == 1
        assert scalar(conn, "SELECT COUNT(*) FROM messages") == before + 1
        assert third.files_read == 1, "only the appended file should be reopened"

    def test_rotated_file_is_reread_without_duplicating(self, conn, populated: Settings) -> None:
        """A genuinely new inode (atomic rename) must force a full re-read.

        Safe only because writes are keyed on dedup_key (ADR-0005).
        """
        run(conn, populated)
        before = scalar(conn, "SELECT COUNT(*) FROM messages")

        path = populated.claude_projects_dir / "-home-user-Projects-demo" / "sess-1.jsonl"
        old_inode = path.stat().st_ino
        tmp = path.with_suffix(".jsonl.tmp")
        tmp.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
        tmp.replace(path)
        assert path.stat().st_ino != old_inode, "fixture must actually rotate the inode"

        stats = run(conn, populated)
        assert stats.files_rotated >= 1
        assert stats.messages_upserted == 0
        assert scalar(conn, "SELECT COUNT(*) FROM messages") == before

    def test_in_place_rewrite_is_detected_even_when_the_inode_is_reused(
        self, conn, populated: Settings
    ) -> None:
        """Delete-and-recreate frequently hands back the *same* inode, so an inode
        check alone misses it. Without the REWRITTEN case the collector would read
        from the old offset, find nothing past it, and silently lose the rewrite.
        """
        run(conn, populated)
        before = scalar(conn, "SELECT COUNT(*) FROM messages")

        path = populated.claude_projects_dir / "-home-user-Projects-demo" / "sess-1.jsonl"
        content = path.read_text(encoding="utf-8")
        path.unlink()
        path.write_text(content, encoding="utf-8")

        stats = run(conn, populated)
        assert stats.files_read >= 1, "a modified file must be reopened"
        assert stats.messages_upserted == 0, "identical content dedups to nothing"
        assert scalar(conn, "SELECT COUNT(*) FROM messages") == before

    def test_rewritten_file_with_new_content_is_not_lost(
        self, conn, settings: Settings
    ) -> None:
        """The failure the REWRITTEN case exists to prevent: same byte length, new
        content, offset already at EOF."""
        path = settings.claude_projects_dir / "-home-user-Projects-demo" / "sess-rw.jsonl"
        first = claude_assistant_lines(msg_id="msg_aaaa", session_id="sess-rw", blocks=("text",))
        write_jsonl(path, first)
        run(conn, settings)
        assert scalar(conn, "SELECT COUNT(*) FROM messages") == 1

        # Same length (ids are the same width), entirely different message.
        second = claude_assistant_lines(msg_id="msg_bbbb", session_id="sess-rw", blocks=("text",))
        path.unlink()
        write_jsonl(path, second)

        run(conn, settings)
        ids = {r[0] for r in conn.execute("SELECT provider_msg_id FROM messages")}
        assert ids == {"msg_aaaa", "msg_bbbb"}, "the rewritten content must be ingested"


class TestDedupThroughTheStack:
    def test_content_block_fanout_is_billed_once_end_to_end(
        self, conn, settings: Settings
    ) -> None:
        """Seven lines, one identical usage each -> one row (ADR-0005 §0.1)."""
        claude_session_file(
            settings.claude_projects_dir, session_id="sess-fan",
            entries=claude_assistant_lines(
                msg_id="msg_fan", session_id="sess-fan",
                blocks=("thinking", "text") + ("tool_use",) * 5,
                tool_names=("Read", "Bash", "Grep", "Edit", "Write"),
                usage=claude_usage(input_tokens=4054, output_tokens=3452),
            ),
        )
        run(conn, settings)

        row = conn.execute("SELECT * FROM messages WHERE provider_msg_id = 'msg_fan'").fetchall()
        assert len(row) == 1
        assert row[0]["input_tokens"] == 4054
        assert row[0]["output_tokens"] == 3452
        assert scalar(conn, "SELECT COUNT(*) FROM tool_calls") == 5

    def test_same_message_in_two_sessions_is_billed_once(
        self, conn, settings: Settings
    ) -> None:
        """Resuming a session copies its history — 148 of 9,186 ids in the real
        sample. A session-scoped key would double-bill every one."""
        shared = claude_assistant_lines(msg_id="msg_shared", blocks=("text",))
        claude_session_file(
            settings.claude_projects_dir, session_id="sess-orig",
            entries=[dict(ln, sessionId="sess-orig") for ln in shared],
        )
        claude_session_file(
            settings.claude_projects_dir, session_id="sess-resumed",
            entries=[dict(ln, sessionId="sess-resumed") for ln in shared],
        )
        run(conn, settings)

        rows = conn.execute(
            "SELECT session_id FROM messages WHERE provider_msg_id = 'msg_shared'"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["session_id"] == "sess-orig", "first write wins (ADR-0005)"

    def test_pi_fork_duplicate_is_billed_once(self, conn, settings: Settings) -> None:
        entry = pi_assistant_entry(entry_id="75fc5036", cost_total=0.01)
        for i, ts_prefix in enumerate(("2026-07-01T10-00-00-000Z", "2026-07-01T10-05-00-000Z")):
            pi_session_file(
                settings.pi_sessions_dir,
                session_id=f"019f827{i}-0000-7000-8000-00000000000{i}",
                ts_prefix=ts_prefix,
                entries=[
                    pi_session_header(session_id=f"019f827{i}-0000-7000-8000-00000000000{i}"),
                    entry,
                ],
            )
        run(conn, settings)

        assert scalar(conn, "SELECT COUNT(*) FROM messages WHERE agent = 'pi'") == 1
        assert scalar(conn, "SELECT SUM(cost_usd) FROM messages WHERE agent = 'pi'") == 0.01


class TestSecurityInvariant:
    def test_no_message_content_reaches_the_database(self, conn, populated: Settings) -> None:
        """architecture.md §8: content is never stored.

        Sweeps every TEXT column of every table rather than checking known
        offenders, so it catches leaks through paths nobody thought to test —
        including pi's `compaction.summary` and run-history `task`.
        """
        run(conn, populated)

        tables = [
            r["name"] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        ]
        leaks: list[str] = []
        for table in tables:
            cols = [r["name"] for r in conn.execute(f"PRAGMA table_info({table})")
                    if r["type"] == "TEXT"]
            for col in cols:
                hits = conn.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE {col} LIKE ?", (f"%{CANARY}%",)
                ).fetchone()[0]
                if hits:
                    leaks.append(f"{table}.{col} ({hits} rows)")

        assert not leaks, f"conversation content leaked into: {leaks}"

    def test_subagent_task_is_hashed_not_stored(self, conn, populated: Settings) -> None:
        run(conn, populated)
        rows = conn.execute(
            "SELECT task_sha256, task_len FROM subagent_runs WHERE task_sha256 IS NOT NULL"
        ).fetchall()
        assert rows, "the fixture includes a run-history line with a task"
        for r in rows:
            assert len(r["task_sha256"]) == 16
            assert r["task_len"] > 0


class TestDiscoveryIntegration:
    def test_claude_artifacts_under_pi_are_not_ingested(
        self, conn, populated: Settings
    ) -> None:
        """120 such files exist here, 82 of whose sessions are already counted
        under ~/.claude/projects — ingesting them double-counts."""
        run(conn, populated)
        assert scalar(
            conn, "SELECT COUNT(*) FROM messages WHERE source_file LIKE '%claude-code-artifacts%'"
        ) == 0

    def test_subagent_spend_rolls_up_into_the_parent_session(
        self, conn, populated: Settings
    ) -> None:
        """Subagent transcripts carry the PARENT sessionId (§1.5)."""
        run(conn, populated)
        sidechain = conn.execute(
            "SELECT session_id, agent_run_id FROM messages WHERE is_sidechain = 1"
        ).fetchall()
        assert sidechain
        assert all(r["session_id"] == "sess-1" for r in sidechain)
        assert all(r["agent_run_id"] == "a4984de84563b051b" for r in sidechain)

    def test_pi_compaction_spend_is_counted(self, conn, populated: Settings) -> None:
        run(conn, populated)
        row = conn.execute(
            "SELECT cost_usd, cost_source FROM messages WHERE kind = 'compaction'"
        ).fetchone()
        assert row is not None
        assert row["cost_usd"] == 0.04
        assert row["cost_source"] == "reported"


class TestPricingIntegration:
    def test_claude_cost_is_derived_and_version_stamped(self, conn, settings: Settings) -> None:
        claude_session_file(
            settings.claude_projects_dir, session_id="sess-priced",
            entries=claude_assistant_lines(
                msg_id="msg_priced", session_id="sess-priced", blocks=("text",),
                model="claude-opus-4-8",
                usage=claude_usage(input_tokens=1_000_000, output_tokens=0),
            ),
        )
        run(conn, settings)

        row = conn.execute(
            "SELECT * FROM messages WHERE provider_msg_id = 'msg_priced'"
        ).fetchone()
        assert row["cost_source"] == "derived"
        assert row["cost_usd"] == pytest.approx(5.0)
        assert row["pricing_version"] == "2026-07-26"
        assert row["reported_cost_usd"] is None

    def test_unknown_model_is_null_and_reported_once(self, conn, settings: Settings) -> None:
        claude_session_file(
            settings.claude_projects_dir, session_id="sess-unknown",
            entries=claude_assistant_lines(
                msg_id="msg_unknown", session_id="sess-unknown", blocks=("text",),
                model="claude-from-the-future-9",
            ),
        )
        stats = run(conn, settings)

        row = conn.execute(
            "SELECT * FROM messages WHERE provider_msg_id = 'msg_unknown'"
        ).fetchone()
        assert row["cost_usd"] is None, "must be NULL, never a silent 0"
        assert row["cost_source"] == "unknown"
        assert stats.unknown_models == {"claude-from-the-future-9"}

    def test_synthetic_is_zero_rated(self, conn, settings: Settings) -> None:
        claude_session_file(
            settings.claude_projects_dir, session_id="sess-syn",
            entries=claude_assistant_lines(
                msg_id="msg_syn", session_id="sess-syn", blocks=("text",), model="<synthetic>",
            ),
        )
        stats = run(conn, settings)

        row = conn.execute("SELECT * FROM messages WHERE provider_msg_id = 'msg_syn'").fetchone()
        assert row["cost_usd"] == 0.0
        assert row["cost_source"] == "zero-rated"
        assert not stats.unknown_models, "zero-rated must not trip the warning"


class TestBudgets:
    def test_max_files_stops_early_and_marks_partial(self, conn, settings: Settings) -> None:
        for i in range(5):
            claude_session_file(settings.claude_projects_dir, session_id=f"sess-{i}")

        stats = run(conn, settings, max_files=2)
        assert stats.files_read == 2
        assert stats.partial is True

    def test_the_next_run_picks_up_what_was_skipped(self, conn, settings: Settings) -> None:
        for i in range(5):
            claude_session_file(
                settings.claude_projects_dir, session_id=f"sess-{i}",
                entries=claude_assistant_lines(msg_id=f"msg_{i}", session_id=f"sess-{i}",
                                               blocks=("text",)),
            )
        run(conn, settings, max_files=2)
        partial_count = scalar(conn, "SELECT COUNT(*) FROM messages")

        run(conn, settings, max_files=0)
        assert scalar(conn, "SELECT COUNT(*) FROM messages") == 5
        assert partial_count == 2

    def test_run_telemetry_is_recorded(self, conn, populated: Settings) -> None:
        stats = run(conn, populated)
        row = conn.execute(
            "SELECT * FROM ingest_runs WHERE run_id = ?", (stats.run_id,)
        ).fetchone()
        assert row is not None
        assert row["files_scanned"] == stats.files_scanned
        assert row["duration_ms"] is not None


class TestTolerance:
    def test_a_malformed_line_does_not_abort_the_file(self, conn, settings: Settings) -> None:
        path = settings.claude_projects_dir / "-home-user-Projects-demo" / "sess-bad.jsonl"
        good = claude_assistant_lines(msg_id="msg_good", session_id="sess-bad", blocks=("text",))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "{not json\n" + "\n".join(json.dumps(e) for e in good) + "\n", encoding="utf-8"
        )

        stats = run(conn, settings)
        assert stats.messages_upserted == 1
        assert stats.lines_skipped >= 1

    def test_partial_trailing_line_is_left_for_the_next_run(
        self, conn, settings: Settings
    ) -> None:
        """The collector reads files the agents are still appending to."""
        path = settings.claude_projects_dir / "-home-user-Projects-demo" / "sess-live.jsonl"
        complete = claude_assistant_lines(
            msg_id="msg_done", session_id="sess-live", blocks=("text",)
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        body = "\n".join(json.dumps(e) for e in complete) + "\n"
        path.write_text(body + '{"type":"assistant","message":{"id":"msg_half"', encoding="utf-8")

        run(conn, settings)
        assert scalar(conn, "SELECT COUNT(*) FROM messages WHERE session_id='sess-live'") == 1

        watermark = conn.execute(
            "SELECT byte_offset FROM ingest_files WHERE path = ?", (str(path),)
        ).fetchone()
        assert watermark["byte_offset"] == len(body.encode()), "must stop before the partial line"


class TestDerivations:
    def test_rollups_match_the_message_grain(self, conn, populated: Settings) -> None:
        run(conn, populated)
        rebuild_sessions(conn, now=NOW)

        mismatches = conn.execute(
            """
            SELECT s.agent, s.session_id
              FROM sessions s JOIN messages m
                ON m.agent = s.agent AND m.session_id = s.session_id
             GROUP BY s.agent, s.session_id
            HAVING ABS(COALESCE(s.cost_usd, 0) - COALESCE(SUM(m.cost_usd), 0)) > 1e-9
                OR s.message_count != COUNT(*)
            """
        ).fetchall()
        assert not mismatches

    def test_rollups_are_stable_across_a_second_recompute(
        self, conn, populated: Settings
    ) -> None:
        """Recompute, not increment — a counter would drift here."""
        run(conn, populated)
        rebuild_sessions(conn, now=NOW)
        first = conn.execute(
            "SELECT agent, session_id, message_count, total_tokens, cost_usd "
            "FROM sessions ORDER BY agent, session_id"
        ).fetchall()

        rebuild_sessions(conn, now="2026-07-27T00:00:00Z")
        second = conn.execute(
            "SELECT agent, session_id, message_count, total_tokens, cost_usd "
            "FROM sessions ORDER BY agent, session_id"
        ).fetchall()

        assert [tuple(r) for r in first] == [tuple(r) for r in second]

    def test_has_unknown_pricing_flags_incomplete_sessions(
        self, conn, settings: Settings
    ) -> None:
        """SUM() skips NULLs, so without the flag this session looks cheap."""
        claude_session_file(
            settings.claude_projects_dir, session_id="sess-mixed",
            entries=[
                *claude_assistant_lines(msg_id="m1", session_id="sess-mixed",
                                        blocks=("text",), model="claude-opus-4-8"),
                *claude_assistant_lines(msg_id="m2", session_id="sess-mixed",
                                        blocks=("text",), model="unknown-model-x",
                                        ts="2026-07-01T10:00:10.000Z"),
            ],
        )
        run(conn, settings)
        rebuild_sessions(conn, now=NOW)

        row = conn.execute(
            "SELECT * FROM sessions WHERE session_id = 'sess-mixed'"
        ).fetchone()
        assert row["has_unknown_pricing"] == 1
        assert row["cost_usd"] is not None, "the priced half still sums"

    def test_latency_is_derived_within_a_session(self, conn, settings: Settings) -> None:
        claude_session_file(
            settings.claude_projects_dir, session_id="sess-lat",
            entries=[
                *claude_assistant_lines(msg_id="m1", session_id="sess-lat", blocks=("text",),
                                        ts="2026-07-01T10:00:00.000Z"),
                *claude_assistant_lines(msg_id="m2", session_id="sess-lat", blocks=("text",),
                                        ts="2026-07-01T10:00:12.500Z"),
            ],
        )
        run(conn, settings)
        backfill_latency(conn)

        rows = conn.execute(
            "SELECT provider_msg_id, latency_ms FROM messages "
            "WHERE session_id = 'sess-lat' ORDER BY ts_epoch_ms"
        ).fetchall()
        assert rows[0]["latency_ms"] is None, "the first message has no predecessor"
        assert rows[1]["latency_ms"] == 12_500

    def test_implausible_gaps_are_not_recorded_as_latency(
        self, conn, settings: Settings
    ) -> None:
        """A user walking away is not the model thinking; storing it would wreck
        every average."""
        claude_session_file(
            settings.claude_projects_dir, session_id="sess-gap",
            entries=[
                *claude_assistant_lines(msg_id="g1", session_id="sess-gap", blocks=("text",),
                                        ts="2026-07-01T10:00:00.000Z"),
                *claude_assistant_lines(msg_id="g2", session_id="sess-gap", blocks=("text",),
                                        ts="2026-07-01T18:00:00.000Z"),
            ],
        )
        run(conn, settings)
        backfill_latency(conn)

        assert scalar(
            conn, "SELECT latency_ms FROM messages WHERE provider_msg_id = 'g2'"
        ) is None

    def test_latency_is_idempotent(self, conn, populated: Settings) -> None:
        run(conn, populated)
        backfill_latency(conn, only_null=False)
        first = conn.execute(
            "SELECT dedup_key, latency_ms FROM messages ORDER BY dedup_key"
        ).fetchall()
        backfill_latency(conn, only_null=False)
        second = conn.execute(
            "SELECT dedup_key, latency_ms FROM messages ORDER BY dedup_key"
        ).fetchall()
        assert [tuple(r) for r in first] == [tuple(r) for r in second]


class TestDoctor:
    def test_all_invariants_hold_on_a_clean_ingest(self, conn, populated: Settings) -> None:
        from tracker.report import run_doctor

        run(conn, populated)
        rebuild_sessions(conn, now=NOW)

        failures = [r for r in run_doctor(conn) if not r["passed"]]
        assert not failures, f"invariants violated: {[f['check'] for f in failures]}"

    def test_doctor_detects_an_injected_violation(self, conn, populated: Settings) -> None:
        """A check that cannot fail is not a check."""
        from tracker.report import run_doctor

        run(conn, populated)
        conn.execute(
            "UPDATE messages SET cost_source = 'unknown', cost_usd = 0.0 "
            "WHERE rowid = (SELECT MIN(rowid) FROM messages)"
        )
        failures = [r for r in run_doctor(conn) if not r["passed"]]
        assert any("NULL, never 0" in f["check"] for f in failures)
