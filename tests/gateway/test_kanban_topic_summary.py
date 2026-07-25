"""Behavioral tests for mapped Telegram forum-topic Kanban summaries."""

import json
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import MessageEvent
from gateway.session import SessionEntry, SessionSource, build_session_key
from hermes_cli import kanban_db as kb


@pytest.fixture
def topic_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    kb.init_db()
    return home


def _write_view(home: Path, target: str, *, label="Arbeid", sources=None):
    path = home / "state" / "kanban_topic_writeback_map.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({
            "topic_views": {
                target: {
                    "label": label,
                    "sources": [{"board": "default"}] if sources is None else sources,
                }
            }
        }),
        encoding="utf-8",
    )


def _event(text: str, *, chat_id="-100", thread_id="42", platform=Platform.TELEGRAM):
    return MessageEvent(
        text=text,
        source=SessionSource(
            platform=platform,
            chat_id=chat_id,
            thread_id=thread_id,
            chat_type="group",
        ),
    )


async def _handle(event):
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    return await runner._handle_kanban_command(event)


def _create(
    conn,
    title,
    *,
    tenant=None,
    assignee=None,
    triage=False,
    parents=(),
    initial_status="running",
):
    return kb.create_task(
        conn,
        title=title,
        tenant=tenant,
        assignee=assignee,
        triage=triage,
        parents=parents,
        initial_status=initial_status,
    )


@pytest.mark.asyncio
async def test_bare_topic_summary_is_exactly_mapped_and_excludes_inactive(topic_home):
    target = "telegram:-100:42"
    _write_view(topic_home, target, label="Lansering", sources=[{"board": "default"}])
    conn = kb.connect(board="default")
    try:
        _create(conn, "Skriv plan", assignee="alice")
        _create(conn, "Sjekk status", triage=True)
        parent = _create(conn, "Blokkerende forelder")
        _create(conn, "Avhengig arbeid", parents=[parent])
        running = _create(conn, "Pågående arbeid")
        assert kb.claim_task(conn, running, claimer="test") is not None
        blocked = _create(conn, "Venter på avklaring")
        assert kb.block_task(conn, blocked, reason="test") is True
        scheduled = _create(conn, "Senere arbeid")
        assert kb.schedule_task(conn, scheduled, reason="test") is True
        done = _create(conn, "Ferdig arbeid")
        kb.complete_task(conn, done, result="done")
        cancelled = _create(conn, "Avbrutt arbeid")
        kb.cancel_task(conn, cancelled, reason="test")
    finally:
        conn.close()

    output = await _handle(_event("/kanban"))

    assert output.startswith("📌 Lansering")
    assert "Triagering:" in output
    assert "Å gjøre:" in output
    assert "Klar:" in output
    assert "Kjører:" in output
    assert "Blokkert:" in output
    assert "Planlagt:" in output
    assert "• Skriv plan — alice" in output
    assert "Sjekk status" in output
    assert "Ferdig arbeid" not in output
    assert "Avbrutt arbeid" not in output
    assert "t_" not in output
    assert "kanban.db" not in output


@pytest.mark.asyncio
async def test_topic_selectors_pin_boards_and_tenants_and_collapse_duplicates(topic_home):
    kb.create_board("second")
    target = "telegram:-200:7"
    _write_view(
        topic_home,
        target,
        label="Pågående",
        sources=[
            {"board": "default", "tenants": ["acme"]},
            {"board": "second", "tenants": ["acme", "beta"]},
        ],
    )
    conn = kb.connect(board="default")
    try:
        _create(conn, "Del oppgave", tenant="acme")
        _create(conn, "Ikke valgt tenant", tenant="other")
    finally:
        conn.close()
    conn = kb.connect(board="second")
    try:
        _create(conn, "Del oppgave", tenant="acme")
        _create(conn, "Beta arbeid", tenant="beta")
        _create(conn, "Skal ikke vises", tenant="other")
    finally:
        conn.close()

    output = await _handle(_event("/kanban", chat_id="-200", thread_id="7"))

    assert output.count("• Del oppgave") == 1
    assert "Del oppgave (×2)" in output
    assert "• Beta arbeid" in output
    assert "Ikke valgt tenant" not in output
    assert "Skal ikke vises" not in output


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        None,
        {"topic_views": []},
        {"topic_views": {"telegram:-300:9": {"label": "x", "sources": []}}},
        {"topic_views": {"telegram:-300:9": {"label": "x", "sources": [{"board": 7}]}}},
        {"topic_views": {"telegram:-300:9": {"label": "x", "sources": [{"board": "missing"}]}}},
    ],
)
async def test_missing_or_malformed_mapping_fails_closed(topic_home, payload):
    path = topic_home / "state" / "kanban_topic_writeback_map.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    if payload is not None:
        path.write_text(json.dumps(payload), encoding="utf-8")

    output = await _handle(_event("/kanban", chat_id="-300", thread_id="9"))

    assert output == "Kanban-visning ikke tilgjengelig for dette emnet."


