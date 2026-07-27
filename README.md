# local-agent-tracker

A local, zero-daemon pipeline that tracks the **performance and cost** of two AI coding agents — **Claude Code** and **pi** — so their costs can be compared across LLMs and across harness changes, and optimized deliberately instead of guessed at.

It answers one question: *"What does a unit of work cost on model A vs model B — and did my last harness change make the same model cheaper or more expensive?"*

- **Design**: [architecture.md](./architecture.md) · **Terminology**: [CONTEXT.md](./CONTEXT.md) · **Decisions**: [docs/adr/](./docs/adr/)

---

## How it works

Both agents already write complete token and cost data to local JSONL. A systemd
timer runs a short Python process every five minutes that reads only the bytes
added since last time, normalizes them, and stores them in SQLite.

```
~/.claude/projects/**/<session>.jsonl ──┐
~/.pi/agent/sessions/**/<session>.jsonl ┼─▶ tracker collect (timer, 5 min, runs and exits)
~/.pi/agent/run-history.jsonl ──────────┘        │
                                                 ▼
                                 SQLite  ~/.local/share/local-agent-tracker/tracker.db
                                                 │
                                       tracker report / status / doctor
```

Nothing runs between timer fires: steady-state memory is zero. A collect run that
finds nothing new takes **~80 ms**; the initial backfill over 513 MB of
transcripts takes **~12 s at 125 MB peak RSS**.

**Message content is never stored.** Only usage and metadata are extracted — see
`architecture.md` §8, enforced by a test that sweeps every text column for a canary.

---

## Quick start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest -q

tracker db init
tracker collect --max-seconds 0 --full-rebuild   # first backfill: reads everything
tracker report --models --since 30d
```

Then install the timer:

```bash
mkdir -p ~/.config/systemd/user
cp systemd/agent-tracker.{service,timer} ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now agent-tracker.timer
systemctl --user list-timers agent-tracker.timer
```

Run the backfill manually **before** enabling the timer — the default 240-second
budget is sized for incremental runs, not a cold start.

---

## Commands

| Command | What it does |
|---|---|
| `tracker collect` | One Collector Run: ingest → derive → store. `--max-seconds 0` for an unbounded backfill, `--full-rebuild` to recompute every rollup. |
| `tracker report --daily \| --models \| --sessions \| --tools` | Terminal tables. `--since 30d`, `--json` for machine output. |
| `tracker status` | Store size, row counts, coverage window, last run. |
| `tracker doctor` | Runs the store's invariants; exits non-zero on violation. |
| `tracker experiment start \| stop \| list \| report` | Named windows marking an intentional harness change. |
| `tracker pricing show \| update` | Inspect or refresh the vendored rate table. |
| `tracker config` | Show resolved paths and the collector budget. |

Every path is overridable via a `TRACKER_`-prefixed environment variable
(`TRACKER_DATA_DIR`, `TRACKER_CLAUDE_PROJECTS_DIR`, …).

---

## Three things that are easy to get wrong

These cost real accuracy and are documented where they bite:

**Claude Code writes one API response as several JSONL lines** — one per content
block — each repeating the *complete* `usage`. Measured here: 49,988 assistant
lines resolve to 20,151 responses. Summing per line inflates cost ~2.5×. The
identity is `message.id`, not the per-line `uuid`. See
[ADR-0005](./docs/adr/0005-global-message-dedup-key.md).

**Both agents copy history into a new transcript when a session is resumed**, so
the dedup key is globally scoped rather than per-session. Session-scoping
double-bills every resumed session; 0.41% of pi spend here is fork-duplicated.

**pi's `usage.reasoning` is a subset of `output`**, not additional tokens. It is
stored as a diagnostic and never enters a cost formula.

---

## Verifying the numbers

Two checks share no code with the implementation, which is what makes them worth
running:

```bash
# Claude: stored rows should be ~40% of raw assistant lines. Equal means the
# content-block fan-out collapse has regressed.
find ~/.claude/projects -name '*.jsonl' -exec grep -c '"type":"assistant"' {} + \
  | awk -F: '{s+=$2} END {print "raw:", s}'
sqlite3 ~/.local/share/local-agent-tracker/tracker.db \
  "SELECT 'stored:', COUNT(*) FROM messages WHERE agent='claude-code'"

# pi: its reported cost is authoritative, so compare against the raw files.
# Stored should be slightly BELOW raw — the difference is fork-duplicate spend.
find ~/.pi/agent/sessions -mindepth 2 -maxdepth 2 -name '*.jsonl' -print0 \
  | xargs -0 cat | jq -s '[.[] | .message.usage.cost.total // empty] | add'
```

`tracker doctor` runs the structural invariants (no double-billed response,
unknown pricing is NULL never 0, rollups match the message grain, …).

---

## Scope

**Shipping now**: collector, SQLite store, CLI reports, experiments, pricing.

**Deferred** (seams are in place, no migration needed):

- **Postgres shipper** for Grafana — every shippable table already carries
  `shipped_at` and a stable natural key. See `tracker/ship.py`.
- **Static HTML dashboard** — `tracker/report.py` already separates queries
  (returning plain dicts) from rendering.
- **Harness fingerprints** — deferred because they are *retroactive*: they can be
  reconstructed at any time from the config repos' git history. Experiments ship
  now precisely because intent is **not** recoverable after the fact (ADR-0003).

---

## Development

```bash
pytest -q                              # offline; no fixtures on disk
pytest tests/test_pipeline_idempotency.py -q
```

Test fixtures are generated in-process by `tests/factories.py` and never copied
from real transcripts — a committed transcript would violate the same content
rule the collector enforces. `tests/test_fixtures_are_synthetic.py` checks that.
