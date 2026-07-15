# Batch JSONL parsing over live telemetry

Both agents already persist complete token/cost data to local JSONL, and the laptop (7.7 GB RAM, earlyoom after a swap-thrash livelock) cannot afford always-on processes. We therefore collect by parsing the JSONL trees from a 5-minute systemd timer that runs and exits, and deliberately do not enable Claude Code's OTel exporter or write a pi extension — live telemetry would add an OTLP receiver daemon without adding any data.

## Considered Options

- Claude Code OTel (`CLAUDE_CODE_ENABLE_TELEMETRY=1`) + pi `message_end` extension → OTLP receiver: rejected — requires a resident receiver; the JSONL already contains everything.
- Hybrid (batch + live pi extension): rejected for now — more code for sub-minute freshness nobody asked for.

## Consequences

- Data freshness is bounded by the 5-minute timer.
- If live visibility is ever needed, pi's `message_end` extension event (full `usage.cost` per message) is the documented upgrade path.
