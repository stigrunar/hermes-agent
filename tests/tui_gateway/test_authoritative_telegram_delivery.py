from __future__ import annotations

import json
from types import SimpleNamespace

from tui_gateway.authoritative_delivery import (
    deliver_resumed_telegram_response,
    reserve_resumed_telegram_delivery,
)


class FakeDB:
    def __init__(self, row=None, assistant_row_id=41):
        self.row = row
        self.assistant_row_id = assistant_row_id
        self.lookups = []

    def get_gateway_session_metadata(self, session_ids):
        self.lookups.append(list(session_ids))
        return {"stored-session": self.row} if self.row is not None else {}

    def latest_message_row_id(self, session_id, *, role):
        assert session_id == "stored-session"
        assert role == "assistant"
        return self.assistant_row_id


class FakeLedger:
    def __init__(self, *, created=True, state="pending", enabled=True):
        self.created = created
        self.state = state
        self.enabled = enabled
        self.calls = []

    def ledger_enabled(self):
        return self.enabled

    def compute_obligation_id(self, session_key, message_ref, content):
        self.calls.append(("compute", session_key, message_ref, content))
        return "obligation-safe"

    def reserve_obligation(self, **kwargs):
        self.calls.append(("reserve", kwargs))
        return self.created, self.state

    def mark_attempting(self, obligation_id):
        self.calls.append(("attempting", obligation_id))

    def mark_delivered(self, obligation_id):
        self.calls.append(("delivered", obligation_id))

    def mark_failed(self, obligation_id, error):
        self.calls.append(("failed", obligation_id, error))


def telegram_dm_row(**overrides):
    row = {
        "id": "stored-session",
        "source": "telegram",
        "chat_type": "dm",
        "chat_id": "private-chat-id",
        "thread_id": "private-topic-id",
        "origin_json": json.dumps(
            {
                "platform": "telegram",
                "chat_type": "dm",
                "chat_id": "private-chat-id",
            }
        ),
    }
    row.update(overrides)
    return row


def telegram_forum_row(**overrides):
    row = {
        "id": "stored-session",
        "source": "telegram",
        "chat_type": "group",
        "chat_id": "forum-chat-id",
        "thread_id": "forum-topic-id",
        "origin_json": json.dumps(
            {
                "platform": "telegram",
                "chat_type": "group",
                "chat_id": "forum-chat-id",
                "thread_id": "forum-topic-id",
            }
        ),
    }
    row.update(overrides)
    return row


def test_delivers_exact_response_once_to_persisted_dm_topic():
    db = FakeDB(telegram_dm_row())
    ledger = FakeLedger()
    sends = []
    profile_config = SimpleNamespace(token="profile-token")

    async def sender(config, chat_id, message, *, thread_id=None):
        sends.append((config, chat_id, message, thread_id))
        return {"success": True, "message_id": "remote-message"}

    receipt = deliver_resumed_telegram_response(
        db=db,
        session_key="stored-session",
        response="Exact final response\n",
        explicitly_resumed_from_authoritative_ui=True,
        ledger=ledger,
        sender_loader=lambda _profile_home: (profile_config, sender),
    )

    assert receipt == {"platform": "telegram", "status": "delivered"}
    assert sends == [
        (
            profile_config,
            "private-chat-id",
            "Exact final response\n",
            "private-topic-id",
        )
    ]
    assert (
        "compute",
        "stored-session",
        "assistant-row:41",
        "Exact final response\n",
    ) in ledger.calls
    assert ("delivered", "obligation-safe") in ledger.calls


def test_completed_obligation_is_idempotent_and_does_not_send_again():
    ledger = FakeLedger(created=False, state="delivered")

    def sender_loader():
        raise AssertionError("sender must not be loaded for an already delivered turn")

    receipt = deliver_resumed_telegram_response(
        db=FakeDB(telegram_dm_row()),
        session_key="stored-session",
        response="same response",
        explicitly_resumed_from_authoritative_ui=True,
        ledger=ledger,
        sender_loader=lambda _profile_home: sender_loader(),
    )

    assert receipt == {"platform": "telegram", "status": "delivered"}
    assert not any(call[0] == "attempting" for call in ledger.calls)


