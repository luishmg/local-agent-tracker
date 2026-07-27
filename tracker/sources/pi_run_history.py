"""
`~/.pi/agent/run-history.jsonl` — one line per subagent run.

Carries no tokens or cost (architecture.md §3.1), so this is a duration and
reliability source only. `task` is the subagent's prompt, which is conversation
content: only its digest and length are kept (architecture.md §8).

The file has no id of any kind, so identity is a hash over the four fields that
jointly distinguish one run from another.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from tracker.normalize.dedup import pi_run_history_key, task_digest
from tracker.normalize.models import NormalizedSubagentRun, ParseResult

AGENT = "pi"


def _iso_from_epoch_seconds(ts: Any) -> str | None:
    """`ts` is epoch *seconds* here, unlike the millisecond timestamps elsewhere."""
    if not isinstance(ts, (int, float)):
        return None
    try:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).isoformat()
    except (ValueError, OverflowError, OSError):
        return None


def parse_lines(lines: Any, *, source_file: str) -> ParseResult:
    result = ParseResult()

    for raw in lines:
        result.lines_read += 1
        try:
            entry = json.loads(raw)
        except (ValueError, TypeError):
            result.lines_skipped += 1
            continue
        if not isinstance(entry, dict):
            result.lines_skipped += 1
            continue

        ts = entry.get("ts")
        started_at = _iso_from_epoch_seconds(ts)
        if started_at is None:
            result.lines_skipped += 1
            continue

        duration = entry.get("duration")
        duration_ms = int(duration) if isinstance(duration, (int, float)) else None
        ended_at = (
            _iso_from_epoch_seconds(float(ts) + duration_ms / 1000)
            if duration_ms is not None and isinstance(ts, (int, float))
            else None
        )

        digest, length = task_digest(entry.get("task"))
        exit_code = entry.get("exit")

        result.subagent_runs.append(
            NormalizedSubagentRun(
                dedup_key=pi_run_history_key(
                    entry.get("agent"), ts, duration, entry.get("status")
                ),
                agent=AGENT,
                source="pi-run-history",
                source_file=source_file,
                agent_type=entry.get("agent"),
                task_sha256=digest,
                task_len=length,
                started_at=started_at,
                ended_at=ended_at,
                duration_ms=duration_ms,
                status=entry.get("status"),
                exit_code=exit_code if isinstance(exit_code, int) else None,
            )
        )

    return result
