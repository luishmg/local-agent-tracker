"""
FUTURE SEAM -- the homelab Postgres shipper (architecture.md §4.3).

Deferred out of increment 1, but the schema already carries everything it needs,
so adding it is additive rather than a migration. This module exists to document
that contract while it is still fresh.

The contract
------------
Every shippable table (`tracker.db.schema.SHIPPABLE_TABLES`) already has:

- a `shipped_at TEXT NULL` column, indexed for `IS NULL` on `messages`, and
- a stable natural key -- `dedup_key` on `messages` / `tool_calls` /
  `subagent_runs`, `(agent, session_id)` on `sessions`, `name` on `experiments`.

So the implementation is three steps with no schema change:

    SELECT ... FROM <table> WHERE shipped_at IS NULL LIMIT <batch>
    INSERT INTO <table> ... ON CONFLICT (<natural key>) DO UPDATE SET ...
    UPDATE <table> SET shipped_at = <now> WHERE dedup_key IN (...)

Why the natural keys matter here specifically: the same dedup keys that make
re-ingest a no-op (ADR-0005) make a Postgres **replay** a no-op. Shipping the same
batch twice is harmless, which is what lets the shipper retry without bookkeeping.

Non-negotiables when this is built
----------------------------------
- **The laptop never blocks on the homelab.** An unreachable sink leaves rows with
  `shipped_at IS NULL` for the next run. It is a non-event, not an error, and must
  not fail the Collector Run that called it.
- **Credentials come from the systemd unit environment**, never from the repo or
  this package.
- **Ship metadata only.** The same §8 rule that governs the local store governs
  the wire: no message content ever leaves the machine.
- Grafana dashboards live on the homelab and are outside this app's scope.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class ShipResult:
    """Per-table counts from one shipping pass."""

    rows_shipped: int = 0
    rows_pending: int = 0
    reachable: bool = False
    error: str | None = None


def ship(conn: Any, dsn: str | None = None) -> ShipResult:  # pragma: no cover - seam
    """Not implemented in increment 1. See the module docstring for the contract."""
    raise NotImplementedError(
        "The Postgres shipper is deferred (architecture.md §4.3). The schema seam "
        "is in place: every shippable table has `shipped_at` and a stable natural "
        "key, so implementing this needs no migration."
    )


def pending_counts(conn: Any) -> dict[str, int]:
    """How many rows are waiting to ship, per table.

    Useful before the shipper exists: it shows the backlog the first run will
    face, and confirms the seam is being maintained as new rows arrive.
    """
    from tracker.db.schema import SHIPPABLE_TABLES

    return {
        table: conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE shipped_at IS NULL"
        ).fetchone()[0]
        for table in SHIPPABLE_TABLES
    }
