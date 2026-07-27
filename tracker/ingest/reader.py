"""
Offset-safe streaming line reader.

The invariant: **the returned offset never points into the middle of a line.**

This is not theoretical. The collector fires every five minutes and reads
transcripts that Claude Code and pi are actively appending to, so a file's last
line is routinely a partial JSON object. Advancing the watermark past it would
lose that Message permanently — the next run would begin after it, and the record
would never be parsed. architecture.md §4.1 does not mention this case.

So the reader yields only lines terminated by a newline, and reports the offset
just past the last one it yielded. A partial trailing line is left for next time,
by which point the agent will have finished writing it.

Memory: files are streamed a line at a time with a 1 MiB buffer. The largest line
observed across 513 MB of real transcripts was 47 KB, so peak RSS is dominated by
the batch size, not by any single file (architecture.md §8's resource budget).
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True)
class ReadResult:
    """Where the read stopped and why."""

    end_offset: int
    lines_yielded: int
    bytes_consumed: int
    had_partial_tail: bool


def iter_complete_lines(
    path: Path,
    *,
    start_offset: int = 0,
    buffer_bytes: int = 1 << 20,
) -> Iterator[tuple[bytes, int]]:
    """Yield `(line_without_newline, offset_after_this_line)` from `start_offset`.

    A trailing chunk with no newline is never yielded, and never contributes to a
    yielded offset — so a caller that stores the last offset it saw is always
    resumable at a line boundary.
    """
    with path.open("rb", buffering=buffer_bytes) as fh:
        fh.seek(start_offset)
        offset = start_offset
        for raw in fh:
            if not raw.endswith(b"\n"):
                # Partial final line: the agent is mid-write. Leave it.
                return
            offset += len(raw)
            line = raw[:-1]
            if line.endswith(b"\r"):
                line = line[:-1]
            if line.strip():
                yield line, offset


def read_lines(
    path: Path,
    *,
    start_offset: int = 0,
    buffer_bytes: int = 1 << 20,
) -> tuple[list[bytes], ReadResult]:
    """Eagerly read complete lines. Used where a caller wants the summary too."""
    lines: list[bytes] = []
    offset = start_offset
    for line, new_offset in iter_complete_lines(
        path, start_offset=start_offset, buffer_bytes=buffer_bytes
    ):
        lines.append(line)
        offset = new_offset

    size = path.stat().st_size
    return lines, ReadResult(
        end_offset=offset,
        lines_yielded=len(lines),
        bytes_consumed=offset - start_offset,
        had_partial_tail=offset < size,
    )
