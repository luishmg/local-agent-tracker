"""
Shared helper for guard tests that check what the code *does*, not what it says.

Several invariants in this project are enforced by scanning source files. All of
them hit the same problem: the comments and docstrings explaining an invariant
necessarily quote the very names the invariant forbids. Stripping STRING and
COMMENT tokens before matching keeps the documentation free while still catching
real use.
"""

from __future__ import annotations

import io
import tokenize

_STRIPPED = (
    tokenize.COMMENT,
    tokenize.STRING,
    tokenize.FSTRING_START,
    tokenize.FSTRING_MIDDLE,
    tokenize.FSTRING_END,
)


def code_only(source: str) -> str:
    """Return `source` with comments and string literals removed."""
    kept: list[str] = []
    try:
        for tok in tokenize.generate_tokens(io.StringIO(source).readline):
            if tok.type in _STRIPPED:
                continue
            kept.append(tok.string)
    except (tokenize.TokenError, IndentationError):  # pragma: no cover
        return source
    return " ".join(kept)
