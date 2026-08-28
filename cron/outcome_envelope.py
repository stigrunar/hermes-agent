"""Versioned, append-only local outcome envelopes.

An outcome envelope is deliberately a small local artifact: one immutable
header followed by immutable JSONL events.  It is a lineage/evidence aid, not
an execution database, scheduler integration, or model-facing tool.  Candidate
status is computed by :func:`project_events` from the event stream every time;
no aggregate status is persisted.

The shared event shape is intentionally narrow.  Domain-specific fields belong
inside ``payload`` and are never promoted into columns in this module.  The
optional ``state`` member of a mapping-valued ``outcome`` is the one shared
projection hint used for candidate lifecycle states; normal domain outcomes
can remain simple strings such as ``PASS``, ``FAIL``, ``NOT_VERIFIED``,
``delivered``, or ``deferred``.
"""

from __future__ import annotations

import copy
import datetime as _datetime
import json
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

from hermes_constants import get_hermes_home

try:  # pragma: no cover - the non-POSIX branch is exercised on Windows CI.
    import fcntl as _fcntl
except ImportError:  # pragma: no cover
    _fcntl = None

try:  # pragma: no cover - the non-Windows branch is exercised on Linux CI.
    import msvcrt as _msvcrt
except ImportError:  # pragma: no cover
    _msvcrt = None


SCHEMA_VERSION = 1
OUTCOME_ENVELOPE_DIRNAME = "outcome-envelopes"
WAIVER_KIND = "waiver"
NON_PASS_OUTCOMES = frozenset({"FAIL", "NOT_VERIFIED"})
PASS_OUTCOME = "PASS"

CANDIDATE_NON_TERMINAL_STATES = (
    "collecting",
    "blocked",
    "approved_not_live",
    "deploying",
)
CANDIDATE_TERMINAL_STATES = (
    "live_verified",
    "live_with_active_waiver",
    "rejected",
    "withdrawn",
    "superseded",
)
CANDIDATE_STATES = frozenset(
    CANDIDATE_NON_TERMINAL_STATES + CANDIDATE_TERMINAL_STATES
)

_HEADER_KEYS = frozenset({"schema_version", "envelope_id", "subject", "contract_refs"})
_SUBJECT_KEYS = frozenset({"candidate_id", "candidate_hash"})
_EVENT_REQUIRED_KEYS = frozenset(
    {
        "event_id",
        "occurred_at",
        "producer",
        "kind",
        "outcome",
        "evidence_refs",
        "payload",
    }
)
_EVENT_OPTIONAL_KEYS = frozenset({"attempt_id", "relates_to_event_id"})
_EVENT_KEYS = _EVENT_REQUIRED_KEYS | _EVENT_OPTIONAL_KEYS
_PRODUCER_KEYS = frozenset({"domain", "actor"})
_OUTCOME_KEYS = frozenset({"status", "state"})
_WAIVER_REQUIRED_PAYLOAD_KEYS = frozenset(
    {"decision_maker", "reason", "scope", "expires_at"}
)


class OutcomeEnvelopeError(ValueError):
    """Base error for malformed, conflicting, or unavailable envelopes."""


class EnvelopeValidationError(OutcomeEnvelopeError):
    """Raised when an envelope header or event violates the schema."""


class EnvelopeConflictError(OutcomeEnvelopeError):
    """Raised when an existing envelope/event ID has different content."""


class EnvelopeNotFoundError(OutcomeEnvelopeError):
    """Raised when an operation refers to an envelope that does not exist."""


class EnvelopeLockError(OutcomeEnvelopeError):
    """Raised when the append/read lock cannot be acquired safely."""


def _non_empty_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise EnvelopeValidationError(f"{field} must be a non-empty string")
    if "\x00" in value:
        raise EnvelopeValidationError(f"{field} must not contain NUL")
    return value


def _validate_exact_keys(
    value: Mapping[str, Any], expected: frozenset[str], field: str
) -> None:
    keys = set(value)
    missing = expected - keys
    unknown = keys - expected
    if missing or unknown:
        details = []
        if missing:
            details.append(f"missing {sorted(missing)!r}")
        if unknown:
            details.append(f"unknown {sorted(unknown)!r}")
        raise EnvelopeValidationError(f"{field} has invalid keys ({'; '.join(details)})")


