#!/usr/bin/env python3
"""Bounded, opt-in Codex app-server canary (handshake only; no model turn)."""

from __future__ import annotations

import argparse
import json
import os
import sys

from agent.transports.codex_app_server import CodexAppServerClient


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex-bin", default="codex")
    parser.add_argument("--codex-home", default=None)
    parser.add_argument("--cwd", default=os.getcwd())
    parser.add_argument("--timeout", type=float, default=15.0)
    args = parser.parse_args()

    client = None
    try:
        client = CodexAppServerClient(codex_bin=args.codex_bin, codex_home=args.codex_home)
        client.initialize(
            client_name="hermes-liveness-canary",
            client_title="Hermes app-server liveness canary",
            client_version="1",
            timeout=args.timeout,
        )
        started = client.request("thread/start", {"cwd": args.cwd}, timeout=args.timeout)
        thread = started.get("thread") if isinstance(started, dict) else None
        thread_id = thread.get("id") if isinstance(thread, dict) else None
        if not thread_id:
            raise RuntimeError("thread/start returned no thread id")
        print(json.dumps({"status": "handshake_ok", "thread_id": thread_id, "initialized": True}))
        return 0
    except Exception as exc:
        print(f"codex app-server liveness canary failed: {exc}", file=sys.stderr)
        return 1
    finally:
        if client is not None:
            client.close()


if __name__ == "__main__":
    raise SystemExit(main())
