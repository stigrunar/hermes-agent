"""Focused proof for the source-only outcome-envelope primitive."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from cron.outcome_envelope import (
    EnvelopeConflictError,
    EnvelopeValidationError,
    OutcomeEnvelopeStore,
    project_events,
)


_FIXTURE = Path(__file__).parents[1] / "fixtures" / "outcome_envelope" / "radar_r1.jsonl"


def _header(envelope_id: str = "candidate-1") -> dict:
    return {
        "schema_version": 1,
        "envelope_id": envelope_id,
        "subject": {"candidate_id": "candidate-1", "candidate_hash": "abc123"},
        "contract_refs": ["contract:test:v1"],
    }


def _event(
    event_id: str,
    *,
    occurred_at: str = "2026-08-28T10:00:00Z",
    outcome: object = "PASS",
    kind: str = "check",
    payload: dict | None = None,
    evidence_refs: list[str] | None = None,
) -> dict:
    return {
        "event_id": event_id,
        "occurred_at": occurred_at,
        "producer": {"domain": "test", "actor": "tester"},
        "kind": kind,
        "outcome": outcome,
        "evidence_refs": evidence_refs or ["evidence/test"],
        "payload": payload or {},
    }


def _create(store: OutcomeEnvelopeStore, envelope_id: str = "candidate-1") -> None:
    header = _header(envelope_id)
    store.create(envelope_id, header["subject"], header["contract_refs"])


def _read_fixture() -> tuple[dict, list[dict]]:
    records = [json.loads(line) for line in _FIXTURE.read_text(encoding="utf-8").splitlines()]
    return records[0], records[1:]


def test_header_and_event_schema_have_only_frozen_shared_fields(tmp_path):
    store = OutcomeEnvelopeStore(tmp_path)
    _create(store)
    event = _event("event-1")
    assert store.append_event("candidate-1", event) is True
    assert store.append_event(
        "candidate-1", _event("event-2", outcome={"status": "PASS"})
    ) is True

    records = [json.loads(line) for line in store.path_for("candidate-1").read_text().splitlines()]
    assert set(records[0]) == {"schema_version", "envelope_id", "subject", "contract_refs"}
    assert set(records[1]) == {
        "event_id",
        "occurred_at",
        "producer",
        "kind",
        "outcome",
        "evidence_refs",
        "payload",
    }
    assert records[1]["payload"] == {}
    assert records[2]["outcome"] == {"status": "PASS"}


def test_duplicate_event_is_idempotent_and_conflicting_reuse_fails_closed(tmp_path):
    store = OutcomeEnvelopeStore(tmp_path)
    _create(store)
    assert store.create(
        "candidate-1", _header()["subject"], _header()["contract_refs"]
    )["envelope_id"] == "candidate-1"
    with pytest.raises(EnvelopeConflictError):
        store.create(
            "candidate-1",
            {"candidate_id": "other", "candidate_hash": "abc123"},
            ["contract:test:v1"],
        )
    event = _event("event-1")
    assert store.append_event("candidate-1", event) is True
    before = store.path_for("candidate-1").read_bytes()

    assert store.append_event("candidate-1", copy.deepcopy(event)) is False
    assert store.path_for("candidate-1").read_bytes() == before

    conflict = copy.deepcopy(event)
    conflict["payload"] = {"changed": True}
    with pytest.raises(EnvelopeConflictError):
        store.append_event("candidate-1", conflict)
    assert store.path_for("candidate-1").read_bytes() == before


def test_unknown_and_malformed_schema_is_rejected_without_rewrite(tmp_path):
    store = OutcomeEnvelopeStore(tmp_path)
    _create(store)
    path = store.path_for("candidate-1")
    before = path.read_bytes()

    invalid = _event("event-1")
    invalid["unexpected"] = "not part of the shared schema"
    with pytest.raises(EnvelopeValidationError):
        store.append_event("candidate-1", invalid)
    assert path.read_bytes() == before

    path.write_bytes(before + b'{"event_id":"truncated"}')
    with pytest.raises(EnvelopeValidationError):
        store.read("candidate-1")
    assert path.read_bytes() == before + b'{"event_id":"truncated"}'


def test_replay_is_order_independent_but_persisted_events_keep_append_order(tmp_path):
    store = OutcomeEnvelopeStore(tmp_path)
    _create(store)
    events = [
        _event(
            "state-1",
            occurred_at="2026-08-28T10:01:00Z",
            outcome={"status": "PASS", "state": "approved_not_live"},
        ),
        _event("check-1", occurred_at="2026-08-28T10:02:00Z", outcome="NOT_VERIFIED"),
        _event(
            "state-2",
            occurred_at="2026-08-28T10:03:00Z",
            outcome={"status": "PASS", "state": "live_verified"},
        ),
    ]
    for event in events:
        assert store.append_event("candidate-1", event)

    persisted = store.read("candidate-1")
    assert [event["event_id"] for event in persisted["events"]] == [
        "state-1",
        "check-1",
        "state-2",
    ]
    forward = project_events(persisted["header"], persisted["events"])
    reverse = project_events(persisted["header"], list(reversed(persisted["events"])))
    assert forward == reverse
    assert forward["status"] == "live_verified"
    assert [event["event_id"] for event in forward["non_pass_events"]] == ["check-1"]


def test_not_verified_never_becomes_a_pass_without_explicit_later_pass(tmp_path):
    store = OutcomeEnvelopeStore(tmp_path)
    _create(store)
    not_verified = _event("qa-1", outcome="NOT_VERIFIED")
    assert store.append_event("candidate-1", not_verified)
    assert store.project("candidate-1")["status"] == "blocked"

    misleading = _event(
        "state-1",
        occurred_at="2026-08-28T10:01:00Z",
        outcome={"status": "NOT_VERIFIED", "state": "live_verified"},
    )
    assert store.append_event("candidate-1", misleading)
    assert store.project("candidate-1")["status"] == "blocked"

    explicit_pass = _event(
        "state-2",
        occurred_at="2026-08-28T10:02:00Z",
        outcome={"status": "PASS", "state": "live_verified"},
    )
    assert store.append_event("candidate-1", explicit_pass)
    projection = store.project("candidate-1")
    assert projection["status"] == "live_verified"
    assert projection["non_pass_events"][0]["outcome"] == "NOT_VERIFIED"


def test_waiver_is_separate_visible_bounded_and_expires(tmp_path):
    store = OutcomeEnvelopeStore(tmp_path)
    _create(store)
    assert store.append_event(
        "candidate-1",
        _event(
            "live-1",
            occurred_at="2026-08-28T10:00:00Z",
            outcome={"status": "PASS", "state": "live_verified"},
        ),
    )
    assert store.append_event(
        "candidate-1",
        _event("failed-1", occurred_at="2026-08-28T10:01:00Z", outcome="FAIL"),
    )
    waiver = _event(
        "waiver-1",
        occurred_at="2026-08-28T10:02:00Z",
        kind="waiver",
        outcome="WAIVED",
        payload={
            "decision_maker": "owner",
            "reason": "bounded source-only exception",
            "scope": {"criterion": "representative-check"},
            "expires_at": "2026-08-28T10:03:00Z",
        },
    )
    waiver["relates_to_event_id"] = "failed-1"
    assert store.append_event("candidate-1", waiver)

    active = store.project("candidate-1", as_of="2026-08-28T10:02:30Z")
    assert active["status"] == "live_with_active_waiver"
    assert active["base_state"] == "live_verified"
    assert len(active["active_waivers"]) == 1
    assert active["active_waivers"][0]["relates_to_event_id"] == "failed-1"
    assert active["active_waivers"][0]["scope"] == {"criterion": "representative-check"}
    assert active["non_pass_events"][0]["event_id"] == "failed-1"

    expired = store.project("candidate-1", as_of="2026-08-28T10:03:00Z")
    assert expired["status"] == "blocked"
    assert expired["active_waivers"] == []
    assert expired["waivers"][0]["active"] is False

    direct_derived_state = _event(
        "waiver-state",
        outcome={"status": "PASS", "state": "live_with_active_waiver"},
    )
    with pytest.raises(EnvelopeValidationError):
        store.append_event("candidate-1", direct_derived_state)


def test_profile_local_default_path_uses_get_hermes_home(monkeypatch, tmp_path):
    import cron.outcome_envelope as outcome_envelope

    home = tmp_path / "profile-home"
    monkeypatch.setattr(outcome_envelope, "get_hermes_home", lambda: home)
    store = outcome_envelope.OutcomeEnvelopeStore()
    _create(store)
    assert store.path_for("candidate-1") == home / "outcome-envelopes" / "candidate-1.jsonl"
    assert store.path_for("candidate-1").is_file()


def test_radar_r1_fixture_preserves_lineage_and_honest_broad_result(tmp_path):
    header, events = _read_fixture()
    store = OutcomeEnvelopeStore(tmp_path)
    store.create(header["envelope_id"], header["subject"], header["contract_refs"])
    for event in events:
        assert store.append_event(header["envelope_id"], event)

    persisted = store.read(header["envelope_id"])
    assert persisted["header"]["subject"] == {
        "candidate_id": "radar-r1",
        "candidate_hash": "188b6d53b1468797313255d0f004d9e29e0dd593",
    }
    qa_event = next(event for event in persisted["events"] if event["kind"] == "qa_matrix")
    assert qa_event["outcome"] == "NOT_VERIFIED"
    assert qa_event["payload"]["passed"] == 408
    assert qa_event["payload"]["failed"] == 1
    assert qa_event["payload"]["warnings"] == 1
    assert qa_event["payload"]["disposition"]

    projection = store.project(header["envelope_id"])
    assert projection["status"] == "live_verified"
    assert projection["non_pass_events"] == [qa_event]
    assert projection["event_ids"] == [event["event_id"] for event in events]
    fixture_text = _FIXTURE.read_text(encoding="utf-8")
    assert "/home/" not in fixture_text
    assert "_API_KEY" not in fixture_text
    assert "token" not in fixture_text.lower()