def _parse_timestamp(value: Any, field: str) -> _datetime.datetime:
    text = _non_empty_string(value, field)
    try:
        parsed = _datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise EnvelopeValidationError(f"{field} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise EnvelopeValidationError(f"{field} must include a timezone")
    return parsed.astimezone(_datetime.timezone.utc)


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise EnvelopeValidationError("envelope contains non-JSON data") from exc


def _outcome_status(outcome: Any) -> str | None:
    if isinstance(outcome, str):
        return outcome.upper()
    if isinstance(outcome, Mapping):
        status = outcome.get("status")
        return status.upper() if isinstance(status, str) else None
    return None


def validate_header(header: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and return a detached copy of an envelope header."""
    if not isinstance(header, Mapping):
        raise EnvelopeValidationError("header must be an object")
    _validate_exact_keys(header, _HEADER_KEYS, "header")
    version = header["schema_version"]
    if isinstance(version, bool) or not isinstance(version, int) or version != SCHEMA_VERSION:
        raise EnvelopeValidationError(
            f"unsupported schema_version {version!r}; expected {SCHEMA_VERSION}"
        )
    envelope_id = _non_empty_string(header["envelope_id"], "envelope_id")
    subject = header["subject"]
    if not isinstance(subject, Mapping):
        raise EnvelopeValidationError("subject must be an object")
    _validate_exact_keys(subject, _SUBJECT_KEYS, "subject")
    candidate_id = _non_empty_string(subject["candidate_id"], "subject.candidate_id")
    candidate_hash = _non_empty_string(subject["candidate_hash"], "subject.candidate_hash")
    refs = header["contract_refs"]
    if not isinstance(refs, list) or not refs:
        raise EnvelopeValidationError("contract_refs must be a non-empty array")
    for index, ref in enumerate(refs):
        _non_empty_string(ref, f"contract_refs[{index}]")
    result = {
        "schema_version": version,
        "envelope_id": envelope_id,
        "subject": {"candidate_id": candidate_id, "candidate_hash": candidate_hash},
        "contract_refs": list(refs),
    }
    _canonical_json(result)
    return copy.deepcopy(result)


def validate_event(event: Mapping[str, Any]) -> dict[str, Any]:
    """Validate one event without checking relations to sibling events."""
    if not isinstance(event, Mapping):
        raise EnvelopeValidationError("event must be an object")
    keys = set(event)
    missing = _EVENT_REQUIRED_KEYS - keys
    unknown = keys - _EVENT_KEYS
    if missing or unknown:
        details = []
        if missing:
            details.append(f"missing {sorted(missing)!r}")
        if unknown:
            details.append(f"unknown {sorted(unknown)!r}")
        raise EnvelopeValidationError(f"event has invalid keys ({'; '.join(details)})")

    result = dict(event)
    _non_empty_string(result["event_id"], "event_id")
    _parse_timestamp(result["occurred_at"], "occurred_at")
    producer = result["producer"]
    if not isinstance(producer, Mapping):
        raise EnvelopeValidationError("producer must be an object")
    _validate_exact_keys(producer, _PRODUCER_KEYS, "producer")
    _non_empty_string(producer["domain"], "producer.domain")
    _non_empty_string(producer["actor"], "producer.actor")
    _non_empty_string(result["kind"], "kind")

    outcome = result["outcome"]
    if isinstance(outcome, str):
        _non_empty_string(outcome, "outcome")
    elif isinstance(outcome, Mapping):
        outcome_keys = set(outcome)
        if "status" not in outcome_keys or outcome_keys - _OUTCOME_KEYS:
            details = []
            if "status" not in outcome_keys:
                details.append("missing ['status']")
            unknown_outcome_keys = outcome_keys - _OUTCOME_KEYS
            if unknown_outcome_keys:
                details.append(f"unknown {sorted(unknown_outcome_keys)!r}")
            raise EnvelopeValidationError(
                f"outcome has invalid keys ({'; '.join(details)})"
            )
        _non_empty_string(outcome["status"], "outcome.status")
        if "state" in outcome:
            state = _non_empty_string(outcome["state"], "outcome.state")
            if state not in CANDIDATE_STATES:
                raise EnvelopeValidationError(f"unknown candidate state {state!r}")
            if state == "live_with_active_waiver":
                raise EnvelopeValidationError(
                    "live_with_active_waiver is derived from a waiver event"
                )
    else:
        raise EnvelopeValidationError("outcome must be a string or object")

    evidence_refs = result["evidence_refs"]
    if not isinstance(evidence_refs, list):
        raise EnvelopeValidationError("evidence_refs must be an array")
    for index, ref in enumerate(evidence_refs):
        _non_empty_string(ref, f"evidence_refs[{index}]")
    if not isinstance(result["payload"], Mapping):
        raise EnvelopeValidationError("payload must be a domain-owned object")

    for key in ("attempt_id", "relates_to_event_id"):
        if key in result:
            _non_empty_string(result[key], key)
    if result["kind"] == WAIVER_KIND:
        if "relates_to_event_id" not in result:
            raise EnvelopeValidationError("waiver events must relate to an event")
        payload_keys = set(result["payload"])
        missing_waiver = _WAIVER_REQUIRED_PAYLOAD_KEYS - payload_keys
        if missing_waiver:
            raise EnvelopeValidationError(
                f"waiver payload missing {sorted(missing_waiver)!r}"
            )
        _non_empty_string(result["payload"]["decision_maker"], "payload.decision_maker")
        _non_empty_string(result["payload"]["reason"], "payload.reason")
        scope = result["payload"]["scope"]
        if not isinstance(scope, (str, Mapping, list)) or (
            (isinstance(scope, str) and not scope.strip())
            or (isinstance(scope, (Mapping, list)) and not scope)
        ):
            raise EnvelopeValidationError("payload.scope must be a bounded non-empty value")
        _parse_timestamp(result["payload"]["expires_at"], "payload.expires_at")

    result["producer"] = dict(producer)
    result["evidence_refs"] = list(evidence_refs)
    result["payload"] = copy.deepcopy(dict(result["payload"]))
    if isinstance(outcome, Mapping):
        result["outcome"] = dict(outcome)
    _canonical_json(result)
    return copy.deepcopy(result)


def _validate_event_stream(events: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    validated: list[dict[str, Any]] = []
    by_id: dict[str, dict[str, Any]] = {}
    for event in events:
        item = validate_event(event)
        event_id = item["event_id"]
        if event_id in by_id:
            raise EnvelopeValidationError(f"duplicate event_id {event_id!r}")
        by_id[event_id] = item
        validated.append(item)

    for event in validated:
        if event["kind"] != WAIVER_KIND:
            continue
        target_id = event["relates_to_event_id"]
        target = by_id.get(target_id)
        if target is None:
            raise EnvelopeValidationError(
                f"waiver {event['event_id']!r} targets missing event {target_id!r}"
            )
        if _outcome_status(target["outcome"]) not in NON_PASS_OUTCOMES:
            raise EnvelopeValidationError(
                f"waiver {event['event_id']!r} must target FAIL or NOT_VERIFIED"
            )
    return validated


def _event_state(event: Mapping[str, Any]) -> str | None:
    outcome = event["outcome"]
    if isinstance(outcome, Mapping):
        state = outcome.get("state")
        return state if isinstance(state, str) and state in CANDIDATE_STATES else None
    return None


def _ordered_events(events: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    # The file retains append order.  Replay uses an explicit chronological
    # key so the same event set projects identically even if supplied in a
    # different order; event_id breaks equal-timestamp ties.
    return sorted(
        (copy.deepcopy(dict(event)) for event in events),
        key=lambda event: (
            _parse_timestamp(event["occurred_at"], "occurred_at"),
            event["event_id"],
        ),
    )


def project_events(
    header: Mapping[str, Any],
    events: Sequence[Mapping[str, Any]],
    *,
    as_of: str | _datetime.datetime | None = None,
) -> dict[str, Any]:
    """Replay events into a deterministic, non-persisted candidate projection.

    ``as_of`` defaults to the latest event timestamp rather than wall-clock
    time, keeping repeated replays deterministic.  A waiver is active only
    before its ``expires_at`` timestamp and is always included in ``waivers``
    with its active flag.  A non-pass event after a state transition blocks the
    candidate unless an explicit later state transition supersedes it; a
    waiver never turns a candidate into plain ``live_verified``.
    """
    validated_header = validate_header(header)
    validated_events = _validate_event_stream(events)
    ordered = _ordered_events(validated_events)
    if as_of is None:
        replay_as_of = (
            _parse_timestamp(ordered[-1]["occurred_at"], "occurred_at")
            if ordered
            else None
        )
    else:
        replay_as_of = _parse_timestamp(as_of, "as_of") if isinstance(as_of, str) else as_of
        if not isinstance(replay_as_of, _datetime.datetime) or replay_as_of.tzinfo is None:
            raise EnvelopeValidationError("as_of must be a timezone-aware datetime or ISO string")
        replay_as_of = replay_as_of.astimezone(_datetime.timezone.utc)

    visible = [
        event
        for event in ordered
        if replay_as_of is None
        or _parse_timestamp(event["occurred_at"], "occurred_at") <= replay_as_of
    ]
    state = "collecting"
    state_event_id: str | None = None
    state_key: tuple[_datetime.datetime, str] | None = None
    latest_non_pass_key: tuple[_datetime.datetime, str] | None = None
    non_pass_events: list[dict[str, Any]] = []
    waivers: list[dict[str, Any]] = []

    for event in visible:
        event_key = (_parse_timestamp(event["occurred_at"], "occurred_at"), event["event_id"])
        outcome_status = _outcome_status(event["outcome"])
        if outcome_status in NON_PASS_OUTCOMES:
            latest_non_pass_key = event_key
            non_pass_events.append(copy.deepcopy(event))
        candidate_state = _event_state(event)
        if candidate_state and outcome_status == PASS_OUTCOME:
            state = candidate_state
            state_event_id = event["event_id"]
            state_key = event_key
        if event["kind"] == WAIVER_KIND:
            expires_at = _parse_timestamp(event["payload"]["expires_at"], "payload.expires_at")
            waivers.append(
                {
                    "event_id": event["event_id"],
                    "relates_to_event_id": event["relates_to_event_id"],
                    "decision_maker": event["payload"]["decision_maker"],
                    "reason": event["payload"]["reason"],
                    "scope": copy.deepcopy(event["payload"]["scope"]),
                    "expires_at": event["payload"]["expires_at"],
                    "active": replay_as_of is None or expires_at > replay_as_of,
                }
            )

    active_waivers = [waiver for waiver in waivers if waiver["active"]]
    effective_state = state
    if (
        latest_non_pass_key is not None
        and (state_key is None or latest_non_pass_key > state_key)
        and state != "rejected"
    ):
        effective_state = "blocked"
    if active_waivers and state == "live_verified":
        effective_state = "live_with_active_waiver"

    return {
        "schema_version": validated_header["schema_version"],
        "envelope_id": validated_header["envelope_id"],
        "subject": copy.deepcopy(validated_header["subject"]),
        "status": effective_state,
        "candidate_state": effective_state,
        "base_state": state,
        "state_event_id": state_event_id,
        "as_of": replay_as_of.isoformat().replace("+00:00", "Z")
        if replay_as_of is not None
        else None,
        "event_ids": [event["event_id"] for event in visible],
        "non_pass_events": non_pass_events,
        "waivers": waivers,
        "active_waivers": active_waivers,
    }


def _safe_envelope_id(envelope_id: str) -> str:
    envelope_id = _non_empty_string(envelope_id, "envelope_id")
    if (
        Path(envelope_id).name != envelope_id
        or "/" in envelope_id
        or "\\" in envelope_id
        or envelope_id in {".", ".."}
    ):
        raise EnvelopeValidationError("envelope_id must be a single path-safe component")
    return envelope_id


def _fsync_directory(path: Path) -> None:
    if os.name != "posix":  # pragma: no cover - Windows has no directory fsync.
        return
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


@contextmanager
def _exclusive_lock(lock_path: Path) -> Iterator[None]:
    """Acquire a mandatory-for-this-module cross-process lock or fail closed."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    if lock_path.is_symlink():
        raise EnvelopeLockError(f"envelope lock must not be a symlink: {lock_path}")
    try:
        handle = open(lock_path, "a+b")
    except OSError as exc:
        raise EnvelopeLockError(f"could not open envelope lock {lock_path}") from exc
    acquired = False
    try:
        try:
            if _fcntl is not None:
                _fcntl.flock(handle.fileno(), _fcntl.LOCK_EX)
                acquired = True
            elif _msvcrt is not None:  # pragma: no cover - Windows only.
                handle.seek(0, os.SEEK_END)
                if handle.tell() == 0:
                    handle.write(b"0")
                    handle.flush()
                handle.seek(0)
                getattr(_msvcrt, "locking")(
                    handle.fileno(), getattr(_msvcrt, "LK_LOCK"), 1
                )
                acquired = True
            else:  # pragma: no cover - supported runtimes have one backend.
                raise EnvelopeLockError("no supported cross-process lock backend")
        except OSError as exc:
            raise EnvelopeLockError(f"could not acquire envelope lock {lock_path}") from exc
        yield
    finally:
        if acquired:
            try:
                if _fcntl is not None:
                    _fcntl.flock(handle.fileno(), _fcntl.LOCK_UN)
                elif _msvcrt is not None:  # pragma: no cover - Windows only.
                    handle.seek(0)
                    getattr(_msvcrt, "locking")(
                        handle.fileno(), getattr(_msvcrt, "LK_UNLCK"), 1
                    )
            finally:
                handle.close()
        else:
            handle.close()


class OutcomeEnvelopeStore:
    """Profile-local append-only JSONL outcome-envelope store."""

    def __init__(self, root: str | os.PathLike[str] | None = None):
        self.root = (
            Path(root).expanduser()
            if root is not None
            else Path(get_hermes_home()) / OUTCOME_ENVELOPE_DIRNAME
        )

    def path_for(self, envelope_id: str) -> Path:
        return self.root / f"{_safe_envelope_id(envelope_id)}.jsonl"

    def _lock_for(self, envelope_id: str) -> Path:
        _safe_envelope_id(envelope_id)
        return self.root / f".{envelope_id}.lock"

    def _read_unlocked(self, envelope_id: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        path = self.path_for(envelope_id)
        if path.is_symlink():
            raise EnvelopeValidationError(f"envelope must not be a symlink: {path}")
        try:
            raw = path.read_bytes()
        except FileNotFoundError as exc:
            raise EnvelopeNotFoundError(f"envelope {envelope_id!r} does not exist") from exc
        except OSError as exc:
            raise OutcomeEnvelopeError(f"could not read envelope {path}") from exc
        if not raw or not raw.endswith(b"\n"):
            raise EnvelopeValidationError(f"envelope {path} is truncated or empty")
        try:
            lines = raw.decode("utf-8").splitlines()
            records = [json.loads(line) for line in lines]
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise EnvelopeValidationError(f"envelope {path} contains invalid JSONL") from exc
        if not records or any(not isinstance(record, Mapping) for record in records):
            raise EnvelopeValidationError(f"envelope {path} contains invalid records")
        header = validate_header(records[0])
        if header["envelope_id"] != envelope_id:
            raise EnvelopeValidationError(
                f"file name {envelope_id!r} disagrees with header {header['envelope_id']!r}"
            )
        events = _validate_event_stream(records[1:])
        return header, events

    def read(self, envelope_id: str) -> dict[str, Any]:
        """Read and validate an envelope while holding its cross-process lock."""
        with _exclusive_lock(self._lock_for(envelope_id)):
            header, events = self._read_unlocked(envelope_id)
        return {"header": header, "events": events}

    def create(
        self,
        envelope_id: str,
        subject: Mapping[str, Any],
        contract_refs: Sequence[str],
        *,
        schema_version: int = SCHEMA_VERSION,
    ) -> dict[str, Any]:
        """Create an envelope, or idempotently return an identical header."""
        header = validate_header(
            {
                "schema_version": schema_version,
                "envelope_id": envelope_id,
                "subject": dict(subject),
                "contract_refs": list(contract_refs),
            }
        )
        path = self.path_for(envelope_id)
        with _exclusive_lock(self._lock_for(envelope_id)):
            if path.is_symlink():
                raise EnvelopeValidationError(f"envelope must not be a symlink: {path}")
            if path.exists():
                existing, _events = self._read_unlocked(envelope_id)
                if existing != header:
                    raise EnvelopeConflictError(
                        f"envelope {envelope_id!r} already exists with a different header"
                    )
                return copy.deepcopy(existing)
            self.root.mkdir(parents=True, exist_ok=True)
            try:
                with open(path, "xb") as handle:
                    handle.write((_canonical_json(header) + "\n").encode("utf-8"))
                    handle.flush()
                    os.fsync(handle.fileno())
            except FileExistsError:
                # The lock should make this unreachable locally, but preserve
                # fail-closed idempotency if a non-cooperating writer races us.
                existing, _events = self._read_unlocked(envelope_id)
                if existing != header:
                    raise EnvelopeConflictError(
                        f"envelope {envelope_id!r} already exists with a different header"
                    )
                return copy.deepcopy(existing)
            _fsync_directory(self.root)
        return copy.deepcopy(header)

    def append_event(self, envelope_id: str, event: Mapping[str, Any]) -> bool:
        """Append an event and return ``False`` for an exact logical duplicate.

        Reusing an existing ``event_id`` with different content raises
        :class:`EnvelopeConflictError`.  No rewrite or truncation path exists.
        """
        validated = validate_event(event)
        path = self.path_for(envelope_id)
        with _exclusive_lock(self._lock_for(envelope_id)):
            header, events = self._read_unlocked(envelope_id)
            del header  # Header validation above is part of the fail-closed read.
            for existing in events:
                if existing["event_id"] != validated["event_id"]:
                    continue
                if existing == validated:
                    return False
                raise EnvelopeConflictError(
                    f"event_id {validated['event_id']!r} already has different content"
                )
            if validated["kind"] == WAIVER_KIND:
                _validate_event_stream([*events, validated])
            with open(path, "ab") as handle:
                handle.write((_canonical_json(validated) + "\n").encode("utf-8"))
                handle.flush()
                os.fsync(handle.fileno())
        return True

    def project(
        self,
        envelope_id: str,
        *,
        as_of: str | _datetime.datetime | None = None,
    ) -> dict[str, Any]:
        """Read and replay an envelope without persisting aggregate status."""
        envelope = self.read(envelope_id)
        return project_events(envelope["header"], envelope["events"], as_of=as_of)

    # ``replay`` is a semantic alias useful to callers that care about the
    # operation rather than its returned projection type.
    replay = project


def create_envelope(
    envelope_id: str,
    subject: Mapping[str, Any],
    contract_refs: Sequence[str],
    *,
    root: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """Convenience wrapper using the current profile's local store."""
    return OutcomeEnvelopeStore(root).create(envelope_id, subject, contract_refs)


def append_event(
    envelope_id: str,
    event: Mapping[str, Any],
    *,
    root: str | os.PathLike[str] | None = None,
) -> bool:
    """Convenience wrapper for :meth:`OutcomeEnvelopeStore.append_event`."""
    return OutcomeEnvelopeStore(root).append_event(envelope_id, event)


def replay_envelope(
    envelope_id: str,
    *,
    root: str | os.PathLike[str] | None = None,
    as_of: str | _datetime.datetime | None = None,
) -> dict[str, Any]:
    """Convenience wrapper for deterministic replay."""
    return OutcomeEnvelopeStore(root).project(envelope_id, as_of=as_of)


__all__ = [
    "CANDIDATE_NON_TERMINAL_STATES",
    "CANDIDATE_STATES",
    "CANDIDATE_TERMINAL_STATES",
    "EnvelopeConflictError",
    "EnvelopeLockError",
    "EnvelopeNotFoundError",
    "EnvelopeValidationError",
    "NON_PASS_OUTCOMES",
    "OutcomeEnvelopeError",
    "OutcomeEnvelopeStore",
    "SCHEMA_VERSION",
    "append_event",
    "create_envelope",
    "project_events",
    "replay_envelope",
    "validate_event",
    "validate_header",
]
