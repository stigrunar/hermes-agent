# HRI App Server liveness

The Codex app-server session treats only substantive, exactly attributable native
progress as turn activity. Non-empty assistant/reasoning/plan deltas, tool
lifecycle events, and non-empty tool output, progress, diff, plan, or patch
payloads call the existing `AIAgent._touch_activity` path before optional display
callbacks.

Liveness requires explicit current `threadId` and `turnId` identity. Unscoped,
internally conflicting, foreign-thread, foreign-turn, and late-old-turn events
cannot refresh it. Transport lifecycle, keepalive/polling, malformed, empty,
unknown, and boundary-only notifications also do not refresh it. A permissive
legacy event may still reach the guarded display callback, but it cannot enter
the authoritative projector, mutate the turn result, complete the turn, or
touch liveness.

Structured progress is schema-shaped before it is counted: plan steps require
their status and step fields, reasoning summaries/content require text entries,
and structured file-change entries require a path, diff, and valid change kind.
A merely non-empty malformed object is not progress.

The session's existing `turn_timeout` is a silence window, not an absolute turn
deadline. Its default remains 600 seconds. Each strictly owned substantive event
resets that window and the same authoritative classifier touches the existing
generation/timestamp-locked `AIAgent` watchdog. Continuous progress may therefore
cross 600 seconds of total elapsed time; 600 seconds without progress still
interrupts and retires the app-server session. This adds no timeout increase,
fake heartbeat, competing watchdog, or config setting.

The focused deterministic gate is:

```bash
HERMES_PYTHON=/home/hermes/.hermes/hermes-agent/venv/bin/python \
HERMES_TEST_FILE_TIMEOUT=90 HERMES_TEST_FILE_RETRIES=0 \
scripts/run_tests.sh -j 2 \
  tests/agent/test_codex_app_server_liveness.py \
  tests/agent/test_codex_app_server_event_bridge.py \
  tests/agent/transports/test_codex_app_server_session.py
```

An opt-in no-model handshake canary is available as
`python scripts/canary_codex_app_server_liveness.py`; it requires a locally
installed and authenticated `codex` binary and does not prove a model turn or
runtime installation.
