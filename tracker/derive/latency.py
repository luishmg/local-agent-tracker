"""
Turn latency, derived by diffing consecutive Message timestamps.

Neither agent records per-message duration (architecture.md §3.1), so latency has
to be inferred. It is computed **in SQL after ingest** rather than in the parsers,
for one reason: ingestion is incremental. A parser only ever sees the lines in the
current read, so the first Message of a run would have no predecessor to diff
against and would be permanently NULL. A `LAG()` window over the stored table sees
the whole session regardless of which run each row arrived in.

Latency is only meaningful *within* one session thread, so the window partitions
by `(agent, session_id, agent_run_id)` -- a subagent's messages interleave with its
parent's in wall-clock time, and diffing across them would produce noise.
"""

from __future__ import annotations

import sqlite3

#: Diffs above this are a user walking away, not a model thinking. Storing them
#: would wreck every latency average.
MAX_PLAUSIBLE_LATENCY_MS = 30 * 60 * 1000


def backfill_latency(conn: sqlite3.Connection, *, only_null: bool = True) -> int:
    """Populate `messages.latency_ms`. Returns the number of rows updated.

    Idempotent: recomputing yields the same values, so it is safe to run on every
    Collector Run. `only_null=False` recomputes everything, which is what you want
    after a backfill inserts rows *before* already-processed ones.
    """
    where_clause = "WHERE latency_ms IS NULL" if only_null else ""

    cur = conn.execute(
        f"""
        WITH ordered AS (
            SELECT
                dedup_key,
                ts_epoch_ms - LAG(ts_epoch_ms) OVER (
                    PARTITION BY agent, session_id, COALESCE(agent_run_id, '')
                    ORDER BY ts_epoch_ms, dedup_key
                ) AS delta
              FROM messages
        )
        UPDATE messages
           SET latency_ms = (
                SELECT delta FROM ordered
                 WHERE ordered.dedup_key = messages.dedup_key
                   AND ordered.delta IS NOT NULL
                   AND ordered.delta >= 0
                   AND ordered.delta <= {MAX_PLAUSIBLE_LATENCY_MS}
           )
        {where_clause}
        """
    )
    return max(cur.rowcount, 0)
