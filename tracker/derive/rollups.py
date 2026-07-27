"""
Session rollups, recomputed from `messages` rather than incremented.

A full recompute of the touched sessions is idempotent by construction; a counter
is not. Since the whole store rests on re-running the collector being a no-op
(architecture.md §8), a rollup that drifts every time a file is re-read would
quietly undermine that guarantee. At personal scale the recompute is cheap.

`has_unknown_pricing` earns its column here. SQL `SUM()` skips NULLs silently, so
a session containing one unpriced Message reports a total that looks complete and
is not. The flag lets reports say "incomplete" instead of "cheap".
"""

from __future__ import annotations

import sqlite3

_ROLLUP_SQL = """
INSERT INTO sessions (
    agent, session_id, cwd, project_slug, git_branch, started_at, ended_at,
    duration_ms, message_count, compaction_count, subagent_message_count,
    models_used, model_switch_count, input_tokens, output_tokens,
    cache_read_tokens, cache_write_tokens, total_tokens, cost_usd,
    has_unknown_pricing, tool_call_count, tool_error_count, updated_at
)
SELECT
    m.agent,
    m.session_id,
    -- A session's cwd/branch are stable; MIN over non-NULLs picks the first seen.
    MIN(m.cwd),
    MIN(m.project_slug),
    MIN(m.git_branch),
    MIN(m.ts),
    MAX(m.ts),
    MAX(m.ts_epoch_ms) - MIN(m.ts_epoch_ms),
    COUNT(*),
    SUM(CASE WHEN m.kind = 'compaction' THEN 1 ELSE 0 END),
    SUM(CASE WHEN m.is_sidechain = 1 THEN 1 ELSE 0 END),
    (SELECT json_group_array(model) FROM (
        SELECT DISTINCT m2.model AS model
          FROM messages m2
         WHERE m2.agent = m.agent AND m2.session_id = m.session_id
           AND m2.model IS NOT NULL
         ORDER BY m2.model
    )),
    COUNT(DISTINCT m.model) - 1,
    SUM(m.input_tokens),
    SUM(m.output_tokens),
    SUM(m.cache_read_tokens),
    SUM(m.cache_write_tokens),
    SUM(m.total_tokens),
    SUM(m.cost_usd),
    -- SUM() skips NULLs, so without this flag a partly-unpriced session looks
    -- cheap rather than incomplete.
    MAX(CASE WHEN m.cost_usd IS NULL THEN 1 ELSE 0 END),
    COALESCE((SELECT COUNT(*) FROM tool_calls tc
               WHERE tc.agent = m.agent AND tc.session_id = m.session_id), 0),
    COALESCE((SELECT COUNT(*) FROM tool_calls tc
               WHERE tc.agent = m.agent AND tc.session_id = m.session_id
                 AND tc.is_error = 1), 0),
    :now
  FROM messages m
 {where}
 GROUP BY m.agent, m.session_id
ON CONFLICT (agent, session_id) DO UPDATE SET
    cwd = excluded.cwd,
    project_slug = excluded.project_slug,
    git_branch = excluded.git_branch,
    started_at = excluded.started_at,
    ended_at = excluded.ended_at,
    duration_ms = excluded.duration_ms,
    message_count = excluded.message_count,
    compaction_count = excluded.compaction_count,
    subagent_message_count = excluded.subagent_message_count,
    models_used = excluded.models_used,
    model_switch_count = excluded.model_switch_count,
    input_tokens = excluded.input_tokens,
    output_tokens = excluded.output_tokens,
    cache_read_tokens = excluded.cache_read_tokens,
    cache_write_tokens = excluded.cache_write_tokens,
    total_tokens = excluded.total_tokens,
    cost_usd = excluded.cost_usd,
    has_unknown_pricing = excluded.has_unknown_pricing,
    tool_call_count = excluded.tool_call_count,
    tool_error_count = excluded.tool_error_count,
    updated_at = excluded.updated_at
"""


def rebuild_sessions(
    conn: sqlite3.Connection, *, now: str, since_epoch_ms: int | None = None
) -> int:
    """Recompute session rollups. Returns the number of sessions written.

    `since_epoch_ms` narrows the recompute to sessions with recent activity, which
    is the steady-state path; omit it for a full rebuild after a backfill.
    """
    if since_epoch_ms is None:
        where = ""
        params: dict[str, object] = {"now": now}
    else:
        where = """
        WHERE EXISTS (
            SELECT 1 FROM messages recent
             WHERE recent.agent = m.agent
               AND recent.session_id = m.session_id
               AND recent.ts_epoch_ms >= :since
        )
        """
        params = {"now": now, "since": since_epoch_ms}

    cur = conn.execute(_ROLLUP_SQL.format(where=where), params)
    return max(cur.rowcount, 0)


def model_switch_count_is_approximate() -> str:
    """Documents a known limitation rather than hiding it.

    `sessions.model_switch_count` is `COUNT(DISTINCT model) - 1`, which counts how
    many *different* models a session used, not how many times it switched. A
    session that alternates A -> B -> A reports 1, not 2. Exact switch counts need
    pi's `model_change` entries, which increment 1 does not store as rows.
    """
    return (
        "model_switch_count = COUNT(DISTINCT model) - 1: distinct models used, "
        "not transitions. A -> B -> A reports 1."
    )
