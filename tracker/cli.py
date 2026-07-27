"""
The single `tracker <command>` entry point (architecture.md §4).

Subcommands are registered here and dispatch into the modules that own the work.
Everything returns a process exit code: 0 success, 1 a handled failure, 2 misuse.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from tracker import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tracker",
        description="Track cost and performance of local AI coding agents.",
    )
    parser.add_argument("--version", action="version", version=f"tracker {__version__}")
    subparsers = parser.add_subparsers(dest="command", metavar="<command>")

    config_p = subparsers.add_parser(
        "config", help="show resolved paths and collector budget"
    )
    config_p.set_defaults(func=_cmd_config)

    db_p = subparsers.add_parser("db", help="database maintenance")
    db_sub = db_p.add_subparsers(dest="db_command", metavar="<subcommand>")
    db_init_p = db_sub.add_parser("init", help="create the SQLite store if absent")
    db_init_p.set_defaults(func=_cmd_db_init)
    db_p.set_defaults(func=_require_subcommand(db_p))

    collect_p = subparsers.add_parser(
        "collect", help="ingest new transcript data (one Collector Run)"
    )
    collect_p.add_argument(
        "--max-seconds", type=int, default=None,
        help="time budget; 0 = unlimited (use for the first backfill)",
    )
    collect_p.add_argument("--max-files", type=int, default=None, help="file budget; 0 = unlimited")
    collect_p.add_argument("--progress", action="store_true", help="show a progress line per file")
    collect_p.add_argument("--quiet", action="store_true", help="suppress the run summary")
    collect_p.add_argument(
        "--full-rebuild", action="store_true",
        help="recompute every session rollup and latency, not just recent ones",
    )
    collect_p.set_defaults(func=_cmd_collect)

    report_p = subparsers.add_parser("report", help="terminal reports from the store")
    report_p.add_argument("--daily", action="store_true", help="cost per day")
    report_p.add_argument("--models", action="store_true", help="cost per model")
    report_p.add_argument("--sessions", action="store_true", help="recent sessions")
    report_p.add_argument("--tools", action="store_true", help="tool reliability")
    report_p.add_argument("--since", default=None, help="e.g. 30d, 12h, or 2026-07-01")
    report_p.add_argument("--limit", type=int, default=20, help="row cap for --sessions")
    report_p.add_argument("--json", action="store_true", help="emit JSON instead of tables")
    report_p.set_defaults(func=_cmd_report)

    status_p = subparsers.add_parser("status", help="store size, coverage and last run")
    status_p.set_defaults(func=_cmd_status)

    doctor_p = subparsers.add_parser("doctor", help="check the store's invariants")
    doctor_p.set_defaults(func=_cmd_doctor)

    exp_p = subparsers.add_parser(
        "experiment", help="named windows marking an intentional harness change"
    )
    exp_sub = exp_p.add_subparsers(dest="experiment_command", metavar="<subcommand>")
    exp_start = exp_sub.add_parser("start", help="open a window")
    exp_start.add_argument("name")
    exp_start.add_argument("--note", default=None, help="why this change is being tried")
    exp_start.set_defaults(func=_cmd_experiment_start)
    exp_stop = exp_sub.add_parser("stop", help="close a window")
    exp_stop.add_argument("name", nargs="?", default=None)
    exp_stop.set_defaults(func=_cmd_experiment_stop)
    exp_list = exp_sub.add_parser("list", help="all windows with their spend")
    exp_list.set_defaults(func=_cmd_experiment_list)
    exp_report = exp_sub.add_parser("report", help="per-model spend inside one window")
    exp_report.add_argument("name")
    exp_report.set_defaults(func=_cmd_experiment_report)
    exp_p.set_defaults(func=_require_subcommand(exp_p))

    pricing_p = subparsers.add_parser("pricing", help="the vendored rate table")
    pricing_sub = pricing_p.add_subparsers(dest="pricing_command", metavar="<subcommand>")
    pricing_show = pricing_sub.add_parser("show", help="show active rates")
    pricing_show.add_argument("--model", default=None, help="filter to one model")
    pricing_show.set_defaults(func=_cmd_pricing_show)
    pricing_update = pricing_sub.add_parser(
        "update", help="refresh from LiteLLM; prints a diff and stops (ADR-0004)"
    )
    pricing_update.add_argument(
        "--dry-run", action="store_true", help="show what would change, write nothing"
    )
    pricing_update.set_defaults(func=_cmd_pricing_update)
    pricing_p.set_defaults(func=_require_subcommand(pricing_p))

    return parser


def _require_subcommand(parser: argparse.ArgumentParser):
    def _fail(_args: argparse.Namespace) -> int:
        parser.print_help()
        return 2

    return _fail


def _cmd_db_init(_args: argparse.Namespace) -> int:
    from tracker.config import get_settings
    from tracker.db.store import get_meta, open_db, table_names

    settings = get_settings()
    path = settings.database_path
    existed = path.exists()

    with open_db(path, cache_kib=settings.sqlite_cache_kib) as conn:
        tables = table_names(conn)
        version = get_meta(conn, "schema_version")

    verb = "verified" if existed else "created"
    print(f"{verb} {path}")
    print(f"  schema_version={version}  tables={len(tables)}")
    return 0


def _cmd_config(_args: argparse.Namespace) -> int:
    from rich.console import Console
    from rich.table import Table

    from tracker.config import get_settings

    settings = get_settings()
    table = Table(title="tracker configuration", title_justify="left")
    table.add_column("setting", style="bold")
    table.add_column("value")

    table.add_row("claude_projects_dir", str(settings.claude_projects_dir))
    table.add_row("pi_sessions_dir", str(settings.pi_sessions_dir))
    table.add_row("pi_run_history_path", str(settings.pi_run_history_path))
    table.add_row("data_dir", str(settings.data_dir))
    table.add_row("database_path", str(settings.database_path))
    table.add_row("max_seconds", str(settings.max_seconds))
    table.add_row("max_files", str(settings.max_files))
    table.add_row("batch_size", str(settings.batch_size))

    Console().print(table)
    return 0


def _parse_since(value: str | None) -> int | None:
    """Turn `30d` / `12h` / `2026-07-01` into epoch milliseconds."""
    if not value:
        return None

    from datetime import datetime, timedelta, timezone

    units = {"d": "days", "h": "hours", "m": "minutes", "w": "weeks"}
    suffix = value[-1].lower()
    if suffix in units and value[:-1].isdigit():
        delta = timedelta(**{units[suffix]: int(value[:-1])})
        return int((datetime.now(tz=timezone.utc) - delta).timestamp() * 1000)

    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise SystemExit(f"cannot parse --since {value!r}; use 30d, 12h, or 2026-07-01")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp() * 1000)


def _cmd_collect(args: argparse.Namespace) -> int:
    from datetime import datetime, timedelta, timezone

    from tracker.config import get_settings
    from tracker.db.store import open_db, transaction
    from tracker.derive.latency import backfill_latency
    from tracker.derive.rollups import rebuild_sessions
    from tracker.ingest.pipeline import collect
    from tracker.pricing import get_pricing_table

    settings = get_settings()
    pricing = get_pricing_table()

    def _progress(source, n: int) -> None:
        print(f"  [{n}] {source.path}", flush=True)

    with open_db(settings.database_path, cache_kib=settings.sqlite_cache_kib) as conn:
        stats = collect(
            conn,
            settings=settings,
            pricing=pricing,
            max_seconds=args.max_seconds,
            max_files=args.max_files,
            progress=_progress if args.progress else None,
        )

        # Derivations run after ingest: latency needs neighbouring rows, and a
        # rollup must see every message the run added.
        since_ms = None
        if not args.full_rebuild:
            cutoff = datetime.now(tz=timezone.utc) - timedelta(days=2)
            since_ms = int(cutoff.timestamp() * 1000)
        with transaction(conn):
            backfill_latency(conn, only_null=not args.full_rebuild)
            sessions_written = rebuild_sessions(
                conn,
                now=datetime.now(tz=timezone.utc).isoformat(),
                since_epoch_ms=since_ms,
            )

    if not args.quiet:
        print(
            f"run={stats.run_id} scanned={stats.files_scanned} read={stats.files_read} "
            f"skipped={stats.files_skipped} rotated={stats.files_rotated} "
            f"lines={stats.lines_read} bad_lines={stats.lines_skipped} "
            f"messages+={stats.messages_upserted} tools+={stats.tool_calls_upserted} "
            f"subagents+={stats.subagent_runs_upserted} sessions={sessions_written} "
            f"{stats.duration_ms}ms" + (" PARTIAL" if stats.partial else "")
        )
        if stats.unknown_models:
            # Once per run, never per row.
            print(f"  unpriced models: {', '.join(sorted(stats.unknown_models))}")
    return 0


def _cmd_report(args: argparse.Namespace) -> int:
    import json as _json

    from rich.console import Console

    from tracker.config import get_settings
    from tracker.db.store import open_db
    from tracker import report as rpt

    settings = get_settings()
    since = _parse_since(args.since)
    wanted = {
        "daily": args.daily, "models": args.models,
        "sessions": args.sessions, "tools": args.tools,
    }
    if not any(wanted.values()):
        wanted["daily"] = wanted["models"] = True

    with open_db(settings.database_path, create=False) as conn:
        payload: dict[str, list[dict]] = {}
        if wanted["daily"]:
            payload["daily"] = rpt.q_daily(conn, since_epoch_ms=since)
        if wanted["models"]:
            payload["models"] = rpt.q_models(conn, since_epoch_ms=since)
        if wanted["sessions"]:
            payload["sessions"] = rpt.q_sessions(conn, since_epoch_ms=since, limit=args.limit)
        if wanted["tools"]:
            payload["tools"] = rpt.q_tools(conn, since_epoch_ms=since)
        unknown = rpt.q_unknown_models(conn)

    if args.json:
        print(_json.dumps(payload, indent=2, default=str))
        return 0

    console = Console()
    renderers = {
        "daily": rpt.render_daily, "models": rpt.render_models,
        "sessions": rpt.render_sessions, "tools": rpt.render_tools,
    }
    for key, rows in payload.items():
        console.print(renderers[key](rows))
        console.print()

    if unknown:
        total = sum(r["messages"] for r in unknown)
        console.print(
            f"[yellow]{total} message(s) have no price[/yellow] — totals above are "
            f"incomplete, not low. Models: "
            f"{', '.join(r['model'] for r in unknown[:5])}"
        )
    return 0


def _cmd_status(_args: argparse.Namespace) -> int:
    from rich.console import Console
    from rich.table import Table

    from tracker.config import get_settings
    from tracker.db.store import open_db
    from tracker.ingest.watermarks import watermark_summary
    from tracker.report import q_status

    settings = get_settings()
    with open_db(settings.database_path, create=False) as conn:
        status = q_status(conn)
        marks = watermark_summary(conn)

    db_bytes = settings.database_path.stat().st_size if settings.database_path.exists() else 0

    table = Table(title="tracker status", title_justify="left")
    table.add_column("metric", style="bold")
    table.add_column("value", justify="right")
    table.add_row("database", str(settings.database_path))
    table.add_row("db size", f"{db_bytes / 1_048_576:.1f} MiB")
    for key in ("messages", "sessions", "tool_calls", "subagent_runs", "experiments"):
        table.add_row(key, f"{status[key]:,}")
    table.add_row("files watermarked", f"{marks['files']:,}")
    table.add_row("bytes ingested", f"{marks['bytes'] / 1_048_576:.1f} MiB")
    table.add_row("unparseable lines", f"{marks['skipped']:,}")
    cost = status["total_cost_usd"]
    table.add_row("total cost", "—" if cost is None else f"${cost:,.2f}")
    table.add_row("unpriced messages", f"{status['unpriced_messages']:,}")
    table.add_row("earliest message", str(status["earliest"] or "—"))
    table.add_row("latest message", str(status["latest"] or "—"))
    table.add_row("last collect", str(status["last_run"] or "never"))

    Console().print(table)
    return 0


def _cmd_doctor(_args: argparse.Namespace) -> int:
    """Exit 1 when any invariant is violated, so it works as a health check."""
    from rich.console import Console

    from tracker.config import get_settings
    from tracker.db.store import open_db
    from tracker.report import run_doctor

    settings = get_settings()
    with open_db(settings.database_path, create=False) as conn:
        results = run_doctor(conn)

    console = Console()
    failed = 0
    for r in results:
        if r["passed"]:
            console.print(f"[green]PASS[/green]  {r['check']}")
        else:
            failed += 1
            console.print(f"[red]FAIL[/red]  {r['check']} — {r['violations']} violation(s)")
            console.print(f"        {r['why']}")
    console.print(f"\n{len(results) - failed}/{len(results)} checks passed")
    return 1 if failed else 0


def _open_existing():
    from tracker.config import get_settings
    from tracker.db.store import open_db

    settings = get_settings()
    return open_db(settings.database_path, create=False)


def _cmd_experiment_start(args: argparse.Namespace) -> int:
    from tracker.experiments import ExperimentError, start

    try:
        with _open_existing() as conn:
            result = start(conn, args.name, note=args.note)
            conn.commit()
    except ExperimentError as exc:
        print(f"error: {exc}")
        return 1
    print(f"started experiment {result['name']!r} at {result['started_at']}")
    return 0


def _cmd_experiment_stop(args: argparse.Namespace) -> int:
    from tracker.experiments import ExperimentError, stop

    try:
        with _open_existing() as conn:
            result = stop(conn, args.name)
            conn.commit()
    except ExperimentError as exc:
        print(f"error: {exc}")
        return 1
    print(f"stopped experiment {result['name']!r} ({result['started_at']} -> {result['ended_at']})")
    return 0


def _cmd_experiment_list(_args: argparse.Namespace) -> int:
    from rich.console import Console
    from rich.table import Table

    from tracker.experiments import list_all

    with _open_existing() as conn:
        rows = list_all(conn)

    if not rows:
        print("no experiments recorded")
        print(
            "  Experiment intent is not recoverable after the fact (ADR-0003) — "
            "`tracker experiment start \"<name>\"` before your next harness change."
        )
        return 0

    table = Table(title="experiments", title_justify="left")
    for col in ("name", "started", "ended", "note"):
        table.add_column(col)
    for col in ("messages", "cost"):
        table.add_column(col, justify="right")

    for r in rows:
        cost = "—" if r["cost_usd"] is None else f"${r['cost_usd']:,.2f}"
        table.add_row(
            r["name"], (r["started_at"] or "")[:19], (r["ended_at"] or "open")[:19],
            r["note"] or "—", f"{r['messages']:,}", cost,
        )
    Console().print(table)
    return 0


def _cmd_experiment_report(args: argparse.Namespace) -> int:
    from rich.console import Console
    from rich.table import Table

    from tracker.experiments import ExperimentError, report

    try:
        with _open_existing() as conn:
            rows = report(conn, args.name)
    except ExperimentError as exc:
        print(f"error: {exc}")
        return 1

    table = Table(title=f"experiment: {args.name}", title_justify="left")
    table.add_column("model")
    table.add_column("agent")
    for col in ("msgs", "in", "out", "avg lat", "cost"):
        table.add_column(col, justify="right")
    for r in rows:
        cost = "—" if r["cost_usd"] is None else f"${r['cost_usd']:,.4f}"
        lat = "—" if r["avg_latency_ms"] is None else f"{r['avg_latency_ms'] / 1000:.1f}s"
        table.add_row(
            r["model"], r["agent"], f"{r['messages']:,}",
            f"{r['input_tokens'] or 0:,}", f"{r['output_tokens'] or 0:,}", lat, cost,
        )
    Console().print(table)
    return 0


def _cmd_pricing_show(args: argparse.Namespace) -> int:
    from rich.console import Console
    from rich.table import Table

    from tracker.pricing import ModelRates, get_pricing_table

    table_data = get_pricing_table()
    if args.model:
        resolved = table_data.resolve(args.model)
        if resolved == "zero-rated":
            print(f"{args.model}: deliberately zero-rated (local, unbilled)")
            return 0
        if resolved is None:
            print(f"{args.model}: NOT PRICED — would cost NULL, never 0")
            return 1
        entries = {args.model: resolved}
    else:
        entries = table_data.models()

    out = Table(
        title=f"pricing v{table_data.version} (USD per million tokens)", title_justify="left"
    )
    out.add_column("model")
    for col in ("input", "output", "cache read", "cache wr 5m", "cache wr 1h"):
        out.add_column(col, justify="right")

    def per_mtok(v: float | None) -> str:
        return "—" if v is None else f"${v * 1_000_000:,.2f}"

    for name, rates in entries.items():
        assert isinstance(rates, ModelRates)
        out.add_row(
            name,
            per_mtok(rates.input_usd_per_token),
            per_mtok(rates.output_usd_per_token),
            per_mtok(rates.cache_read_usd_per_token),
            per_mtok(rates.cache_write_5m_usd_per_token),
            per_mtok(rates.cache_write_1h_usd_per_token)
            + ("*" if rates.cache_write_1h_source == "derived-2x-input" else ""),
        )
    console = Console()
    console.print(out)
    console.print("[dim]* 1h rate derived as 2x input (not published upstream)[/dim]")
    return 0


def _cmd_pricing_update(args: argparse.Namespace) -> int:
    from tracker.pricing.litellm import update

    if args.dry_run:
        print("--dry-run is not implemented; `update` already stops before committing")
        return 2

    try:
        result = update()
    except Exception as exc:  # network, upstream shape change, disk
        print(f"error: pricing update failed: {type(exc).__name__}: {exc}")
        print("  The vendored table is unchanged; collection stays deterministic.")
        return 1

    if not result.changed:
        print(result.message)
        return 0

    print(result.message)
    if result.missing_rate_models:
        print(
            f"  {len(result.missing_rate_models)} model(s) have no input rate upstream "
            f"and will price as unknown: {', '.join(result.missing_rate_models[:5])}"
        )
    print()
    print(result.diff or "(no textual diff — this is the first version)")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not hasattr(args, "func"):
        parser.print_help()
        return 2
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
