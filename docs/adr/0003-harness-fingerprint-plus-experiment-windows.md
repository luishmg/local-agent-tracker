# Harness attribution via git-derived fingerprints plus explicit experiment windows

Neither agent records which version of the user's harness (pi-config, ai-skills, claude-config) produced a session, yet "same model, before vs after my change" is the system's core query. We construct the dimension two ways: automatically, by mapping each message's timestamp to the commit that was HEAD in each config repo at that moment (retroactive, derived from git history, zero user discipline); and explicitly, via named `tracker experiment start/stop` windows that record *why* a change was made.

## Consequences

- All historical sessions become comparable by fingerprint without any prior labeling.
- Fingerprints capture only committed harness state — uncommitted config changes are invisible, which is a reason to commit config tweaks before evaluating them.
- Experiments and fingerprints are independent tables joined by time range; they can disagree (an experiment spanning several commits), and that is informative rather than an error.
