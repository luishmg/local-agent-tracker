"""
Shared fixtures.

The central concern is hermeticity: no test may read the real `~/.claude/projects`,
`~/.pi/agent/sessions`, or `~/.local/share/local-agent-tracker`. `isolated_env`
autouses its way into every test to guarantee that, by pointing every configurable
path at a tmp_path and clearing the Settings cache on both sides of the test.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from tracker.config import Settings, get_settings, reset_settings

_PATH_ENV_VARS = (
    "TRACKER_CLAUDE_PROJECTS_DIR",
    "TRACKER_PI_SESSIONS_DIR",
    "TRACKER_PI_RUN_HISTORY_PATH",
    "TRACKER_DATA_DIR",
    "TRACKER_DB_PATH",
)


@pytest.fixture(autouse=True)
def isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Point every source and store path into tmp_path. Autouse — opting out is
    not offered, because a test that reads the user's real transcripts is a bug."""
    claude = tmp_path / "claude" / "projects"
    pi_sessions = tmp_path / "pi" / "agent" / "sessions"
    pi_run_history = tmp_path / "pi" / "agent" / "run-history.jsonl"
    data = tmp_path / "data"
    for d in (claude, pi_sessions, data):
        d.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("TRACKER_CLAUDE_PROJECTS_DIR", str(claude))
    monkeypatch.setenv("TRACKER_PI_SESSIONS_DIR", str(pi_sessions))
    monkeypatch.setenv("TRACKER_PI_RUN_HISTORY_PATH", str(pi_run_history))
    monkeypatch.setenv("TRACKER_DATA_DIR", str(data))
    # A stray .env in the working directory would otherwise leak into Settings.
    monkeypatch.chdir(tmp_path)

    reset_settings()
    yield tmp_path
    reset_settings()


@pytest.fixture
def settings(isolated_env: Path) -> Settings:  # noqa: ARG001 — ordering dependency
    return get_settings()


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Strip every TRACKER_* path override, so defaults can be asserted."""
    for var in _PATH_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    reset_settings()
    yield
    reset_settings()
