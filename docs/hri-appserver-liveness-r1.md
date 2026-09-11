# HRI App Server liveness

The Codex app-server bridge treats only substantive, in-scope native progress as
turn activity. Non-empty assistant/reasoning/plan deltas, tool lifecycle events,
and non-empty tool output, progress, diff, plan, or patch payloads call the
existing `AIAgent._touch_activity` path before optional display callbacks.

Transport lifecycle, keepalive/polling, malformed, empty, unknown, and
boundary-only notifications do not refresh the clock. The session's existing
thread/turn ownership fence runs before the bridge, so stale child notifications
cannot refresh a later parent turn. Display callback failures are guarded and
cannot interrupt execution. The existing 600-second watchdog and its
generation/timestamp-locked abort settlement are unchanged: this adds no
timeout increase, fake heartbeat, second watchdog, or config setting.

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
