"""
Discovery, the offset-safe reader, and watermark decisions.

The partial-trailing-line test is the one that would be easiest to omit and most
expensive to be missing: the collector reads files the agents are writing to, so
a watermark that advances into a half-written line drops that Message forever.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from tests.factories import (
    claude_assistant_lines,
    claude_session_file,
    claude_subagent_file,
    pi_assistant_entry,
    pi_claude_artifacts_file,
    pi_session_file,
    pi_session_header,
    pi_subagent_session_file,
    write_jsonl,
)
from tracker.db.store import open_db
from tracker.ingest.reader import iter_complete_lines, read_lines
from tracker.ingest.watermarks import (
    ReadDecision,
    Watermark,
    decide,
    load_all_watermarks,
    load_watermark,
    save_watermark,
    stat_file,
    watermark_summary,
)
from tracker.sources.discovery import discover_all, discover_claude, discover_pi_sessions


@pytest.fixture
def conn(tmp_path: Path):
    with open_db(tmp_path / "t.db") as c:
        yield c


class TestReader:
    def test_partial_trailing_line_is_not_consumed(self, tmp_path: Path) -> None:
        """The single most important reader property. A file mid-append ends in a
        half-written JSON object; advancing past it loses that Message for good."""
        path = tmp_path / "s.jsonl"
        complete = json.dumps({"n": 1}) + "\n"
        path.write_text(complete + '{"n": 2, "partial"', encoding="utf-8")

        lines, result = read_lines(path)

        assert len(lines) == 1
        assert result.end_offset == len(complete)
        assert result.had_partial_tail is True
        assert result.end_offset < path.stat().st_size

    def test_appending_the_rest_yields_it_exactly_once(self, tmp_path: Path) -> None:
        path = tmp_path / "s.jsonl"
        first = json.dumps({"n": 1}) + "\n"
        path.write_text(first + '{"n": 2', encoding="utf-8")
        _, first_result = read_lines(path)

        with path.open("a", encoding="utf-8") as fh:
            fh.write('}\n')

        lines, second = read_lines(path, start_offset=first_result.end_offset)
        assert [json.loads(raw) for raw in lines] == [{"n": 2}]
        assert second.end_offset == path.stat().st_size
        assert second.had_partial_tail is False

    def test_resuming_from_an_offset_reads_only_new_bytes(self, tmp_path: Path) -> None:
        path = tmp_path / "s.jsonl"
        write_jsonl(path, [{"n": 1}, {"n": 2}])
        _, first = read_lines(path)

        write_jsonl(path, [{"n": 1}, {"n": 2}, {"n": 3}])
        lines, second = read_lines(path, start_offset=first.end_offset)

        assert [json.loads(raw) for raw in lines] == [{"n": 3}]
        assert second.bytes_consumed < path.stat().st_size

    def test_blank_lines_are_skipped_but_still_advance_the_offset(self, tmp_path: Path) -> None:
        path = tmp_path / "s.jsonl"
        path.write_text('{"n":1}\n\n\n{"n":2}\n', encoding="utf-8")

        lines, result = read_lines(path)
        assert len(lines) == 2
        assert result.end_offset == path.stat().st_size

    def test_empty_file_yields_nothing(self, tmp_path: Path) -> None:
        path = tmp_path / "s.jsonl"
        path.write_text("", encoding="utf-8")
        lines, result = read_lines(path)
        assert lines == []
        assert result.end_offset == 0

    def test_crlf_line_endings_are_stripped(self, tmp_path: Path) -> None:
        path = tmp_path / "s.jsonl"
        path.write_bytes(b'{"n":1}\r\n')
        lines, _ = read_lines(path)
        assert json.loads(lines[0]) == {"n": 1}

    def test_iterator_offsets_are_always_line_boundaries(self, tmp_path: Path) -> None:
        path = tmp_path / "s.jsonl"
        write_jsonl(path, [{"n": i} for i in range(5)])

        for _line, offset in iter_complete_lines(path):
            resumed = [
                json.loads(raw) for raw, _ in iter_complete_lines(path, start_offset=offset)
            ]
            assert all(isinstance(e, dict) for e in resumed), "offset split a line"


class TestWatermarkDecisions:
    def test_unseen_file_is_new(self) -> None:
        from tracker.ingest.watermarks import FileStat

        decision, offset = decide(None, FileStat(inode=1, size_bytes=100, mtime_ns=5))
        assert decision is ReadDecision.NEW
        assert offset == 0
        assert decision.should_read

    def test_identical_stat_is_unchanged_and_skipped(self) -> None:
        """The fast path: on a steady run this covers ~1,300 of 1,309 files."""
        from tracker.ingest.watermarks import FileStat

        wm = Watermark(path="/x", inode=1, size_bytes=100, mtime_ns=5, byte_offset=100)
        decision, _ = decide(wm, FileStat(inode=1, size_bytes=100, mtime_ns=5))

        assert decision is ReadDecision.UNCHANGED
        assert not decision.should_read

    def test_grown_file_resumes_from_the_stored_offset(self) -> None:
        from tracker.ingest.watermarks import FileStat

        wm = Watermark(path="/x", inode=1, size_bytes=100, mtime_ns=5, byte_offset=100)
        decision, offset = decide(wm, FileStat(inode=1, size_bytes=250, mtime_ns=9))

        assert decision is ReadDecision.APPENDED
        assert offset == 100
        assert not decision.is_reset

    def test_changed_inode_forces_a_full_reread(self) -> None:
        from tracker.ingest.watermarks import FileStat

        wm = Watermark(path="/x", inode=1, size_bytes=100, mtime_ns=5, byte_offset=100)
        decision, offset = decide(wm, FileStat(inode=999, size_bytes=100, mtime_ns=5))

        assert decision is ReadDecision.ROTATED
        assert offset == 0
        assert decision.is_reset

    def test_shrunk_file_forces_a_full_reread(self) -> None:
        from tracker.ingest.watermarks import FileStat

        wm = Watermark(path="/x", inode=1, size_bytes=500, mtime_ns=5, byte_offset=500)
        decision, offset = decide(wm, FileStat(inode=1, size_bytes=10, mtime_ns=9))

        assert decision is ReadDecision.TRUNCATED
        assert offset == 0

    def test_same_size_new_mtime_at_eof_is_a_rewrite_not_an_append(self) -> None:
        """An in-place rewrite of identical length is invisible to a size check,
        and inode reuse means the inode check can miss it too.

        Reading from the stored offset would return nothing and silently lose the
        new content, so the offset must be discarded instead.
        """
        from tracker.ingest.watermarks import FileStat

        wm = Watermark(path="/x", inode=1, size_bytes=100, mtime_ns=5, byte_offset=100)
        decision, offset = decide(wm, FileStat(inode=1, size_bytes=100, mtime_ns=77))

        assert decision is ReadDecision.REWRITTEN
        assert offset == 0, "a rewrite must be re-read in full"
        assert decision.should_read and decision.is_reset

    def test_same_size_new_mtime_with_unread_bytes_is_an_append(self) -> None:
        """The offset is behind EOF, so there is genuinely new data to append."""
        from tracker.ingest.watermarks import FileStat

        wm = Watermark(path="/x", inode=1, size_bytes=100, mtime_ns=5, byte_offset=60)
        decision, offset = decide(wm, FileStat(inode=1, size_bytes=100, mtime_ns=77))

        assert decision is ReadDecision.APPENDED
        assert offset == 60

    def test_offset_behind_size_is_read_even_if_stat_matches(self) -> None:
        """A previous run stopped at a partial line; the rest is still pending."""
        from tracker.ingest.watermarks import FileStat

        wm = Watermark(path="/x", inode=1, size_bytes=100, mtime_ns=5, byte_offset=60)
        decision, offset = decide(wm, FileStat(inode=1, size_bytes=100, mtime_ns=5))

        assert decision is ReadDecision.APPENDED
        assert offset == 60


class TestWatermarkPersistence:
    def test_round_trip(self, conn, tmp_path: Path) -> None:
        path = tmp_path / "s.jsonl"
        write_jsonl(path, [{"n": 1}])
        st = stat_file(path)

        save_watermark(
            conn, path=str(path), agent="pi", source_kind="pi_session", stat=st,
            byte_offset=10, lines_ingested=1, lines_skipped=0, now="2026-07-26T00:00:00Z",
        )
        wm = load_watermark(conn, str(path))

        assert wm is not None
        assert wm.byte_offset == 10
        assert wm.inode == st.inode

    def test_counters_accumulate_across_runs(self, conn, tmp_path: Path) -> None:
        path = tmp_path / "s.jsonl"
        write_jsonl(path, [{"n": 1}])
        st = stat_file(path)
        args = dict(path=str(path), agent="pi", source_kind="pi_session", stat=st,
                    now="2026-07-26T00:00:00Z")

        save_watermark(conn, byte_offset=10, lines_ingested=5, lines_skipped=1, **args)
        save_watermark(conn, byte_offset=20, lines_ingested=3, lines_skipped=2, **args)

        wm = load_watermark(conn, str(path))
        assert wm is not None
        assert (wm.lines_ingested, wm.lines_skipped) == (8, 3)
        assert wm.byte_offset == 20

    def test_reset_replaces_counters_rather_than_doubling_them(self, conn, tmp_path: Path) -> None:
        """After a rotation the file is re-read from zero; accumulating would make
        `lines_ingested` count the same lines twice."""
        path = tmp_path / "s.jsonl"
        write_jsonl(path, [{"n": 1}])
        st = stat_file(path)
        args = dict(path=str(path), agent="pi", source_kind="pi_session", stat=st,
                    now="2026-07-26T00:00:00Z")

        save_watermark(conn, byte_offset=100, lines_ingested=50, lines_skipped=0, **args)
        save_watermark(conn, byte_offset=40, lines_ingested=20, lines_skipped=0, reset=True, **args)

        wm = load_watermark(conn, str(path))
        assert wm is not None
        assert wm.lines_ingested == 20
        assert wm.byte_offset == 40

    def test_first_ingested_at_is_preserved(self, conn, tmp_path: Path) -> None:
        path = tmp_path / "s.jsonl"
        write_jsonl(path, [{"n": 1}])
        st = stat_file(path)
        common = dict(path=str(path), agent="pi", source_kind="pi_session", stat=st,
                      byte_offset=10, lines_ingested=1, lines_skipped=0)

        save_watermark(conn, now="2026-07-01T00:00:00Z", **common)
        save_watermark(conn, now="2026-07-26T00:00:00Z", **common)

        wm = load_watermark(conn, str(path))
        assert wm is not None
        assert wm.first_ingested_at == "2026-07-01T00:00:00Z"
        assert wm.last_ingested_at == "2026-07-26T00:00:00Z"

    def test_load_all_and_summary(self, conn, tmp_path: Path) -> None:
        for i in range(3):
            p = tmp_path / f"s{i}.jsonl"
            write_jsonl(p, [{"n": i}])
            save_watermark(
                conn, path=str(p), agent="pi", source_kind="pi_session",
                stat=stat_file(p), byte_offset=10, lines_ingested=2, lines_skipped=1,
                now="2026-07-26T00:00:00Z",
            )

        assert len(load_all_watermarks(conn)) == 3
        summary = watermark_summary(conn)
        assert summary["files"] == 3
        assert summary["lines"] == 6
        assert summary["skipped"] == 3


class TestDiscovery:
    def test_claude_sessions_and_subagents_are_both_found(self, tmp_path: Path) -> None:
        """Subagent tokens are real spend (architecture.md §3.2)."""
        root = tmp_path / "projects"
        claude_session_file(root, session_id="sess-1")
        claude_subagent_file(root, session_id="sess-1", agent_id="a4984de84563b051b")

        found = list(discover_claude(root))
        kinds = {f.kind for f in found}

        assert kinds == {"claude_session", "claude_subagent"}
        sub = next(f for f in found if f.kind == "claude_subagent")
        assert sub.parent_session_id == "sess-1"
        assert sub.agent_run_id == "a4984de84563b051b"

    def test_pi_artifacts_directory_is_excluded(self, tmp_path: Path) -> None:
        """The 120 `claude-code-artifacts` files are Claude Code SDK stream-json,
        not pi transcripts -- and 82 of 85 of their sessions are already counted
        under ~/.claude/projects, so ingesting them double-counts."""
        root = tmp_path / "sessions"
        pi_session_file(root)
        pi_claude_artifacts_file(root)

        found = list(discover_pi_sessions(root))

        assert len(found) == 1
        assert "claude-code-artifacts" not in str(found[0].path)

    def test_nested_pi_subagent_sessions_are_found(self, tmp_path: Path) -> None:
        """Five levels deep -- a `<slug>/*.jsonl` glob misses them entirely, and
        they hold real uncounted spend."""
        root = tmp_path / "sessions"
        pi_session_file(root)
        pi_subagent_session_file(root, tool_call_id="b2954803")

        found = list(discover_pi_sessions(root))
        nested = [f for f in found if f.kind == "pi_subagent_session"]

        assert len(nested) == 1
        assert nested[0].agent_run_id == "b2954803"
        assert nested[0].parent_session_id == "019f0000-0000-7000-8000-000000000000"

    def test_pi_session_id_is_recoverable_from_the_filename(self, tmp_path: Path) -> None:
        """Needed when an incremental read starts past the `session` header line."""
        root = tmp_path / "sessions"
        pi_session_file(root, session_id="019f1111-2222-7333-8444-555555555555")
        found = list(discover_pi_sessions(root))
        assert found[0].session_id == "019f1111-2222-7333-8444-555555555555"

    def test_non_transcript_files_are_ignored(self, tmp_path: Path) -> None:
        root = tmp_path / "sessions"
        pi_session_file(root)
        (root / "--home-user-Projects-demo--" / "notes.txt").write_text("x", encoding="utf-8")
        (root / "--home-user-Projects-demo--" / "random.jsonl").write_text("{}", encoding="utf-8")

        found = list(discover_pi_sessions(root))
        assert len(found) == 1, "only the timestamp_uuid.jsonl shape is a pi session"

    def test_missing_directories_are_not_an_error(self, tmp_path: Path) -> None:
        assert list(discover_claude(tmp_path / "nope")) == []
        assert list(discover_pi_sessions(tmp_path / "nope")) == []

    def test_discover_all_is_deterministically_ordered(self, tmp_path: Path) -> None:
        """With --max-files or a time budget, a run reads a prefix of this list.
        Unstable order would make coverage depend on filesystem iteration."""
        claude_root = tmp_path / "projects"
        pi_root = tmp_path / "sessions"
        for i in range(4):
            claude_session_file(claude_root, session_id=f"sess-{i}")
            pi_session_file(pi_root, session_id=f"019f000{i}-0000-7000-8000-00000000000{i}",
                            ts_prefix=f"2026-07-0{i + 1}T10-00-00-000Z")
        run_history = tmp_path / "run-history.jsonl"
        write_jsonl(run_history, [{"agent": "X", "ts": 1, "status": "ok", "duration": 1}])

        kwargs = dict(claude_projects_dir=claude_root, pi_sessions_dir=pi_root,
                      pi_run_history_path=run_history)
        first = [str(f.path) for f in discover_all(**kwargs)]
        second = [str(f.path) for f in discover_all(**kwargs)]

        assert first == second
        assert len(first) == 9
