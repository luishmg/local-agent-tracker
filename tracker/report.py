"""
Terminal reports (architecture.md §4.5).

Split deliberately: `q_*` functions return plain `list[dict]` and know nothing
about presentation; the `render_*` functions turn those into rich tables. The
deferred static dashboard (§4.4) embeds the same dicts as JSON, so it will reuse
the query half untouched rather than growing a second set of SQL.

Every aggregate carries an `unknown_models` / `has_unknown` companion count.
`SUM(cost_usd)` skips NULLs, so a day containing unpriced Messages otherwise
renders as a smaller number with no indication that it is incomplete.
"""

from __future__ import annotations

import sqlite3
from typing import Any

Row = dict[str, Any]


def _rows(cur: sqlite3.Cursor) -> list[Row]:
    return [dict(r) for r in cur.fetchall()]


def _since_clause(since_epoch_ms: int | None, column: str = "ts_epoch_ms") -> tuple[str, dict]:
    if since_epoch_ms is None:
        return "", {}
    return f"WHERE {column} >= :since", {"since": since_epoch_ms}


# --------------------------------------------------------------------------- #
# Queries
# --------------------------------------------------------------------------- #

def q_daily(conn: sqlite3.Connection, *, since_epoch_ms: int | None = None) -> list[Row]:
    where, params = _since_clause(since_epoch_ms)
    return _rows(conn.execute(
        f"""
        SELECT date(ts) AS day,
               agent,
               COUNT(*) AS messages,
               COUNT(DISTINCT session_id) AS sessions,
               SUM(input_tokens) AS input_tokens,
               SUM(output_tokens) AS output_tokens,
               SUM(cache_read_tokens) AS cache_read_tokens,
               SUM(cache_write_tokens) AS cache_write_tokens,
               SUM(total_tokens) AS total_tokens,
               SUM(cost_usd) AS cost_usd,
               SUM(CASE WHEN cost_usd IS NULL THEN 1 ELSE 0 END) AS unpriced
          FROM messages
          {where}
         GROUP BY day, agent
         ORDER BY day DESC, agent
        """,
        params,
    ))


def q_models(conn: sqlite3.Connection, *, since_epoch_ms: int | None = None) -> list[Row]:
    """Per-model aggregate — the primary question of architecture.md §1.

    `reasoning_pct` is pi-only and diagnostic: reasoning tokens are a subset of
    output, so this is "what share of the output was thinking", not extra spend.
    """
    where, params = _since_clause(since_epoch_ms)
    return _rows(conn.execute(
        f"""
        SELECT COALESCE(model, '(none)') AS model,
               agent,
               COUNT(*) AS messages,
               SUM(input_tokens) AS input_tokens,
               SUM(output_tokens) AS output_tokens,
               SUM(cache_read_tokens) AS cache_read_tokens,
               SUM(cache_write_tokens) AS cache_write_tokens,
               SUM(total_tokens) AS total_tokens,
               SUM(cost_usd) AS cost_usd,
               AVG(latency_ms) AS avg_latency_ms,
               SUM(retry_count) AS retries,
               SUM(CASE WHEN cost_usd IS NULL THEN 1 ELSE 0 END) AS unpriced,
               CASE WHEN SUM(output_tokens) > 0
                    THEN 100.0 * SUM(reasoning_tokens) / SUM(output_tokens) END
                    AS reasoning_pct,
               CASE WHEN SUM(cache_read_tokens + input_tokens + cache_write_tokens) > 0
                    THEN 100.0 * SUM(cache_read_tokens)
                         / SUM(cache_read_tokens + input_tokens + cache_write_tokens) END
                    AS cache_hit_pct
          FROM messages
          {where}
         GROUP BY model, agent
         ORDER BY cost_usd DESC NULLS LAST, total_tokens DESC
        """,
        params,
    ))


def q_sessions(
    conn: sqlite3.Connection, *, since_epoch_ms: int | None = None, limit: int = 20
) -> list[Row]:
    where, params = _since_clause(since_epoch_ms, "started_at_ms")
    params["limit"] = limit
    return _rows(conn.execute(
        f"""
        SELECT * FROM (
            SELECT agent, session_id, project_slug, git_branch,
                   started_at, message_count, total_tokens, cost_usd,
                   has_unknown_pricing, tool_call_count, tool_error_count,
                   models_used,
                   CAST(strftime('%s', started_at) AS INTEGER) * 1000 AS started_at_ms
              FROM sessions
        )
        {where}
         ORDER BY started_at DESC
         LIMIT :limit
        """,
        params,
    ))


