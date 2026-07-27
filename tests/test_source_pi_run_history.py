"""`run-history.jsonl`: durations and reliability, with the task prompt hashed."""

from __future__ import annotations

import json

from tests.factories import CANARY, pi_run_history_line
from tracker.sources.pi_run_history import parse_lines


def _raw(entries: list[dict]) -> list[str]:
    return [json.dumps(e) for e in entries]


def test_run_is_captured_with_duration_and_status() -> None:
    result = parse_lines(
        _raw([pi_run_history_line(agent="Explore", duration=4210, status="ok")]),
        source_file="/rh.jsonl",
    )
    run = result.subagent_runs[0]

    assert run.agent_type == "Explore"
    assert run.duration_ms == 4210
    assert run.status == "ok"
    assert run.source == "pi-run-history"


def test_task_text_never_survives() -> None:
    """architecture.md §8: the task prompt is conversation content."""
    result = parse_lines(
        _raw([pi_run_history_line(task=f"{CANARY} do the thing")]), source_file="/rh.jsonl"
    )
    run = result.subagent_runs[0]

    assert CANARY not in json.dumps(run.to_row("now"))
    assert run.task_sha256 is not None
    assert run.task_len == len(f"{CANARY} do the thing")


def test_ts_is_epoch_seconds_not_milliseconds() -> None:
    """Unlike every other timestamp in the project, this one is in seconds --
    reading it as ms would place every subagent run in 1970."""
    result = parse_lines(
        _raw([pi_run_history_line(ts=1782900000)]), source_file="/rh.jsonl"
    )
    assert result.subagent_runs[0].started_at is not None
    assert result.subagent_runs[0].started_at.startswith("2026-07-01T10:00:00")


def test_ended_at_is_derived_from_duration() -> None:
    result = parse_lines(
        _raw([pi_run_history_line(ts=1782900000, duration=5000)]), source_file="/rh.jsonl"
    )
    assert result.subagent_runs[0].ended_at is not None
    assert result.subagent_runs[0].ended_at.startswith("2026-07-01T10:00:05")


def test_error_runs_key_differently_from_successful_ones() -> None:
    """The file has no id, so status must participate in identity -- otherwise a
    retried run would overwrite the failure that prompted it."""
    result = parse_lines(
        _raw([
            pi_run_history_line(status="ok"),
            pi_run_history_line(status="error", exit_code=1),
        ]),
        source_file="/rh.jsonl",
    )
    assert len({r.dedup_key for r in result.subagent_runs}) == 2
    assert result.subagent_runs[1].exit_code == 1


def test_optional_exit_field_is_tolerated() -> None:
    result = parse_lines(_raw([pi_run_history_line()]), source_file="/rh.jsonl")
    assert result.subagent_runs[0].exit_code is None


def test_malformed_and_untimestamped_lines_are_skipped() -> None:
    entries = _raw([pi_run_history_line()])
    entries.append("{broken")
    entries.append(json.dumps({"agent": "X", "status": "ok"}))  # no ts

    result = parse_lines(entries, source_file="/rh.jsonl")
    assert len(result.subagent_runs) == 1
    assert result.lines_skipped == 2
