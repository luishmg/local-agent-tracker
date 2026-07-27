"""
The Collector Run: discover -> read -> parse -> price -> upsert (architecture.md §4.1).

Transaction boundary is **one source file**, and the file's watermark is written
inside the same transaction as the rows it accounts for. That single choice is
what makes a crash mid-run safe: you never end up with rows the next run will
duplicate, nor a watermark past data that was never stored. It also makes the
first backfill resumable at file granularity.

Budget flags exist because the systemd timer fires every five minutes. A run that
would overrun its slot commits what it has, marks itself partial, and exits 0; the
next run picks up the files it never reached. Combined with the fast-skip in
`watermarks.decide()` -- which never opens a file whose (inode, size, mtime) are
unchanged -- steady-state runs touch only the handful of files actually being
written.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from tracker.config import Settings
from tracker.db import store
from tracker.ingest.reader import iter_complete_lines
from tracker.ingest.watermarks import (
    ReadDecision,
    decide,
    load_all_watermarks,
    save_watermark,
    stat_file,
)
from tracker.normalize.models import ParseResult
from tracker.pricing import PricingTable
from tracker.sources import claude_code as claude_source
from tracker.sources import pi as pi_source
from tracker.sources import pi_run_history as run_history_source
from tracker.sources.discovery import SourceFile, discover_all


def _utcnow_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


@dataclass(slots=True)
class RunStats:
    """What one Collector Run did. Persisted to `ingest_runs`."""

    run_id: str
    started_at: str
    finished_at: str | None = None
    files_scanned: int = 0
    files_read: int = 0
    files_rotated: int = 0
    files_skipped: int = 0
    bytes_read: int = 0
    lines_read: int = 0
    lines_skipped: int = 0
    messages_upserted: int = 0
    tool_calls_upserted: int = 0
    subagent_runs_upserted: int = 0
    unknown_models: set[str] = field(default_factory=set)
    partial: bool = False
    duration_ms: int | None = None

    def to_row(self) -> dict[str, Any]:
        import json

        return {
            "run_id": self.run_id,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "files_scanned": self.files_scanned,
            "files_read": self.files_read,
            "files_rotated": self.files_rotated,
            "bytes_read": self.bytes_read,
            "lines_read": self.lines_read,
            "lines_skipped": self.lines_skipped,
            "messages_upserted": self.messages_upserted,
            "tool_calls_upserted": self.tool_calls_upserted,
            "subagent_runs_upserted": self.subagent_runs_upserted,
            "unknown_models": json.dumps(sorted(self.unknown_models)),
            "partial": int(self.partial),
            "duration_ms": self.duration_ms,
        }


def _parse_source(
    source: SourceFile,
    lines: list[bytes],
    *,
    settings: Settings,
    carry: dict[str, Any],
) -> ParseResult:
    """Route a file's lines to the parser that owns its format.

    `carry` holds per-file streaming state across incremental reads -- the Claude
    seen-message-id set and the pi session/model state machine. Without it, a
    response whose content blocks straddle a run boundary would be counted twice,
    and a pi compaction entry read after the header would lose its model.
    """
    path = str(source.path)

    if source.kind in ("claude_session", "claude_subagent"):
        seen = carry.setdefault("seen_message_ids", set())
        return claude_source.parse_lines(
            lines,
            source_file=path,
            projects_root=settings.claude_projects_dir,
            seen_message_ids=seen,
        )

    if source.kind in ("pi_session", "pi_subagent_session"):
        state = carry.get("pi_state")
        if state is None:
            state = pi_source.SessionState(source.session_id)
            carry["pi_state"] = state
        return pi_source.parse_lines(
            lines,
            source_file=path,
            sessions_root=settings.pi_sessions_dir,
            state=state,
            fallback_session_id=source.session_id,
            parent_session_id=source.parent_session_id,
            agent_run_id=source.agent_run_id,
        )

    if source.kind == "pi_run_history":
        return run_history_source.parse_lines(lines, source_file=path)

    return ParseResult()


def _apply_pricing(
    result: ParseResult, pricing: PricingTable, stats: RunStats
) -> None:
    """Fill cost fields in place, collecting unknown models for one summary line.

    Per-row logging is deliberately avoided: a single unpriced model would
    otherwise emit thousands of identical warnings.
    """
    for message in result.messages:
        priced = pricing.price_message(
            agent=message.agent,
            model=message.model,
            usage=message.usage,
            reported_cost_usd=message.reported_cost_usd,
        )
        message.cost_usd = priced.cost_usd
        message.cost_source = priced.source
        message.pricing_version = priced.pricing_version
        if priced.source == "derived":
            message.derived_cost_usd = priced.cost_usd
        elif priced.source == "zero-rated":
            message.derived_cost_usd = 0.0
        if priced.unknown_model:
            stats.unknown_models.add(priced.unknown_model)


def collect(
    conn: Any,
    *,
    settings: Settings,
    pricing: PricingTable,
    max_seconds: int | None = None,
    max_files: int | None = None,
    progress: Callable[[SourceFile, int], None] | None = None,
) -> RunStats:
    """Run one full ingest pass and return what it did."""
    started = time.monotonic()
    stats = RunStats(run_id=f"run_{uuid.uuid4().hex[:16]}", started_at=_utcnow_iso())

    budget_seconds = settings.max_seconds if max_seconds is None else max_seconds
    file_budget = settings.max_files if max_files is None else max_files

    sources = discover_all(
        claude_projects_dir=settings.claude_projects_dir,
        pi_sessions_dir=settings.pi_sessions_dir,
        pi_run_history_path=settings.pi_run_history_path,
    )
    stats.files_scanned = len(sources)
    watermarks = load_all_watermarks(conn)

    for source in sources:
        if budget_seconds and (time.monotonic() - started) >= budget_seconds:
            stats.partial = True
            break
        if file_budget and stats.files_read >= file_budget:
            stats.partial = True
            break

        try:
            current = stat_file(source.path)
        except OSError:
            # Vanished between discovery and stat -- a session file being rotated.
            continue

        decision, start_offset = decide(watermarks.get(str(source.path)), current)
        if not decision.should_read:
            stats.files_skipped += 1
            continue
        if decision.is_rotation:
            stats.files_rotated += 1

        _ingest_one_file(
            conn,
            source=source,
            start_offset=start_offset,
            reset=decision.is_reset,
            current=current,
            settings=settings,
            pricing=pricing,
            stats=stats,
        )
        stats.files_read += 1
        if progress is not None:
            progress(source, stats.files_read)

    stats.finished_at = _utcnow_iso()
    stats.duration_ms = int((time.monotonic() - started) * 1000)

    with store.transaction(conn):
        conn.execute(
            """
            INSERT INTO ingest_runs (
                run_id, started_at, finished_at, files_scanned, files_read,
                files_rotated, bytes_read, lines_read, lines_skipped,
                messages_upserted, tool_calls_upserted, subagent_runs_upserted,
                unknown_models, partial, duration_ms
            ) VALUES (
                :run_id, :started_at, :finished_at, :files_scanned, :files_read,
                :files_rotated, :bytes_read, :lines_read, :lines_skipped,
                :messages_upserted, :tool_calls_upserted, :subagent_runs_upserted,
                :unknown_models, :partial, :duration_ms
            )
            """,
            stats.to_row(),
        )

    return stats


def _ingest_one_file(
    conn: Any,
    *,
    source: SourceFile,
    start_offset: int,
    reset: bool,
    current: Any,
    settings: Settings,
    pricing: PricingTable,
    stats: RunStats,
) -> None:
    """Read, parse, price and store one file inside a single transaction.

    Rows are flushed in batches for very large files, but the watermark advances
    only once at the end -- so a crash mid-file re-reads that file from its last
    committed offset rather than leaving a partially-accounted watermark.
    """
    now = _utcnow_iso()
    carry: dict[str, Any] = {}
    pending = ParseResult()
    offset = start_offset
    bytes_before = start_offset
    last_error: str | None = None

    batch: list[bytes] = []
    totals = {"messages": 0, "tool_calls": 0, "subagent_runs": 0}

    def flush(chunk: list[bytes]) -> None:
        if not chunk:
            return
        parsed = _parse_source(source, chunk, settings=settings, carry=carry)
        _apply_pricing(parsed, pricing, stats)
        pending.extend(parsed)

    try:
        with store.transaction(conn):
            for line, new_offset in iter_complete_lines(
                source.path,
                start_offset=start_offset,
                buffer_bytes=settings.read_buffer_bytes,
            ):
                batch.append(line)
                offset = new_offset
                if len(batch) >= settings.batch_size:
                    flush(batch)
                    batch = []
                    totals["messages"] += store.upsert_messages(
                        conn, (m.to_row(now) for m in pending.messages)
                    )
                    totals["tool_calls"] += store.upsert_tool_calls(
                        conn, (t.to_row(now) for t in pending.tool_calls)
                    )
                    totals["subagent_runs"] += store.upsert_subagent_runs(
                        conn, (s.to_row(now) for s in pending.subagent_runs)
                    )
                    pending.messages.clear()
                    pending.tool_calls.clear()
                    pending.subagent_runs.clear()

            flush(batch)
            totals["messages"] += store.upsert_messages(
                conn, (m.to_row(now) for m in pending.messages)
            )
            totals["tool_calls"] += store.upsert_tool_calls(
                conn, (t.to_row(now) for t in pending.tool_calls)
            )
            totals["subagent_runs"] += store.upsert_subagent_runs(
                conn, (s.to_row(now) for s in pending.subagent_runs)
            )

            if pending.lines_skipped > settings.max_skipped_lines_per_file:
                last_error = (
                    f"{pending.lines_skipped} unparseable lines; stopped counting"
                )

            save_watermark(
                conn,
                path=str(source.path),
                agent=source.agent,
                source_kind=source.kind,
                stat=current,
                byte_offset=offset,
                lines_ingested=pending.lines_read,
                lines_skipped=pending.lines_skipped,
                now=now,
                last_error=last_error,
                reset=reset,
            )
    except OSError as exc:
        # An unreadable file is one bad file, not a failed run (architecture.md §8).
        stats.lines_skipped += 1
        with store.transaction(conn):
            save_watermark(
                conn,
                path=str(source.path),
                agent=source.agent,
                source_kind=source.kind,
                stat=current,
                byte_offset=start_offset,
                lines_ingested=0,
                lines_skipped=0,
                now=now,
                last_error=f"{type(exc).__name__}: {exc}",
                reset=False,
            )
        return

    stats.messages_upserted += totals["messages"]
    stats.tool_calls_upserted += totals["tool_calls"]
    stats.subagent_runs_upserted += totals["subagent_runs"]
    stats.lines_read += pending.lines_read
    stats.lines_skipped += pending.lines_skipped
    stats.bytes_read += max(offset - bytes_before, 0)