def q_tools(conn: sqlite3.Connection, *, since_epoch_ms: int | None = None) -> list[Row]:
    """Tool reliability. architecture.md §5 keeps this with cost deliberately:
    error -> retry loops are a cost phenomenon."""
    where, params = _since_clause(since_epoch_ms)
    return _rows(conn.execute(
        f"""
        SELECT COALESCE(tool_name, '(unknown)') AS tool_name,
               agent,
               COUNT(*) AS calls,
               SUM(CASE WHEN is_error = 1 THEN 1 ELSE 0 END) AS errors,
               100.0 * SUM(CASE WHEN is_error = 1 THEN 1 ELSE 0 END) / COUNT(*)
                   AS error_pct,
               AVG(duration_ms) AS avg_duration_ms
          FROM tool_calls
          {where}
         GROUP BY tool_name, agent
         ORDER BY calls DESC
        """,
        params,
    ))


def q_status(conn: sqlite3.Connection) -> Row:
    def scalar(sql: str) -> Any:
        row = conn.execute(sql).fetchone()
        return row[0] if row else None

    return {
        "messages": scalar("SELECT COUNT(*) FROM messages"),
        "sessions": scalar("SELECT COUNT(*) FROM sessions"),
        "tool_calls": scalar("SELECT COUNT(*) FROM tool_calls"),
        "subagent_runs": scalar("SELECT COUNT(*) FROM subagent_runs"),
        "watermarks": scalar("SELECT COUNT(*) FROM ingest_files"),
        "experiments": scalar("SELECT COUNT(*) FROM experiments"),
        "total_cost_usd": scalar("SELECT SUM(cost_usd) FROM messages"),
        "unpriced_messages": scalar("SELECT COUNT(*) FROM messages WHERE cost_usd IS NULL"),
        "earliest": scalar("SELECT MIN(ts) FROM messages"),
        "latest": scalar("SELECT MAX(ts) FROM messages"),
        "last_run": scalar("SELECT MAX(finished_at) FROM ingest_runs"),
    }


def q_unknown_models(conn: sqlite3.Connection) -> list[Row]:
    return _rows(conn.execute(
        """
        SELECT COALESCE(model, '(none)') AS model, agent, COUNT(*) AS messages,
               SUM(total_tokens) AS total_tokens
          FROM messages
         WHERE cost_usd IS NULL
         GROUP BY model, agent
         ORDER BY total_tokens DESC
        """
    ))


# --------------------------------------------------------------------------- #
# Invariant checks (`tracker doctor`)
# --------------------------------------------------------------------------- #

#: Each entry is (name, sql, expectation). A check "passes" when it returns no
#: rows -- these are the properties that must hold for the numbers to be trusted.
DOCTOR_CHECKS: tuple[tuple[str, str, str], ...] = (
    (
        "no duplicate billing of one API response",
        """
        SELECT provider_msg_id, COUNT(*) AS n
          FROM messages
         WHERE agent = 'claude-code' AND provider_msg_id IS NOT NULL
         GROUP BY provider_msg_id HAVING COUNT(*) > 1
        """,
        "ADR-0005: message.id is the grain; duplicates mean the collapse regressed",
    ),
    (
        "unknown pricing is NULL, never 0",
        "SELECT dedup_key FROM messages WHERE cost_source = 'unknown' AND cost_usd IS NOT NULL",
        "ADR-0004: a guessed zero makes an incomplete day look cheap",
    ),
    (
        "zero-rated rows really are zero",
        "SELECT dedup_key FROM messages WHERE cost_source = 'zero-rated' AND cost_usd != 0",
        "<synthetic> is unbilled, not unknown",
    ),
    (
        "derived costs carry a pricing version",
        "SELECT dedup_key FROM messages WHERE cost_source = 'derived' AND pricing_version IS NULL",
        "ADR-0004: history must never be silently repriced",
    ),
    (
        "session rollups agree with the message grain",
        """
        SELECT s.agent, s.session_id
          FROM sessions s
          JOIN messages m ON m.agent = s.agent AND m.session_id = s.session_id
         GROUP BY s.agent, s.session_id
        HAVING ABS(COALESCE(s.cost_usd, 0) - COALESCE(SUM(m.cost_usd), 0)) > 1e-9
        """,
        "rollups are recomputed, not incremented -- drift means that broke",
    ),
    (
        "watermarks never exceed their file size",
        "SELECT path FROM ingest_files WHERE byte_offset > size_bytes",
        "an offset past EOF would skip data forever",
    ),
    (
        "every message has a session",
        "SELECT dedup_key FROM messages WHERE session_id IS NULL OR session_id = ''",
        "an unattributable message cannot be rolled up",
    ),
)


