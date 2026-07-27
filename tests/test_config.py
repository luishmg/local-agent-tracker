"""
Config is the load-bearing piece for every other test: if a path is not
env-overridable, the test that needs to override it cannot be hermetic.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tracker.config import Settings, get_settings, reset_settings


def test_defaults_resolve_to_documented_source_paths(
    clean_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """architecture.md §3 names these three sources explicitly."""
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    reset_settings()
    s = Settings()

    home = Path.home()
    assert s.claude_projects_dir == home / ".claude" / "projects"
    assert s.pi_sessions_dir == home / ".pi" / "agent" / "sessions"
    assert s.pi_run_history_path == home / ".pi" / "agent" / "run-history.jsonl"
    assert s.database_path == (
        home / ".local" / "share" / "local-agent-tracker" / "tracker.db"
    )


def test_data_dir_follows_xdg_data_home(
    clean_env: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    reset_settings()
    s = Settings()
    assert s.data_dir == tmp_path / "xdg" / "local-agent-tracker"


def test_every_path_is_env_overridable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    overrides = {
        "TRACKER_CLAUDE_PROJECTS_DIR": tmp_path / "cc",
        "TRACKER_PI_SESSIONS_DIR": tmp_path / "pi",
        "TRACKER_PI_RUN_HISTORY_PATH": tmp_path / "rh.jsonl",
        "TRACKER_DATA_DIR": tmp_path / "data",
        "TRACKER_DB_PATH": tmp_path / "custom.db",
    }
    for key, value in overrides.items():
        monkeypatch.setenv(key, str(value))
    reset_settings()
    s = Settings()

    assert s.claude_projects_dir == overrides["TRACKER_CLAUDE_PROJECTS_DIR"]
    assert s.pi_sessions_dir == overrides["TRACKER_PI_SESSIONS_DIR"]
    assert s.pi_run_history_path == overrides["TRACKER_PI_RUN_HISTORY_PATH"]
    assert s.data_dir == overrides["TRACKER_DATA_DIR"]
    assert s.database_path == overrides["TRACKER_DB_PATH"]


def test_db_path_defaults_under_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """db_path is optional; when unset it must follow data_dir, not the XDG default."""
    monkeypatch.delenv("TRACKER_DB_PATH", raising=False)
    monkeypatch.setenv("TRACKER_DATA_DIR", str(tmp_path / "elsewhere"))
    reset_settings()
    assert Settings().database_path == tmp_path / "elsewhere" / "tracker.db"


def test_tilde_is_expanded(monkeypatch: pytest.MonkeyPatch) -> None:
    """`~` survives a round-trip through the environment; config must expand it
    rather than creating a literal './~' directory later."""
    monkeypatch.setenv("TRACKER_DATA_DIR", "~/some-tracker-dir")
    reset_settings()
    s = Settings()
    assert not str(s.data_dir).startswith("~")
    assert s.data_dir == Path.home() / "some-tracker-dir"


def test_get_settings_is_cached_and_resettable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    first = get_settings()
    assert get_settings() is first

    monkeypatch.setenv("TRACKER_DATA_DIR", str(tmp_path / "moved"))
    assert get_settings() is first, "cache should hide the change until reset"

    reset_settings()
    assert get_settings().data_dir == tmp_path / "moved"


def test_collector_budget_defaults_fit_the_five_minute_timer() -> None:
    """architecture.md §8: the timer fires every 5 minutes, so a run's own budget
    must leave headroom rather than risk overlapping the next fire."""
    s = get_settings()
    assert 0 < s.max_seconds < 300
    assert s.batch_size > 0
    assert s.max_skipped_lines_per_file > 0
