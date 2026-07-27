"""CLI surface for the execution-envelope auditor plugin."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, TextIO

from .auditor import audit_execution_envelope


def register_cli(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "input",
        nargs="?",
        default="-",
        help="JSON input file, or '-' for stdin (default)",
    )
    parser.add_argument(
        "--mode",
        choices=("shadow", "strict-fixture"),
        default="shadow",
        help="shadow always reports without blocking; strict-fixture exits 1 on findings",
    )
    parser.add_argument("--pretty", action="store_true", help="indent JSON output")
    parser.set_defaults(func=audit_command)


def _read_payload(path: str, stdin: TextIO) -> Any:
    if path == "-":
        return json.load(stdin)
    with Path(path).open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _write_json(value: Any, *, pretty: bool) -> None:
    if pretty:
        rendered = json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False)
    else:
        rendered = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    sys.stdout.write(rendered + "\n")


def audit_command(args: argparse.Namespace) -> int:
    try:
        payload = _read_payload(args.input, sys.stdin)
    except (OSError, json.JSONDecodeError, UnicodeError):
        _write_json(
            {
                "schema_version": 1,
                "mode": "shadow",
                "valid": False,
                "normalized_envelope": {},
                "findings": [
                    {
                        "code": "invalid_input",
                        "severity": "error",
                        "path": "$",
                        "message": "Input must be a readable UTF-8 JSON object.",
                    }
                ],
                "summary": {"error": 1, "warning": 0, "info": 0, "total": 1},
            },
            pretty=args.pretty,
        )
        return 2

    if not isinstance(payload, dict):
        payload = {}
    report = audit_execution_envelope(payload)
    report["mode"] = args.mode
    _write_json(report, pretty=args.pretty)
    if args.mode == "strict-fixture" and report["findings"]:
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="execution-envelope-audit",
        description="Deterministic shadow-mode execution-envelope auditor",
    )
    register_cli(parser)
    return audit_command(parser.parse_args(argv))
