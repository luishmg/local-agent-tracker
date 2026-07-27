"""Allows `python -m tracker` as an alternative to the `tracker` console script."""

from __future__ import annotations

import sys

from tracker.cli import main

if __name__ == "__main__":
    sys.exit(main())
