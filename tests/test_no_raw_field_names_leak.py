"""
The module boundary guard.

`sources/claude_code.py` and `sources/pi.py` are the only files allowed to read
agent-native field names off a transcript. That boundary keeps the rest of the
codebase from quietly growing two dialects, and it erodes silently — one
convenient `entry["cacheRead"]` at a time — unless something checks.

Two things make this guard non-trivial to write honestly:

*Comments and docstrings must stay free.* Explaining that `cacheRead` maps to
`cache_read_tokens` is exactly the documentation this codebase wants, so the scan
runs over tokenized code with STRING and COMMENT tokens removed.

*Some names are shared.* `input_tokens` and `stop_reason` are Claude's field names
AND our own normalized column names, so they cannot be forbidden without banning
the schema from describing itself. Only names unambiguously belonging to one
agent's dialect are listed below.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import tracker
from tests._codescan import code_only as _code_only

#: Names that exist only in one agent's on-disk format. Deliberately excludes
#: `input_tokens`, `output_tokens` and `stop_reason`: those are also the
#: normalized names this project chose, so they are ours as much as Claude's.
FORBIDDEN = (
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
    "ephemeral_5m_input_tokens",
    "ephemeral_1h_input_tokens",
    "cache_creation",
    "stopReason",
    "cacheRead",
    "cacheWrite",
    "cacheWrite1h",
    "totalTokens",
    "toolCallId",
    "toolName",
    "isError",
    "attributionAgent",
    "isSidechain",
    "tokensBefore",
    "firstKeptEntryId",
    "modelId",
    "responseModel",
    "parentUuid",
    "sessionId",
    "gitBranch",
    "requestId",
    "agentId",
    "agentType",
    "spawnDepth",
    "toolUseId",
    "readFiles",
    "modifiedFiles",
)

#: The parsers own these names by design -- translating them is their whole job.
EXEMPT = {
    "sources/claude_code.py",
    "sources/pi.py",
    "sources/pi_run_history.py",
}

assert tracker.__file__ is not None, "tracker must be importable from disk to scan it"
PACKAGE_ROOT = Path(tracker.__file__).parent


def _python_files() -> list[Path]:
    return sorted(p for p in PACKAGE_ROOT.rglob("*.py") if "__pycache__" not in p.parts)


def test_the_guard_actually_scans_something() -> None:
    """A guard that silently matches zero files passes forever."""
    assert len(_python_files()) >= 8


def test_the_guard_can_actually_fail() -> None:
    """Proves the stripping did not defeat the check it exists to perform."""
    offending = "usage = entry['usage']\ntotal = usage.totalTokens\n"
    assert any(name in _code_only(offending) for name in FORBIDDEN)


def test_prose_about_the_mapping_is_not_flagged() -> None:
    """Documenting the translation must stay possible."""
    documented = '"""cacheRead maps to cache_read_tokens."""\n# cacheWrite too\nx = 1\n'
    assert not any(name in _code_only(documented) for name in FORBIDDEN)


@pytest.mark.parametrize("path", _python_files(), ids=lambda p: p.name)
def test_agent_field_names_stay_inside_the_parsers(path: Path) -> None:
    rel = path.relative_to(PACKAGE_ROOT).as_posix()
    if rel in EXEMPT:
        pytest.skip(f"{rel} owns agent-native names by design")

    code = _code_only(path.read_text(encoding="utf-8"))
    leaked = sorted({name for name in FORBIDDEN if name in code})
    assert not leaked, (
        f"{rel} uses agent-native field name(s) {leaked} in code. Translate them in "
        f"tracker/sources/ and speak the tracker.normalize.models vocabulary here."
    )


def test_schema_columns_are_agent_neutral() -> None:
    """The schema is the other place a dialect could leak in. A column named
    `input_tokens` is fine -- that name is ours -- but `cacheRead` would not be."""
    from tracker.db.schema import DDL

    leaks = [n for n in ("cacheRead", "cacheWrite", "totalTokens", "stopReason") if n in DDL]
    assert not leaks, f"schema carries pi's dialect: {leaks}"
