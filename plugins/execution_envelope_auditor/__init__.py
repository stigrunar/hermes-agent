"""Deterministic shadow-mode execution-envelope auditor plugin."""

from __future__ import annotations

from .auditor import audit_execution_envelope
from .cli import audit_command, register_cli


def register(ctx) -> None:
    """Expose ``hermes execution-envelope-audit`` when the plugin is enabled."""

    ctx.register_cli_command(
        name="execution-envelope-audit",
        help="Audit an execution envelope without blocking or mutating runtime state",
        setup_fn=register_cli,
        handler_fn=audit_command,
        description="Deterministic JSON execution-envelope drift report (shadow mode)",
    )


__all__ = ["audit_execution_envelope", "register"]
