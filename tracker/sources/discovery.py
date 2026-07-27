"""
Turning the filesystem into a list of source files.

Discovery is an **explicit allowlist of path shapes**, not a bare
`rglob("*.jsonl")`. That is a correctness requirement, not tidiness.

`~/.pi/agent/sessions/` is not homogeneous. Its 273 `.jsonl` files are three
different things:

===========================================  =====  ==================================
path shape                                   count  what it is
===========================================  =====  ==================================
`<slug>/<iso-ts>_<uuid>.jsonl`                 146  real pi session transcripts
`<slug>/claude-code-artifacts/*.jsonl`         120  Claude Code SDK stream-json output
`<slug>/<session>/<toolcall-id>/run-0/…`         7  pi nested subagent sessions
===========================================  =====  ==================================

The middle group is the trap. Those files use Claude's `snake_case` shape, so the
pi parser reads zero tokens off them. And 82 of their 85 distinct session ids also
exist under `~/.claude/projects/`, making them duplicate copies of spend that is
already counted. They are excluded outright.

The last group is the opposite trap: five levels deep, so a `<slug>/*.jsonl` glob
misses them entirely — and they are real, uncounted subagent spend.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

SourceKind = Literal[
    "claude_session",
    "claude_subagent",
    "pi_session",
    "pi_subagent_session",
    "pi_run_history",
]

#: Files under this directory are Claude Code SDK output, not pi transcripts.
PI_EXCLUDED_DIRS = frozenset({"claude-code-artifacts"})

#: `2026-07-01T10-00-00-000Z_019f0000-...jsonl`
_PI_SESSION_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T[\d-]+Z_[0-9a-f-]+\.jsonl$", re.IGNORECASE)

#: `agent-<id>.jsonl` inside a `subagents/` directory. Deliberately permissive
#: about the id: observed ids are hex, but being strict here would silently drop
#: subagent spend if the format ever changes, and the containing `subagents/`
#: directory is already the real discriminator.
_CLAUDE_SUBAGENT_RE = re.compile(r"^agent-.+\.jsonl$", re.IGNORECASE)


@dataclass(slots=True, frozen=True)
class SourceFile:
    """One file the collector may read, labelled with how to parse it."""

    path: Path
    agent: str
    kind: SourceKind
    #: For subagent sources: the Session the spend rolls up into.
    parent_session_id: str | None = None
    #: For pi nested subagent sessions: the tool call that spawned the run.
    agent_run_id: str | None = None
    #: For Claude sessions: the session id, which is also the filename stem.
    session_id: str | None = None

    @property
    def sort_key(self) -> tuple[str, str]:
        return (self.kind, str(self.path))


def discover_claude(projects_dir: Path) -> Iterator[SourceFile]:
    """Walk `~/.claude/projects/<slug>/`.

    Two shapes: `<slug>/<session-uuid>.jsonl` for a Session, and
    `<slug>/<session-uuid>/subagents/agent-<id>.jsonl` for its subagents. The
    subagent transcripts carry the PARENT sessionId, so their spend rolls up
    automatically -- but they must be ingested, because those tokens are real.
    """
    if not projects_dir.is_dir():
        return

    for project_dir in sorted(p for p in projects_dir.iterdir() if p.is_dir()):
        for entry in sorted(project_dir.iterdir()):
            if entry.is_file() and entry.suffix == ".jsonl":
                yield SourceFile(
                    path=entry, agent="claude-code", kind="claude_session",
                    session_id=entry.stem,
                )
                continue

            if not entry.is_dir():
                continue

            subagents_dir = entry / "subagents"
            if not subagents_dir.is_dir():
                continue
            # Recurse: subagent transcripts sit directly under `subagents/`, but
            # Workflow-spawned ones nest a further two levels under
            # `subagents/workflows/wf_<id>/`. A direct-children glob misses those
            # entirely -- 304 files and ~1,800 billed messages on this laptop.
            for sub in sorted(subagents_dir.rglob("*.jsonl")):
                # The `agent-` prefix is what separates transcripts from workflow
                # bookkeeping: `journal.jsonl` sits in the same directories and is
                # not a transcript.
                if not _CLAUDE_SUBAGENT_RE.match(sub.name):
                    continue
                yield SourceFile(
                    path=sub, agent="claude-code", kind="claude_subagent",
                    parent_session_id=entry.name,
                    session_id=entry.name,
                    agent_run_id=sub.stem.removeprefix("agent-"),
                )


def discover_pi_sessions(sessions_dir: Path) -> Iterator[SourceFile]:
    """Walk `~/.pi/agent/sessions/<slug>/`, excluding `claude-code-artifacts/`."""
    if not sessions_dir.is_dir():
        return

    for project_dir in sorted(p for p in sessions_dir.iterdir() if p.is_dir()):
        for entry in sorted(project_dir.iterdir()):
            if entry.is_file() and entry.suffix == ".jsonl":
                if _PI_SESSION_RE.match(entry.name):
                    yield SourceFile(path=entry, agent="pi", kind="pi_session",
                                     session_id=_pi_session_id(entry.name))
                continue

            if not entry.is_dir() or entry.name in PI_EXCLUDED_DIRS:
                continue

            # `<slug>/<session-dir>/<toolcall-id>/run-N/session.jsonl`
            for nested in sorted(entry.glob("*/run-*/session.jsonl")):
                yield SourceFile(
                    path=nested, agent="pi", kind="pi_subagent_session",
                    parent_session_id=_pi_session_id(entry.name),
                    agent_run_id=nested.parent.parent.name,
                )


def _pi_session_id(name: str) -> str | None:
    """pi filenames are `<iso-ts>_<session-uuid>[.jsonl]`.

    Used as a fallback when an incremental read starts past the `session` header
    line, which is the only other place the id appears.
    """
    stem = name[:-6] if name.endswith(".jsonl") else name
    _, sep, session_id = stem.partition("_")
    return session_id if sep else None


def discover_pi_run_history(path: Path) -> Iterator[SourceFile]:
    if path.is_file():
        yield SourceFile(path=path, agent="pi", kind="pi_run_history")


def discover_all(
    *,
    claude_projects_dir: Path,
    pi_sessions_dir: Path,
    pi_run_history_path: Path,
) -> list[SourceFile]:
    """Every file the collector will consider this run, in a stable order.

    Stable ordering matters for reproducibility: with `--max-files` or a time
    budget, a run reads a prefix of this list, and an unstable order would make
    which files got read depend on filesystem iteration order.
    """
    found: list[SourceFile] = []
    found.extend(discover_claude(claude_projects_dir))
    found.extend(discover_pi_sessions(pi_sessions_dir))
    found.extend(discover_pi_run_history(pi_run_history_path))
    return sorted(found, key=lambda s: s.sort_key)
