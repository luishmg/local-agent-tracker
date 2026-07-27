"""
Fixture hygiene.

The collector reads transcripts that can contain anything the user has worked on.
The rule (architecture.md §8) is that content is never stored — and a test suite
that ships a copied-in real transcript violates that rule in the repository
itself, where it is permanent and public.

So fixtures are built in-process by `tests/factories.py` and written to `tmp_path`.
This guard makes that a checked property rather than a convention.
"""

from __future__ import annotations

from pathlib import Path

from tests._codescan import code_only

TESTS_DIR = Path(__file__).parent

#: A real transcript is megabytes. Anything this large in tests/ is a copied file.
MAX_TEST_FILE_BYTES = 60_000


def test_no_transcript_fixtures_are_committed() -> None:
    committed = [
        p for p in TESTS_DIR.rglob("*")
        if p.is_file() and p.suffix in {".jsonl", ".ndjson"} and "__pycache__" not in p.parts
    ]
    assert not committed, (
        f"committed transcript fixtures found: {[str(p) for p in committed]}. "
        f"Build them in-process with tests/factories.py and write to tmp_path."
    )


def test_no_test_file_is_large_enough_to_be_a_copied_transcript() -> None:
    oversized = [
        (p.name, p.stat().st_size)
        for p in TESTS_DIR.rglob("*.py")
        if p.is_file() and p.stat().st_size > MAX_TEST_FILE_BYTES
    ]
    assert not oversized, f"suspiciously large test files: {oversized}"


def test_factories_never_read_from_the_filesystem() -> None:
    """A factory that reached into the real `~/.claude` would launder genuine
    conversation content into fixtures while still looking synthetic.

    Checked against code with prose stripped: the module docstring necessarily
    names those directories in order to explain what it is imitating.
    """
    code = code_only((TESTS_DIR / "factories.py").read_text(encoding="utf-8"))
    for forbidden in ("home", "expanduser", "environ", "read_text", "read_bytes", "iterdir", "glob"):
        assert forbidden not in code, (
            f"factories.py uses {forbidden!r} -- fixtures must be built from "
            f"literals, never read from disk"
        )
