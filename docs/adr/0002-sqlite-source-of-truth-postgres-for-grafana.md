# SQLite is the source of truth; homelab Postgres is a shipped replica for Grafana

The laptop keeps the canonical store in SQLite (zero-daemon, survives Claude Code's ~30-day transcript cleanup, works offline), and each collector run idempotently upserts rows into a small homelab Postgres over Tailscale for the existing Grafana to query via its native Postgres datasource. Dashboards on the homelab are user-managed and outside this app.

## Considered Options

- Time-series backends (VictoriaMetrics, Prometheus remote_write/pushgateway): rejected — the core queries (model vs model, same model across harness versions) are relational slice-and-dice, and batch shipping needs timestamp backfill, which Prometheus-family ingestion handles poorly (pushgateway is last-value-only).
- Syncing the SQLite file itself + Grafana SQLite plugin: rejected — community-grade plugin plus file-locking hazards during sync.
- Homelab-only storage: rejected — history would be hostage to homelab availability.

## Consequences

- The homelab being unreachable is a non-event: unshipped rows wait in SQLite for the next run.
- One small Postgres instance must be provisioned on the homelab.