@pytest.mark.asyncio
async def test_empty_mapped_view_names_view(topic_home):
    _write_view(
        topic_home,
        "telegram:-400:3",
        label="Min telefon",
        sources=[{"board": "default"}],
    )

    output = await _handle(_event("/kanban", chat_id="-400", thread_id="3"))

    assert output == "📌 Min telefon: ingen aktive oppgaver."


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["/kanban-list", "/kanban_list"])
async def test_aliases_resolve_and_bare_alias_uses_topic_summary(topic_home, command):
    _write_view(topic_home, "telegram:-500:8", label="Aliasvisning")

    output = await _handle(_event(command, chat_id="-500", thread_id="8"))

    assert output == "📌 Aliasvisning: ingen aktive oppgaver."


@pytest.mark.asyncio
async def test_explicit_help_list_and_board_are_unchanged(topic_home):
    from hermes_cli.kanban import run_slash

    _write_view(topic_home, "telegram:-600:11", label="Topic")
    kb.create_board("explicit")
    conn = kb.connect(board="explicit")
    try:
        _create(conn, "Explicit task")
    finally:
        conn.close()

    help_output = await _handle(_event("/kanban help", chat_id="-600", thread_id="11"))
    list_output = await _handle(_event("/kanban list", chat_id="-600", thread_id="11"))
    board_output = await _handle(
        _event("/kanban --board explicit list", chat_id="-600", thread_id="11")
    )

    assert help_output == run_slash("help")
    assert "Explicit task" not in list_output
    assert "Explicit task" in board_output


@pytest.mark.asyncio
async def test_bare_non_topic_delegates_to_existing_run_slash(topic_home):
    from hermes_cli.kanban import run_slash

    output = await _handle(
        _event("/kanban", platform=Platform.DISCORD, chat_id="room", thread_id="")
    )

    assert output == run_slash("")


@pytest.mark.asyncio
async def test_bare_telegram_dm_topic_delegates_to_existing_run_slash(topic_home):
    from hermes_cli.kanban import run_slash

    event = _event("/kanban", chat_id="dm", thread_id="99")
    event.source.chat_type = "dm"

    output = await _handle(event)

    assert output == run_slash("")


def test_kanban_aliases_are_canonical_gateway_commands():
    from hermes_cli.commands import GATEWAY_KNOWN_COMMANDS, resolve_command

    for alias in ("kanban-list", "kanban_list"):
        assert resolve_command(alias).name == "kanban"
        assert alias in GATEWAY_KNOWN_COMMANDS


@pytest.mark.asyncio
async def test_bare_topic_summary_bypasses_running_agent_without_interrupt(topic_home):
    from gateway.run import GatewayRunner

    source = SessionSource(
        platform=Platform.TELEGRAM,
        user_id="u1",
        chat_id="-700",
        thread_id="12",
        user_name="tester",
        chat_type="group",
    )
    _write_view(topic_home, "telegram:-700:12", label="Aktiv visning")

    runner: Any = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="test")}
    )
    adapter = MagicMock()
    adapter.send = AsyncMock()
    adapter._pending_messages = {}
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._voice_mode = {}
    runner.hooks = SimpleNamespace(emit=AsyncMock(), loaded_hooks=False)
    runner.session_store = MagicMock()
    runner.session_store.get_or_create_session.return_value = SessionEntry(
        session_key=build_session_key(source),
        session_id="sess-topic",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="group",
        total_tokens=0,
    )
    runner.session_store.load_transcript.return_value = []
    runner.session_store.has_any_sessions.return_value = True
    session_key = build_session_key(source)
    running_agent = MagicMock()
    running_agent.get_activity_summary.return_value = {
        "seconds_since_activity": 0.0,
        "last_activity_desc": "test",
        "api_call_count": 1,
        "max_iterations": 10,
    }
    runner._running_agents = {session_key: running_agent}
    runner._running_agents_ts = {session_key: time.time()}
    runner._pending_messages = {}
    runner._queued_events = {}
    runner._pending_approvals = {}
    runner._session_db = MagicMock()
    runner._session_db.get_session_title.return_value = None
    runner._reasoning_config = None
    runner._provider_routing = {}
    runner._fallback_model = None
    runner._show_reasoning = False
    runner._is_user_authorized = lambda source: True

    output = await runner._handle_message(MessageEvent(text="/kanban", source=source))

    assert output == "📌 Aktiv visning: ingen aktive oppgaver."
    running_agent.interrupt.assert_not_called()
