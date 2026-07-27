"""
Watermarks: whether to open a file at all, and from what offset.

Two jobs, and the first one is what makes the 5-minute timer viable. On a steady
run, 1,300 of 1,309 files are byte-for-byte what they were five minutes ago. If
`(inode, size, mtime_ns)` all match what was stored, the file is never opened.
That is the difference between a 90-second run and a 200 ms one.

The second job is detecting when an offset has become meaningless. A file whose
inode changed was replaced; one that is now smaller than the stored offset was
truncated. Either way the offset is reset to zero and the file re-read in full,
which is safe precisely because every write is keyed on a `dedup_key` (ADR-0005) —
re-reading is a no-op, not a duplication.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any


class ReadDecision(Enum):
    """Why the collector is (or is not) going to open a file."""

    NEW = "new"
    APPENDED = "appended"
    ROTATED = "rotated"
    TRUNCATED = "truncated"
    REWRITTEN = "rewritten"
    UNCHANGED = "unchanged"

    @property
    def should_read(self) -> bool:
        return self is not ReadDecision.UNCHANGED

    @property
    def is_reset(self) -> bool:
        """Whether the stored offset must be discarded."""
        return self in (
            ReadDecision.ROTATED,
            ReadDecision.TRUNCATED,
            ReadDecision.REWRITTEN,
            ReadDecision.NEW,
        )

    @property
    def is_rotation(self) -> bool:
        """Whether this counts as a rotation for run telemetry."""
        return self in (
            ReadDecision.ROTATED,
            ReadDecision.TRUNCATED,
            ReadDecision.REWRITTEN,
        )


@dataclass(slots=True)
class Watermark:
    """The stored ingest state for one file."""

    path: str
    inode: int | None = None
    size_bytes: int = 0
    mtime_ns: int | None = None
    byte_offset: int = 0
    lines_ingested: int = 0
    lines_skipped: int = 0
    first_ingested_at: str | None = None
    last_ingested_at: str | None = None
    last_error: str | None = None


@dataclass(slots=True)
class FileStat:
    inode: int
    size_bytes: int
    mtime_ns: int


def stat_file(path: Path) -> FileStat:
    st = path.stat()
    return FileStat(inode=st.st_ino, size_bytes=st.st_size, mtime_ns=st.st_mtime_ns)


def load_watermark(conn: sqlite3.Connection, path: str) -> Watermark | None:
    row = conn.execute(
        "SELECT * FROM ingest_files WHERE path = ?", (path,)
    ).fetchone()
    if row is None:
        return None
    return Watermark(
        path=row["path"],
        inode=row["inode"],
        size_bytes=row["size_bytes"],
        mtime_ns=row["mtime_ns"],
        byte_offset=row["byte_offset"],
        lines_ingested=row["lines_ingested"],
        lines_skipped=row["lines_skipped"],
        first_ingested_at=row["first_ingested_at"],
        last_ingested_at=row["last_ingested_at"],
        last_error=row["last_error"],
    )


def load_all_watermarks(conn: sqlite3.Connection) -> dict[str, Watermark]:
    """Load every watermark at once.

    One query beats 1,309 point lookups when the whole point of the fast path is
    to avoid touching most files.
    """
    return {
        row["path"]: Watermark(
            path=row["path"],
            inode=row["inode"],
            size_bytes=row["size_bytes"],
            mtime_ns=row["mtime_ns"],
            byte_offset=row["byte_offset"],
            lines_ingested=row["lines_ingested"],
            lines_skipped=row["lines_skipped"],
            first_ingested_at=row["first_ingested_at"],
            last_ingested_at=row["last_ingested_at"],
            last_error=row["last_error"],
        )
        for row in conn.execute("SELECT * FROM ingest_files")
    }


def decide(watermark: Watermark | None, current: FileStat) -> tuple[ReadDecision, int]:
    """Return `(decision, start_offset)` for a file.

    The unchanged check requires all three of inode, size and mtime to match.
    Size alone would miss an in-place rewrite of identical length; mtime alone has
    filesystem-granularity hazards.

    The REWRITTEN case is subtler and was found by testing rather than reasoning:
    **inode reuse is common** -- deleting and immediately recreating a file
    frequently hands back the same inode -- so an inode check alone does not catch
    every replacement. If the file was modified (`mtime` moved) but has no bytes
    past the stored offset, it cannot have been appended to; it was rewritten in
    place. Reading from the old offset would return nothing and silently lose the
    new content, so the offset is discarded and the file re-read in full. Safe
    because every write is keyed on `dedup_key` -- if the content really is
    identical, the re-read is a no-op.
    """
    if watermark is None:
        return ReadDecision.NEW, 0

    if watermark.inode is not None and watermark.inode != current.inode:
        return ReadDecision.ROTATED, 0

    if current.size_bytes < watermark.byte_offset:
        return ReadDecision.TRUNCATED, 0

    unchanged_stat = (
        watermark.size_bytes == current.size_bytes
        and watermark.mtime_ns == current.mtime_ns
    )
    at_eof = watermark.byte_offset >= current.size_bytes

    if unchanged_stat and at_eof:
        return ReadDecision.UNCHANGED, watermark.byte_offset

    if at_eof:
        # Modified, but nothing past the offset to read -- an in-place rewrite.
        return ReadDecision.REWRITTEN, 0

    return ReadDecision.APPENDED, watermark.byte_offset


def save_watermark(
    conn: sqlite3.Connection,
    *,
    path: str,
    agent: str,
    source_kind: str,
    stat: FileStat,
    byte_offset: int,
    lines_ingested: int,
    lines_skipped: int,
    now: str,
    last_error: str | None = None,
    reset: bool = False,
) -> None:
    """Persist a watermark.

    Counters accumulate across runs, except after a reset -- a re-read from zero
    would otherwise double them and make `lines_ingested` meaningless.

    This must be called inside the same transaction as the rows it accounts for.
    A crash between the two would leave either rows the collector will duplicate
    on the next run, or a watermark past data that was never stored.
    """
    if reset:
        conn.execute(
            """
            INSERT INTO ingest_files (
                path, agent, source_kind, inode, size_bytes, mtime_ns, byte_offset,
                lines_ingested, lines_skipped, first_ingested_at, last_ingested_at,
                last_error
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (path) DO UPDATE SET
                agent = excluded.agent,
                source_kind = excluded.source_kind,
                inode = excluded.inode,
                size_bytes = excluded.size_bytes,
                mtime_ns = excluded.mtime_ns,
                byte_offset = excluded.byte_offset,
                lines_ingested = excluded.lines_ingested,
                lines_skipped = excluded.lines_skipped,
                first_ingested_at = COALESCE(ingest_files.first_ingested_at,
                                             excluded.first_ingested_at),
                last_ingested_at = excluded.last_ingested_at,
                last_error = excluded.last_error
            """,
            (path, agent, source_kind, stat.inode, stat.size_bytes, stat.mtime_ns,
             byte_offset, lines_ingested, lines_skipped, now, now, last_error),
        )
        return

    conn.execute(
        """
        INSERT INTO ingest_files (
            path, agent, source_kind, inode, size_bytes, mtime_ns, byte_offset,
            lines_ingested, lines_skipped, first_ingested_at, last_ingested_at,
            last_error
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (path) DO UPDATE SET
            agent = excluded.agent,
            source_kind = excluded.source_kind,
            inode = excluded.inode,
            size_bytes = excluded.size_bytes,
            mtime_ns = excluded.mtime_ns,
            byte_offset = excluded.byte_offset,
            lines_ingested = ingest_files.lines_ingested + excluded.lines_ingested,
            lines_skipped = ingest_files.lines_skipped + excluded.lines_skipped,
            first_ingested_at = COALESCE(ingest_files.first_ingested_at,
                                         excluded.first_ingested_at),
            last_ingested_at = excluded.last_ingested_at,
            last_error = excluded.last_error
        """,
        (path, agent, source_kind, stat.inode, stat.size_bytes, stat.mtime_ns,
         byte_offset, lines_ingested, lines_skipped, now, now, last_error),
    )


def watermark_summary(conn: sqlite3.Connection) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT COUNT(*) AS files,
               COALESCE(SUM(byte_offset), 0) AS bytes,
               COALESCE(SUM(lines_ingested), 0) AS lines,
               COALESCE(SUM(lines_skipped), 0) AS skipped
          FROM ingest_files
        """
    ).fetchone()
    return {
        "files": row["files"],
        "bytes": row["bytes"],
        "lines": row["lines"],
        "skipped": row["skipped"],
    }
