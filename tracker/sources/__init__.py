"""
Source discovery and the agent-native parsers.

`tracker.sources.claude_code` and `tracker.sources.pi` are the ONLY modules in
this package permitted to know agent-native field names (`input_tokens` vs
`input`, `stop_reason` vs `stopReason`, and so on). Everything downstream speaks
the `tracker.normalize.models` vocabulary. `tests/test_no_raw_field_names_leak.py`
enforces that boundary, because it erodes silently otherwise.
"""

from __future__ import annotations
