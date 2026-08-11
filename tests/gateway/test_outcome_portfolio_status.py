from __future__ import annotations

from types import SimpleNamespace

import pytest

from gateway.slash_commands import GatewaySlashCommandsMixin
from hermes_cli import outcome_operating_model as oom


class _Runner(GatewaySlashCommandsMixin):
    pass


@pytest.mark.asyncio
async def test_status_portfolio_reuses_existing_status_handler(monkeypatch):
    monkeypatch.setattr(
        oom,
        "render_current_portfolio_status",
        lambda: "FOCUS:\n- demo\nINCIDENT:\n- none\nWARM:\n- none\nDECISIONS:\n- none",
    )
    event = SimpleNamespace(
        text="/status portfolio",
        get_command_args=lambda: "portfolio",
    )

    result = await _Runner()._handle_status_command(event)

    assert result.startswith("FOCUS:")
    assert "DECISIONS:" in result
