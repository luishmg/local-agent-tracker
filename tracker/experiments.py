"""
Experiment windows (ADR-0003).

This ships in increment 1 even though harness fingerprints are deferred, and the
asymmetry is the whole reason: fingerprints are **retroactive** -- the collector
can reconstruct them at any point from the config repos' git history -- whereas an
Experiment records *why* a change was made, which no commit hash carries. Every
day without `experiment start` is a day of intent permanently unrecoverable.

Windows may overlap and may be left open; both are informative rather than errors.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any


def _utcnow_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


class ExperimentError(RuntimeError):
    """A user-facing problem, reported as a message rather than a traceback."""


def start(conn: sqlite3.Connection, name: str, *, note: str | None = None) -> dict[str, Any]:
    """Open a named window. Fails if one by that name is already open."""
    if not name.strip():
        raise ExperimentError("an experiment needs a name")

    existing = conn.execute(
        "SELECT started_at, ended_at FROM experiments WHERE name = ?", (name,)
    ).fetchone()
    if existing is not None and existing["ended_at"] is None:
        raise ExperimentError(f"experiment {name!r} is already open (since {existing['started_at']})")
    if existing is not None:
        raise ExperimentError(
            f"experiment {name!r} already ran ({existing['started_at']} -> {existing['ended_at']}); "
            f"pick a new name rather than reusing one -- reuse would merge two "
            f"different harness states under one label"
        )

    now = _utcnow_iso()
    conn.execute(
        "INSERT INTO experiments (name, started_at, note, created_at) VALUES (?, ?, ?, ?)",
        (name, now, note, now),
    )
    return {"name": name, "started_at": now, "note": note}


def stop(conn: sqlite3.Connection, name: str | None = None) -> dict[str, Any]:
    """Close a window. With no name, closes the single open one."""
    if name is None:
        open_rows = conn.execute(
            "SELECT name FROM experiments WHERE ended_at IS NULL ORDER BY started_at"
        ).fetchall()
        if not open_rows:
            raise ExperimentError("no experiment is open")
        if len(open_rows) > 1:
            names = ", ".join(r["name"] for r in open_rows)
            raise ExperimentError(f"several experiments are open ({names}); name the one to stop")
        name = open_rows[0]["name"]

    row = conn.execute(
        "SELECT started_at, ended_at FROM experiments WHERE name = ?", (name,)
    ).fetchone()
    if row is None:
        raise ExperimentError(f"no experiment named {name!r}")
    if row["ended_at"] is not None:
        raise ExperimentError(f"experiment {name!r} already ended at {row['ended_at']}")

    now = _utcnow_iso()
    conn.execute("UPDATE experiments SET ended_at = ? WHERE name = ?", (now, name))
    return {"name": name, "started_at": row["started_at"], "ended_at": now}


def list_all(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Every window with the spend that falls inside it.

    An open window is bounded by 'now', so its totals grow between calls.
    """
    return [
        dict(r)
        for r in conn.execute(
            """
            SELECT e.name,
                   e.started_at,
                   e.ended_at,
                   e.note,
                   (SELECT COUNT(*) FROM messages m
                     WHERE m.ts >= e.started_at
                       AND (e.ended_at IS NULL OR m.ts <= e.ended_at)) AS messages,
                   (SELECT SUM(cost_usd) FROM messages m
                     WHERE m.ts >= e.started_at
                       AND (e.ended_at IS NULL OR m.ts <= e.ended_at)) AS cost_usd
              FROM experiments e
             ORDER BY e.started_at DESC
            """
        )
    ]


def report(conn: sqlite3.Connection, name: str) -> list[dict[str, Any]]:
    """Per-model spend inside one window -- the `GROUP BY model, experiment` of
    architecture.md §6, which is the comparison the system exists for."""
    row = conn.execute(
        "SELECT started_at, ended_at FROM experiments WHERE name = ?", (name,)
    ).fetchone()
    if row is None:
        raise ExperimentError(f"no experiment named {name!r}")

    return [
        dict(r)
        for r in conn.execute(
            """
            SELECT COALESCE(model, '(none)') AS model,
                   agent,
                   COUNT(*) AS messages,
                   SUM(input_tokens) AS input_tokens,
                   SUM(output_tokens) AS output_tokens,
                   SUM(total_tokens) AS total_tokens,
                   AVG(latency_ms) AS avg_latency_ms,
                   SUM(cost_usd) AS cost_usd,
                   SUM(CASE WHEN cost_usd IS NULL THEN 1 ELSE 0 END) AS unpriced
              FROM messages
             WHERE ts >= :start AND (:end IS NULL OR ts <= :end)
             GROUP BY model, agent
             ORDER BY cost_usd DESC NULLS LAST
            """,
            {"start": row["started_at"], "end": row["ended_at"]},
        )
    ]
