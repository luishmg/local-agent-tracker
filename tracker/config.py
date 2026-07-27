"""
Centralized configuration: where the source transcripts live, where the store goes,
and the collector's resource budget.

Unlike mcp-plane's config, nothing here is a required secret — every value has a
working default derived from the user's home directory. What matters instead is
that *every path is env-overridable*, because that is what lets the test suite point
the whole pipeline at a tmp_path tree and stay hermetic.

For the same reason there is no module-level `settings = get_settings()` singleton:
that would freeze the paths at import time, before a test could set the environment.
Call `get_settings()` at use time; call `reset_settings()` after mutating the env.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _default_data_dir() -> Path:
    """`$XDG_DATA_HOME/local-agent-tracker`, falling back to `~/.local/share`."""
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg) if xdg else Path.home() / ".local" / "share"
    return base / "local-agent-tracker"


class Settings(BaseSettings):
    """Runtime configuration. Every field is overridable via a `TRACKER_`-prefixed
    environment variable (e.g. `TRACKER_DATA_DIR=/tmp/x`)."""

    model_config = SettingsConfigDict(
        env_prefix="TRACKER_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Sources (read-only; the collector never writes into these) ---
    claude_projects_dir: Path = Field(
        default_factory=lambda: Path.home() / ".claude" / "projects"
    )
    pi_sessions_dir: Path = Field(
        default_factory=lambda: Path.home() / ".pi" / "agent" / "sessions"
    )
    pi_run_history_path: Path = Field(
        default_factory=lambda: Path.home() / ".pi" / "agent" / "run-history.jsonl"
    )

    # --- Store ---
    data_dir: Path = Field(default_factory=_default_data_dir)
    db_path: Path | None = None  # defaults to `data_dir / "tracker.db"`

    # --- Collector budget (see architecture.md §8: the 5-min timer must never
    #     overlap itself, so a run that would exceed its slot commits and exits) ---
    max_seconds: int = 240  # 0 = unlimited; use for the first backfill
    max_files: int = 0  # 0 = unlimited
    batch_size: int = 2000  # records per intra-file flush
    read_buffer_bytes: int = 1 << 20  # 1 MiB
    sqlite_cache_kib: int = 20_000  # PRAGMA cache_size = -20000

    # --- Tolerance (architecture.md §8: a bad line is never fatal) ---
    max_skipped_lines_per_file: int = 1000

    @field_validator(
        "claude_projects_dir",
        "pi_sessions_dir",
        "pi_run_history_path",
        "data_dir",
        "db_path",
        mode="after",
    )
    @classmethod
    def _expand(cls, v: Path | None) -> Path | None:
        # `~` survives a round-trip through the environment, so expand it here
        # rather than trusting callers to have done it.
        return v.expanduser() if v is not None else None

    @property
    def database_path(self) -> Path:
        """The canonical SQLite store (architecture.md §4.2)."""
        return self.db_path if self.db_path is not None else self.data_dir / "tracker.db"

    @property
    def dashboard_path(self) -> Path:
        """Where `tracker render` will write. Unused in increment 1."""
        return self.data_dir / "dashboard.html"


@lru_cache
def get_settings() -> Settings:
    return Settings()


def reset_settings() -> None:
    """Drop the cached Settings. Tests call this after mutating the environment."""
    get_settings.cache_clear()