def test_send_failure_is_recorded_and_receipt_contains_no_origin_or_secret():
    ledger = FakeLedger()

    async def sender(_config, _chat_id, _message, *, thread_id=None):
        assert thread_id == "private-topic-id"
        return {"error": "request with profile-token failed for private-chat-id"}

    receipt = deliver_resumed_telegram_response(
        db=FakeDB(telegram_dm_row()),
        session_key="stored-session",
        response="saved answer",
        explicitly_resumed_from_authoritative_ui=True,
        ledger=ledger,
        sender_loader=lambda _profile_home: (
            SimpleNamespace(token="profile-token"),
            sender,
        ),
    )

    assert receipt == {
        "platform": "telegram",
        "status": "failed",
        "error": "delivery_failed",
    }
    assert (
        "failed",
        "obligation-safe",
        "standalone telegram delivery failed",
    ) in ledger.calls
    serialized = json.dumps(receipt)
    assert "private-chat-id" not in serialized
    assert "private-topic-id" not in serialized
    assert "profile-token" not in serialized


def test_delivers_group_forum_topic_and_accepts_object_sender_result():
    ledger = FakeLedger()
    sends = []

    async def sender(config, chat_id, message, *, thread_id=None):
        sends.append((config, chat_id, message, thread_id))
        return SimpleNamespace(success=True)

    profile_config = SimpleNamespace(token="work-profile-token")
    receipt = deliver_resumed_telegram_response(
        db=FakeDB(telegram_forum_row()),
        session_key="stored-session",
        response="topic response",
        explicitly_resumed_from_authoritative_ui=True,
        profile_home="/profiles/work",
        ledger=ledger,
        sender_loader=lambda profile_home: (
            profile_config if profile_home == "/profiles/work" else None,
            sender,
        ),
    )

    assert receipt == {"platform": "telegram", "status": "delivered"}
    assert sends == [
        (profile_config, "forum-chat-id", "topic response", "forum-topic-id")
    ]


def test_group_without_thread_and_mismatched_thread_do_not_send():
    cases = [
        telegram_forum_row(thread_id=None),
        telegram_forum_row(
            origin_json=json.dumps(
                {
                    "platform": "telegram",
                    "chat_type": "group",
                    "chat_id": "forum-chat-id",
                    "thread_id": "different-topic-id",
                }
            )
        ),
    ]
    for row in cases:
        ledger = FakeLedger()
        receipt = deliver_resumed_telegram_response(
            db=FakeDB(row),
            session_key="stored-session",
            response="answer",
            explicitly_resumed_from_authoritative_ui=True,
            ledger=ledger,
            sender_loader=lambda _profile_home: (_ for _ in ()).throw(
                AssertionError("must not send")
            ),
        )
        assert receipt is None
        assert ledger.calls == []


def test_unresumed_legacy_malformed_and_unsupported_sessions_do_not_send():
    cases = [
        (False, telegram_dm_row()),
        (True, telegram_dm_row(origin_json=None)),
        (True, telegram_dm_row(chat_type="group")),
        (True, telegram_dm_row(source="discord")),
        (
            True,
            telegram_dm_row(
                origin_json=json.dumps(
                    {
                        "platform": "telegram",
                        "chat_type": "dm",
                        "chat_id": "different-chat-id",
                    }
                )
            ),
        ),
        (
            True,
            telegram_dm_row(
                chat_type="channel",
                origin_json=json.dumps(
                    {
                        "platform": "telegram",
                        "chat_type": "channel",
                        "chat_id": "private-chat-id",
                    }
                ),
            ),
        ),
    ]
    for explicitly_resumed, row in cases:
        ledger = FakeLedger()
        receipt = deliver_resumed_telegram_response(
            db=FakeDB(row),
            session_key="stored-session",
            response="answer",
            explicitly_resumed_from_authoritative_ui=explicitly_resumed,
            ledger=ledger,
            sender_loader=lambda _profile_home: (_ for _ in ()).throw(
                AssertionError("must not send")
            ),
        )
        assert receipt is None
        assert ledger.calls == []


