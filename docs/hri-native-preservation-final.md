# HRI R4 native preservation correction

Project `p_155df2bb`, Outcome `o_cde72dc3`; base `9066252f717dd4b1c2e9351a257ccadd408fdac6`.

## Implemented

- TUI hosted-room capabilities now bind through the existing handler registry without an undefined default.
- Persisted prompt identity parsing isolates the renderer-owned runtime block from project/host decoys; loop wakeups dedupe identical route/timestamp candidates and keep all SessionDB work off the event loop.
- Read-only `file_readonly` and `skills_readonly` toolsets are restored without mutation tools.
- Kanban preview enforces global in-progress capacity and records respawn guards; WAL cadence remains on the existing real-tick helper while preview stays SELECT-only.
- Legacy notification subscriptions receive a one-time event baseline that preserves nonzero cursors and suppresses history replay.
- Review handoff preserves a live reviewer claim until stale-claim release confirms termination.
- Corrupt-board and WAL fixtures now exercise the reconciled review-lane and preview contracts.

## Local proof

- Affected Python sources/tests compile successfully with `python -m compileall`.
- `git diff --check` passes.
- No pytest or live service/database process was run in this implementation lane; owner acceptance remains pending.

This is an uncommitted internal source correction. No deployment, push, commit, credential/config change, or live business-data operation was performed.