def run_doctor(conn: sqlite3.Connection) -> list[Row]:
    """Run every invariant check. Returns one row per check with its violations."""
    results: list[Row] = []
    for name, sql, why in DOCTOR_CHECKS:
        violations = conn.execute(sql).fetchall()
        results.append({
            "check": name,
            "passed": not violations,
            "violations": len(violations),
            "why": why,
        })
    return results


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #

def _usd(value: Any) -> str:
    if value is None:
        return "—"
    return f"${value:,.4f}" if value < 1 else f"${value:,.2f}"


def _num(value: Any) -> str:
    return "—" if value is None else f"{int(value):,}"


def _pct(value: Any) -> str:
    return "—" if value is None else f"{value:.1f}%"


def _ms(value: Any) -> str:
    return "—" if value is None else f"{value / 1000:.1f}s"


def render_daily(rows: list[Row]) -> Any:
    from rich.table import Table

    table = Table(title="cost by day", title_justify="left")
    for col in ("day", "agent"):
        table.add_column(col)
    for col in ("messages", "sessions", "in", "out", "cache rd", "cache wr", "cost"):
        table.add_column(col, justify="right")

    for r in rows:
        cost = _usd(r["cost_usd"])
        if r["unpriced"]:
            cost += f" (+{r['unpriced']} unpriced)"
        table.add_row(
            r["day"], r["agent"], _num(r["messages"]), _num(r["sessions"]),
            _num(r["input_tokens"]), _num(r["output_tokens"]),
            _num(r["cache_read_tokens"]), _num(r["cache_write_tokens"]), cost,
        )
    return table


def render_models(rows: list[Row]) -> Any:
    from rich.table import Table

    table = Table(title="cost by model", title_justify="left")
    table.add_column("model")
    table.add_column("agent")
    for col in ("msgs", "in", "out", "cache hit", "reasoning", "avg lat", "retries", "cost"):
        table.add_column(col, justify="right")

    for r in rows:
        cost = _usd(r["cost_usd"])
        if r["unpriced"]:
            cost += f" (+{r['unpriced']}?)"
        table.add_row(
            r["model"], r["agent"], _num(r["messages"]),
            _num(r["input_tokens"]), _num(r["output_tokens"]),
            _pct(r["cache_hit_pct"]), _pct(r["reasoning_pct"]),
            _ms(r["avg_latency_ms"]), _num(r["retries"]), cost,
        )
    return table


def render_sessions(rows: list[Row]) -> Any:
    from rich.table import Table

    table = Table(title="recent sessions", title_justify="left")
    for col in ("started", "agent", "project", "branch"):
        table.add_column(col)
    for col in ("msgs", "tokens", "tools", "errors", "cost"):
        table.add_column(col, justify="right")

    for r in rows:
        cost = _usd(r["cost_usd"])
        if r["has_unknown_pricing"]:
            cost += " ⚠"
        table.add_row(
            (r["started_at"] or "")[:19], r["agent"],
            (r["project_slug"] or "—")[:32], r["git_branch"] or "—",
            _num(r["message_count"]), _num(r["total_tokens"]),
            _num(r["tool_call_count"]), _num(r["tool_error_count"]), cost,
        )
    return table


def render_tools(rows: list[Row]) -> Any:
    from rich.table import Table

    table = Table(title="tool reliability", title_justify="left")
    table.add_column("tool")
    table.add_column("agent")
    for col in ("calls", "errors", "error rate", "avg duration"):
        table.add_column(col, justify="right")

    for r in rows:
        table.add_row(
            r["tool_name"], r["agent"], _num(r["calls"]), _num(r["errors"]),
            _pct(r["error_pct"]), _ms(r["avg_duration_ms"]),
        )
    return table