def test_empty_response_and_missing_persisted_assistant_row_do_not_send():
    ledger = FakeLedger()
    assert (
        deliver_resumed_telegram_response(
            db=FakeDB(telegram_dm_row()),
            session_key="stored-session",
            response="  ",
            explicitly_resumed_from_authoritative_ui=True,
            ledger=ledger,
        )
        is None
    )
    receipt = deliver_resumed_telegram_response(
        db=FakeDB(telegram_dm_row(), assistant_row_id=None),
        session_key="stored-session",
        response="not durably addressable",
        explicitly_resumed_from_authoritative_ui=True,
        ledger=ledger,
    )
    assert receipt == {
        "platform": "telegram",
        "status": "failed",
        "error": "delivery_not_tracked",
    }
    assert ledger.calls == []


def test_profile_scope_wraps_config_load_and_send_and_restores_in_reverse_order(
    monkeypatch,
):
    events = []
    profile_home = "/profiles/work"

    monkeypatch.setattr(
        "hermes_constants.set_hermes_home_override",
        lambda home: events.append(("set_home", home)) or "home-token",
    )
    monkeypatch.setattr(
        "agent.secret_scope.build_profile_secret_scope",
        lambda home: events.append(("build_secrets", str(home))) or {"TOKEN": "work"},
    )
    monkeypatch.setattr(
        "agent.secret_scope.set_secret_scope",
        lambda scope: events.append(("set_secrets", scope)) or "secret-token",
    )
    monkeypatch.setattr(
        "agent.secret_scope.reset_secret_scope",
        lambda token: events.append(("reset_secrets", token)),
    )
    monkeypatch.setattr(
        "hermes_constants.reset_hermes_home_override",
        lambda token: events.append(("reset_home", token)),
    )

    async def sender(_config, _chat_id, _message, *, thread_id=None):
        events.append(("send", thread_id))
        return {"success": True}

    def sender_loader(home):
        events.append(("load_config", home))
        return SimpleNamespace(token="work"), sender

    receipt = deliver_resumed_telegram_response(
        db=FakeDB(telegram_dm_row(thread_id=None)),
        session_key="stored-session",
        response="profile response",
        explicitly_resumed_from_authoritative_ui=True,
        profile_home=profile_home,
        ledger=FakeLedger(),
        sender_loader=sender_loader,
    )

    assert receipt == {"platform": "telegram", "status": "delivered"}
    assert events == [
        ("set_home", profile_home),
        ("build_secrets", profile_home),
        ("set_secrets", {"TOKEN": "work"}),
        ("load_config", profile_home),
        ("send", None),
        ("reset_secrets", "secret-token"),
        ("reset_home", "home-token"),
    ]


def test_profile_scoped_reservation_uses_profile_ledger(tmp_path, monkeypatch):
    """The durable row is created in the stored profile before any send."""
    from gateway import delivery_ledger

    launch_home = tmp_path / "launch"
    profile_home = tmp_path / "profiles" / "work"
    launch_home.mkdir()
    profile_home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(launch_home))

    reservation = reserve_resumed_telegram_delivery(
        db=FakeDB(telegram_dm_row()),
        session_key="stored-session",
        response="profile-owned response",
        explicitly_resumed_from_authoritative_ui=True,
        profile_home=str(profile_home),
        ledger=delivery_ledger,
    )

    assert reservation and reservation.get("obligation_id")
    assert (profile_home / "state.db").exists()
    assert not (launch_home / "state.db").exists()
