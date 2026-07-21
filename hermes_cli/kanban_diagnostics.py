"""Kanban diagnostics — structured, actionable distress signals for tasks.

A ``Diagnostic`` is a machine-readable description of something that's wrong
with a kanban task: a hallucinated card id, a spawn crash-loop, a task
stuck blocked for too long, etc. Each one carries:

* A **kind** (canonical code; UI/tests match on this).
* A **severity** (``warning`` / ``error`` / ``critical``).
* A **title** (one-line human description) and **detail** (longer text).
* A list of **suggested actions** — structured entries the dashboard
  turns into buttons and the CLI turns into hints.

Rules run over (task, recent events, recent runs) and emit diagnostics.
They are stateless and read-only — no DB writes. Callers compute
diagnostics on demand (on ``/board`` load, ``/tasks/:id`` fetch, or
``hermes kanban diagnostics``).

Design goals:

* Fixable-on-the-operator's-side signals only (missing config, phantom
  ids, crash loop). Not "the provider returned 502 once" — that's a
  transient runtime blip, not a diagnostic.
* Recoverable: every diagnostic comes with at least one suggested
  recovery action the operator can actually take from the UI.
* Auto-clearing: when the underlying failure mode resolves (a clean
  ``completed`` event arrives, a spawn succeeds, the task gets
  unblocked), the diagnostic stops firing. The audit event trail stays.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Optional
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time

from agent.redact import redact_for_persistence as _redact_diagnostic


# Severity rungs, ordered least → most urgent. The UI colors them
# amber (warning), orange (error), red (critical). Sorted outputs put
# critical first so operators see the worst fires at the top.
SEVERITY_ORDER = ("warning", "error", "critical")


RECONCILIATION_TERMINAL_STATES = frozenset({"merged", "landed", "archived"})
RECONCILIATION_HEAD_STATES = frozenset({
    "head_current", "head_superseded", "branch_missing", "branch_unknown",
})
_FULL_SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")
_REF_FORBIDDEN_CHARS = frozenset(" ~^:?*[\\")
DEFAULT_GIT_PROBE_MAX_COMMANDS = 12
DEFAULT_GIT_PROBE_DEADLINE_SECONDS = 6.0
DEFAULT_GIT_PROBE_COMMAND_TIMEOUT_SECONDS = 2.0


def severity_at_or_above(severity: Optional[str], threshold: Optional[str]) -> bool:
    """Return True when ``severity`` meets or exceeds ``threshold``."""
    if threshold is None:
        return True
    if severity not in SEVERITY_ORDER or threshold not in SEVERITY_ORDER:
        return False
    return SEVERITY_ORDER.index(severity) >= SEVERITY_ORDER.index(threshold)


@dataclass
class DiagnosticAction:
    """A single recovery action attached to a diagnostic.

    The ``kind`` determines how both the UI and CLI render it:

    * ``reclaim`` / ``reassign`` — POST to the matching /tasks/:id/*
      endpoint; dashboard wires into the existing recovery popover.
    * ``unblock`` — PATCH status back to ``ready`` (for stuck-blocked
      diagnostics).
    * ``cli_hint`` — print/copy a shell command (e.g.
      ``hermes -p <profile> auth``). No HTTP side effect.
    * ``open_docs`` — deep-link to the docs URL named in ``payload.url``.
    * ``comment`` — nudge the operator to add a comment (for
      stuck-blocked tasks that need human input).

    ``suggested=True`` marks the action as the recommended first step;
    the UI highlights it. Multiple actions can be suggested if they're
    equally valid.
    """

    kind: str
    label: str
    payload: dict = field(default_factory=dict)
    suggested: bool = False

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "label": _redact_diagnostic(self.label),
            "payload": _redact_diagnostic(self.payload),
            "suggested": self.suggested,
        }


@dataclass
class Diagnostic:
    """One active distress signal on a task."""

    kind: str
    severity: str  # "warning" | "error" | "critical"
    title: str
    detail: str
    actions: list[DiagnosticAction] = field(default_factory=list)
    first_seen_at: int = 0
    last_seen_at: int = 0
    count: int = 1
    # Optional: the run id this diagnostic is scoped to. None = task-wide.
    run_id: Optional[int] = None
    # Optional structured payload for the UI (phantom ids, failure count).
    data: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "severity": self.severity,
            "title": _redact_diagnostic(self.title),
            "detail": _redact_diagnostic(self.detail),
            "actions": [a.to_dict() for a in self.actions],
            "first_seen_at": self.first_seen_at,
            "last_seen_at": self.last_seen_at,
            "count": self.count,
            "run_id": self.run_id,
            "data": _redact_diagnostic(self.data),
        }


# ---------------------------------------------------------------------------
# Rule helpers
# ---------------------------------------------------------------------------

def _task_field(task, name, default=None):
    """Read a field from a task regardless of representation.

    Callers pass sqlite3.Row (dict-like with [] but no attribute
    access), kanban_db.Task dataclasses (attribute access), or plain
    dicts (both). This normalises them so rule functions don't have
    to branch on type each time.
    """
    if task is None:
        return default
    # sqlite Row + plain dicts both support mapping access; Row also
    # supports .keys().
    try:
        # Row raises IndexError if the key isn't a column in the query;
        # dicts return default via .get. Handle both.
        if hasattr(task, "keys") and name in task.keys():
            return task[name]
    except Exception:
        pass
    if isinstance(task, dict):
        return task.get(name, default)
    return getattr(task, name, default)


def _parse_payload(ev) -> dict:
    """Tolerate event.payload being either a dict or a JSON string."""
    p = _task_field(ev, "payload", None)
    if p is None:
        return {}
    if isinstance(p, dict):
        return p
    if isinstance(p, str):
        try:
            return json.loads(p) or {}
        except Exception:
            return {}
    return {}


def _event_kind(ev) -> str:
    return _task_field(ev, "kind", "") or ""


def _event_ts(ev) -> int:
    t = _task_field(ev, "created_at", 0)
    return int(t or 0)


def _active_hallucination_events(
    events: Iterable[Any],
    kind: str,
) -> list[Any]:
    """Return events of ``kind`` that have no ``completed``/``edited``
    event *strictly after* them. Walks chronologically: each clean
    event resets the accumulator; each matching event gets appended.

    Events must be sorted by id (i.e. arrival order); callers pass the
    task's full event list which the DB already returns in that order.
    """
    # Events arrive sorted by id asc (chronological). Walk once, track
    # which hallucination events are still "active" (no clean event
    # supersedes them).
    active: list[Any] = []
    for ev in events:
        k = _event_kind(ev)
        if k in {"completed", "edited"}:
            active.clear()
        elif k == kind:
            active.append(ev)
    return active
# Standard always-available actions. Every diagnostic can offer these as
# fallbacks regardless of kind — they're the two baseline recovery
# primitives the kernel supports.
def _generic_recovery_actions(task: Any, *, running: bool) -> list[DiagnosticAction]:
    out: list[DiagnosticAction] = []
    if running:
        out.append(DiagnosticAction(
            kind="reclaim",
            label="Reclaim task",
            payload={},
        ))
    out.append(DiagnosticAction(
        kind="reassign",
        label="Reassign to different profile",
        payload={"reclaim_first": running},
    ))
    return out


# ---------------------------------------------------------------------------
# Cross-task graph / chain diagnostics
# ---------------------------------------------------------------------------

_REVIEW_REQUIRED_PREFIX = "review-required:"
_TERMINAL_STATUSES = {"done", "archived", "failed", "cancelled"}
_RELEASE_PARENT_STATUSES = {"done", "archived"}
_RELEASED_CHILD_STATUSES = {"ready", "running"}
_NEGATIVE_VERDICTS = {"changes_requested", "rejected", "failed"}


def _task_map(tasks: Iterable[Any] | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(tasks, Mapping):
        items = tasks.items()
    else:
        items = ((None, task) for task in tasks)
    out: dict[str, Any] = {}
    for key, task in items:
        task_id = _task_field(task, "id", key)
        if task_id:
            out[str(task_id)] = task
    return out


def _data_by_task(data: Any, task_id: str) -> list[Any]:
    """Read either a task-id mapping or a flat collection of rows."""
    if isinstance(data, Mapping):
        return list(data.get(task_id, []) or [])
    return [item for item in (data or [])
            if _task_field(item, "task_id") == task_id]


def _link_ids(link: Any) -> tuple[Optional[str], Optional[str]]:
    if isinstance(link, Mapping) or hasattr(link, "keys"):
        return (
            _task_field(link, "parent_id"),
            _task_field(link, "child_id"),
        )
    try:
        return str(link[0]), str(link[1])
    except (IndexError, TypeError, KeyError):
        return None, None


def _latest_event(events: list[Any], kinds: set[str]) -> Optional[Any]:
    candidates = [ev for ev in events if _event_kind(ev) in kinds]
    if not candidates:
        return None
    # ``id`` is the canonical tie-breaker for DB rows. The input position
    # keeps plain test fixtures deterministic when they have no id field.
    return max(
        enumerate(candidates),
        key=lambda pair: (
            _event_ts(pair[1]),
            int(_task_field(pair[1], "id", 0) or 0),
            pair[0],
        ),
    )[1]


def _review_required_reason(events: list[Any]) -> Optional[str]:
    latest = _latest_event(events, {"blocked", "unblocked"})
    if latest is None or _event_kind(latest) != "blocked":
        return None
    payload = _parse_payload(latest)
    reason = payload.get("reason", payload.get("block_reason"))
    if not isinstance(reason, str):
        return None
    if reason.strip().lower().startswith(_REVIEW_REQUIRED_PREFIX):
        return reason
    return None


def _latest_run(runs: list[Any]) -> Optional[Any]:
    if not runs:
        return None
    return max(
        enumerate(runs),
        key=lambda pair: (
            int(_task_field(pair[1], "started_at", 0) or 0),
            int(_task_field(pair[1], "ended_at", 0) or 0),
            int(_task_field(pair[1], "id", 0) or 0),
            pair[0],
        ),
    )[1]


@dataclass(frozen=True)
class GitProbeResult:
    """A bounded observation of one exact repository ref."""

    state: str
    current_head: Optional[str] = None
    canonical_ref: Optional[str] = None
    repo_root: Optional[str] = None
    common_dir: Optional[str] = None
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class WorkspaceProbeResult:
    """Repository identity for one declared checkout path."""

    state: str
    path: str
    head: Optional[str] = None
    root: Optional[str] = None
    git_dir: Optional[str] = None
    common_dir: Optional[str] = None
    remote: Optional[str] = None
    linked_worktree: bool = False
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass
class ReconciliationResult:
    """Read-only receipt consumed by diagnostics, dispatch, and the ledger."""

    task_id: str
    findings: list[Diagnostic] = field(default_factory=list)
    head_state: Optional[str] = None
    candidate: dict[str, Any] = field(default_factory=dict)
    review: dict[str, Any] = field(default_factory=dict)
    replacement: dict[str, Any] = field(default_factory=dict)
    receipt_run_id: Optional[int] = None
    db_fingerprint: Optional[str] = None
    data_version: Optional[int] = None
    snapshot_stable: bool = True
    opted_in: bool = False
    last_verified_at: int = 0

    @property
    def actionable(self) -> bool:
        return not any(d.data.get("dispatch_blocked") is True for d in self.findings)

    @property
    def suppressed(self) -> bool:
        return any(d.kind == "replacement_suppressed" for d in self.findings)

    def to_dict(self) -> dict[str, Any]:
        return _redact_diagnostic({
            "task_id": self.task_id,
            "head_state": self.head_state,
            "candidate": self.candidate,
            "review": self.review,
            "replacement": self.replacement,
            "receipt_run_id": self.receipt_run_id,
            "db_fingerprint": self.db_fingerprint,
            "data_version": self.data_version,
            "snapshot_stable": self.snapshot_stable,
            "opted_in": self.opted_in,
            "actionable": self.actionable,
            "suppressed": self.suppressed,
            "last_verified_at": self.last_verified_at,
            "summary": [d.to_dict() for d in self.findings],
        })


def _canonical_exact_ref(ref: Any) -> Optional[str]:
    """Return a full ref without ever asking Git to DWIM it."""
    raw = str(ref or "").strip()
    if not raw:
        return None
    candidate = raw if raw.startswith("refs/") else f"refs/heads/{raw}"
    if (
        candidate.endswith(("/", "."))
        or candidate.startswith("/")
        or "//" in candidate
        or ".." in candidate
        or "@{" in candidate
        or any(ch in _REF_FORBIDDEN_CHARS or ord(ch) < 32 or ord(ch) == 127
               for ch in candidate)
    ):
        return None
    parts = candidate.split("/")
    if len(parts) < 3 or any(
        not part or part.startswith(".") or part.endswith(".lock")
        for part in parts
    ):
        return None
    return candidate


def _normalize_repo_identity(value: Any) -> str:
    text = str(value or "").strip().rstrip("/")
    if text.startswith("file://"):
        text = text[7:]
    if text in {".", ".."} or text.startswith(("/", "./", "../", "~")):
        try:
            return str(Path(text).expanduser().resolve(strict=False))
        except (OSError, RuntimeError, ValueError):
            return text
    return text


def _looks_like_local_repo(value: str) -> bool:
    return (
        value.startswith("file://")
        or value.startswith(("/", "./", "../", "~"))
        or Path(os.path.expanduser(value)).exists()
    )


def _default_git_runner(args: list[str], timeout: float) -> tuple[int, str, str]:
    try:
        completed = subprocess.run(
            ["git", *args],
            capture_output=True,
            text=True,
            timeout=max(0.01, timeout),
            check=False,
        )
    except subprocess.TimeoutExpired:
        return 124, "", "git probe timed out"
    except OSError as exc:
        return 125, "", str(exc)
    return completed.returncode, completed.stdout.strip(), completed.stderr.strip()


class GitProbeSession:
    """Request-scoped exact-ref/worktree cache with a hard total budget."""

    def __init__(
        self,
        *,
        runner: Optional[Callable[[list[str], float], tuple[int, str, str]]] = None,
        max_commands: int = DEFAULT_GIT_PROBE_MAX_COMMANDS,
        deadline_seconds: float = DEFAULT_GIT_PROBE_DEADLINE_SECONDS,
        command_timeout: float = DEFAULT_GIT_PROBE_COMMAND_TIMEOUT_SECONDS,
    ) -> None:
        self._runner = runner or _default_git_runner
        self.max_commands = max(1, int(max_commands))
        self.command_timeout = max(0.01, float(command_timeout))
        self._deadline = time.monotonic() + max(0.01, float(deadline_seconds))
        self.commands_used = 0
        self._ref_cache: dict[tuple[str, str], GitProbeResult] = {}
        self._repo_cache: dict[str, GitProbeResult] = {}
        self._workspace_cache: dict[str, WorkspaceProbeResult] = {}

    def _run(self, args: list[str]) -> tuple[int, str, str, Optional[str]]:
        remaining = self._deadline - time.monotonic()
        if self.commands_used >= self.max_commands or remaining <= 0:
            return 124, "", "git probe budget exhausted", "probe_budget_exhausted"
        self.commands_used += 1
        timeout = min(self.command_timeout, remaining)
        try:
            code, out, err = self._runner(args, timeout)
        except (OSError, subprocess.SubprocessError, TimeoutError) as exc:
            return 125, "", str(exc), "probe_error"
        if code == 124:
            return code, out, err, "probe_timeout"
        if code == 125:
            return code, out, err, "probe_error"
        return int(code), str(out or "").strip(), str(err or "").strip(), None

    def _local_repo(self, repo: str) -> GitProbeResult:
        local = _normalize_repo_identity(repo)
        cached = self._repo_cache.get(local)
        if cached is not None:
            return cached
        path = Path(local)
        if not path.is_dir():
            result = GitProbeResult(
                "branch_unknown",
                evidence={"reason": "repo_missing", "repo": repo},
            )
            self._repo_cache[local] = result
            return result
        code, out, err, reason = self._run([
            "-C", str(path), "rev-parse", "--is-bare-repository",
            "--absolute-git-dir", "--path-format=absolute", "--git-common-dir",
        ])
        lines = out.splitlines()
        if code != 0 or len(lines) < 3:
            result = GitProbeResult(
                "branch_unknown",
                evidence={
                    "reason": reason or "repo_unreadable", "repo": repo, "error": err,
                },
            )
            self._repo_cache[local] = result
            return result
        bare = lines[0].strip().lower() == "true"
        git_dir = _normalize_repo_identity(lines[1])
        common_dir = _normalize_repo_identity(lines[2])
        root = local if bare else (
            _normalize_repo_identity(Path(git_dir).parent)
            if git_dir == common_dir else local
        )
        result = GitProbeResult(
            "branch_unknown",
            repo_root=root,
            common_dir=common_dir,
            evidence={"repo": repo, "bare": bare, "git_dir": git_dir},
        )
        self._repo_cache[local] = result
        return result

    def observe_ref(self, repo: str, ref: str) -> GitProbeResult:
        canonical_ref = _canonical_exact_ref(ref)
        normalized_repo = _normalize_repo_identity(repo)
        if canonical_ref is None:
            return GitProbeResult(
                "branch_unknown",
                evidence={"reason": "invalid_exact_ref", "repo": repo, "ref": ref},
            )
        key = (normalized_repo, canonical_ref)
        cached = self._ref_cache.get(key)
        if cached is not None:
            return cached

        if _looks_like_local_repo(str(repo)):
            identity = self._local_repo(str(repo))
            if identity.evidence.get("reason"):
                result = GitProbeResult(
                    "branch_unknown",
                    canonical_ref=canonical_ref,
                    repo_root=identity.repo_root,
                    common_dir=identity.common_dir,
                    evidence=dict(identity.evidence),
                )
            else:
                local = _normalize_repo_identity(repo)
                code, out, err, reason = self._run([
                    "-C", local, "rev-parse", "--verify", "--quiet",
                    f"{canonical_ref}^{{commit}}",
                ])
                if code == 0 and _FULL_SHA_RE.fullmatch(out):
                    result = GitProbeResult(
                        "branch_unknown",
                        current_head=out.lower(),
                        canonical_ref=canonical_ref,
                        repo_root=identity.repo_root,
                        common_dir=identity.common_dir,
                        evidence={**identity.evidence, "repo": repo, "ref": canonical_ref},
                    )
                elif code == 1:
                    result = GitProbeResult(
                        "branch_missing",
                        canonical_ref=canonical_ref,
                        repo_root=identity.repo_root,
                        common_dir=identity.common_dir,
                        evidence={"repo": repo, "ref": canonical_ref},
                    )
                else:
                    result = GitProbeResult(
                        "branch_unknown",
                        canonical_ref=canonical_ref,
                        repo_root=identity.repo_root,
                        common_dir=identity.common_dir,
                        evidence={
                            "reason": reason or "ref_probe_failed", "repo": repo,
                            "ref": canonical_ref, "error": err,
                        },
                    )
        else:
            code, out, err, reason = self._run([
                "ls-remote", "--exit-code", "--refs", str(repo), canonical_ref,
            ])
            rows = [line.split() for line in out.splitlines() if line.split()]
            exact = [row for row in rows if len(row) >= 2 and row[1] == canonical_ref]
            if code == 0 and len(exact) == 1 and _FULL_SHA_RE.fullmatch(exact[0][0]):
                result = GitProbeResult(
                    "branch_unknown",
                    current_head=exact[0][0].lower(),
                    canonical_ref=canonical_ref,
                    evidence={"repo": repo, "ref": canonical_ref},
                )
            elif code == 2 or (code == 0 and not exact):
                result = GitProbeResult(
                    "branch_missing",
                    canonical_ref=canonical_ref,
                    evidence={"repo": repo, "ref": canonical_ref},
                )
            else:
                result = GitProbeResult(
                    "branch_unknown",
                    canonical_ref=canonical_ref,
                    evidence={
                        "reason": reason or "remote_probe_failed", "repo": repo,
                        "ref": canonical_ref, "error": err,
                    },
                )
        self._ref_cache[key] = result
        return result

    def __call__(
        self, repo: str, ref: str, candidate_head: Optional[str] = None,
    ) -> GitProbeResult:
        observed = self.observe_ref(repo, ref)
        state = observed.state
        candidate = str(candidate_head or "").strip().lower()
        if observed.current_head:
            state = (
                "head_current"
                if _FULL_SHA_RE.fullmatch(candidate) and observed.current_head == candidate
                else "head_superseded"
            )
        return GitProbeResult(
            state,
            current_head=observed.current_head,
            canonical_ref=observed.canonical_ref,
            repo_root=observed.repo_root,
            common_dir=observed.common_dir,
            evidence=dict(observed.evidence),
        )

    def probe_workspace(self, path_value: Any) -> WorkspaceProbeResult:
        path = Path(os.path.expanduser(str(path_value or ""))).resolve(strict=False)
        key = str(path)
        cached = self._workspace_cache.get(key)
        if cached is not None:
            return cached
        if not path.is_dir():
            result = WorkspaceProbeResult(
                "workspace_missing", key, evidence={"path": key},
            )
            self._workspace_cache[key] = result
            return result
        if not (path / ".git").exists():
            result = WorkspaceProbeResult(
                "workspace_not_git", key, evidence={"path": key},
            )
            self._workspace_cache[key] = result
            return result
        code, out, err, reason = self._run([
            "-C", key, "rev-parse", "--is-bare-repository", "--absolute-git-dir",
            "--path-format=absolute", "--git-common-dir", "--show-toplevel", "HEAD",
        ])
        lines = out.splitlines()
        if code != 0 or len(lines) < 5:
            result = WorkspaceProbeResult(
                "workspace_unknown", key,
                evidence={"path": key, "reason": reason or "workspace_probe_failed", "error": err},
            )
            self._workspace_cache[key] = result
            return result
        git_dir = _normalize_repo_identity(lines[1])
        common_dir = _normalize_repo_identity(lines[2])
        root = _normalize_repo_identity(lines[3])
        head = lines[4].strip().lower()
        remote = None
        remote_code, remote_out, _remote_err, _remote_reason = self._run([
            "-C", key, "remote", "get-url", "origin",
        ])
        if remote_code == 0 and remote_out:
            remote = _normalize_repo_identity(remote_out)
        result = WorkspaceProbeResult(
            "workspace_current",
            key,
            head=head if _FULL_SHA_RE.fullmatch(head) else None,
            root=root,
            git_dir=git_dir,
            common_dir=common_dir,
            remote=remote,
            linked_worktree=git_dir != common_dir,
            evidence={"path": key},
        )
        self._workspace_cache[key] = result
        return result


def probe_git_candidate(
    repo: str, ref: str, candidate_head: Optional[str] = None,
) -> GitProbeResult:
    """Compatibility entry point for a one-off bounded exact-ref probe."""
    return GitProbeSession()(repo, ref, candidate_head)


def cached_git_probe(
    probe: Optional[Callable[..., Any]] = None,
) -> Callable[..., Any]:
    """Return one request-scoped cache; production uses :class:`GitProbeSession`."""
    if probe is None:
        return GitProbeSession()
    cache: dict[tuple[str, str], Any] = {}

    def _cached(repo: str, ref: str, candidate_head: Optional[str] = None) -> Any:
        key = (
            _normalize_repo_identity(repo),
            _canonical_exact_ref(ref) or str(ref),
        )
        if key not in cache:
            try:
                cache[key] = probe(repo, ref, candidate_head)
            except TypeError:
                cache[key] = probe(repo, ref)
        value = cache[key]
        parsed = _probe_result(value)
        candidate = str(candidate_head or "").strip().lower()
        if parsed.current_head:
            return GitProbeResult(
                "head_current"
                if _FULL_SHA_RE.fullmatch(candidate) and parsed.current_head == candidate
                else "head_superseded",
                current_head=parsed.current_head,
                canonical_ref=parsed.canonical_ref,
                repo_root=parsed.repo_root,
                common_dir=parsed.common_dir,
                evidence=dict(parsed.evidence),
            )
        return value

    return _cached


def _probe_result(value: Any) -> GitProbeResult:
    if isinstance(value, GitProbeResult):
        return value
    if isinstance(value, Mapping):
        return GitProbeResult(
            str(value.get("state") or "branch_unknown"),
            current_head=(str(value.get("current_head") or value.get("head") or "").lower() or None),
            canonical_ref=value.get("canonical_ref"),
            repo_root=value.get("repo_root"),
            common_dir=value.get("common_dir"),
            evidence=dict(value.get("evidence") or {}),
        )
    return GitProbeResult(
        "branch_unknown", evidence={"reason": "invalid_probe_result"},
    )


def _metadata_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return {}
    return dict(value) if isinstance(value, Mapping) else {}


def reconciliation_receipt(runs: Iterable[Any]) -> tuple[dict[str, Any], Optional[int]]:
    """Resolve the newest valid receipt; unrelated retries cannot erase it."""
    ordered = sorted(
        list(runs or []),
        key=lambda run: (
            int(_task_field(run, "started_at", 0) or 0),
            int(_task_field(run, "ended_at", 0) or 0),
            int(_task_field(run, "id", 0) or 0),
        ),
        reverse=True,
    )
    for run in ordered:
        metadata = _metadata_mapping(_task_field(run, "metadata"))
        receipt = metadata.get("reconciliation")
        if isinstance(receipt, Mapping) and receipt:
            run_id = _task_field(run, "id")
            return dict(receipt), int(run_id) if run_id is not None else None
    return {}, None


def reconciliation_metadata(runs: Iterable[Any]) -> dict[str, Any]:
    """Compatibility wrapper returning only the durable receipt mapping."""
    return reconciliation_receipt(runs)[0]


def _context_parts(value: Any) -> tuple[Any, list[Any]]:
    if isinstance(value, Mapping) and "task" in value:
        return value.get("task"), list(value.get("_runs") or [])
    return value, []


def reconciliation_state_fingerprint(
    task: Any,
    runs: Iterable[Any],
    *,
    tasks: Optional[Mapping[str, Any]] = None,
) -> str:
    """Hash only DB-backed claim inputs, excluding clocks and probe output."""
    task_id = str(_task_field(task, "id") or "")
    receipt, receipt_run_id = reconciliation_receipt(runs)
    relevant_ids = {
        str(value) for value in (
            receipt.get("replacement_task_id"), receipt.get("canonical_live_task"),
        ) if value
    }
    related: list[dict[str, Any]] = []
    for other_id, context in sorted((tasks or {}).items(), key=lambda item: str(item[0])):
        other_task, other_runs = _context_parts(context)
        other_receipt, other_run_id = reconciliation_receipt(other_runs)
        if (
            str(other_id) not in relevant_ids
            and str(other_receipt.get("supersedes_task_id") or "") != task_id
        ):
            continue
        related.append({
            "id": str(other_id),
            "status": _task_field(other_task, "status"),
            "current_run_id": _task_field(other_task, "current_run_id"),
            "receipt_run_id": other_run_id,
            "receipt": other_receipt,
        })
    material = {
        "task": {
            "id": task_id,
            "status": _task_field(task, "status"),
            "assignee": _task_field(task, "assignee"),
            "workspace_kind": _task_field(task, "workspace_kind"),
            "workspace_path": _task_field(task, "workspace_path"),
            "branch_name": _task_field(task, "branch_name"),
            "current_run_id": _task_field(task, "current_run_id"),
            "claim_lock": _task_field(task, "claim_lock"),
        },
        "receipt_run_id": receipt_run_id,
        "receipt": receipt,
        "related": related,
    }
    encoded = json.dumps(
        material, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        default=str,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _profile_is_runnable(assignee: Any, profile_roster: Any) -> Optional[bool]:
    if not assignee or profile_roster is None:
        return None
    name = str(assignee).strip()
    if callable(profile_roster):
        try:
            return bool(profile_roster(name))
        except Exception:
            return None
    try:
        names = {
            str(getattr(item, "name", item)).strip().casefold()
            for item in profile_roster
        }
    except TypeError:
        return None
    return name.casefold() in names


def _replacement_identity(
    task: Any,
    receipt: Mapping[str, Any],
    tasks: Optional[Mapping[str, Any]],
) -> dict[str, Any]:
    """Require target-owned, terminal, mutually-linked replacement proof."""
    source_id = str(_task_field(task, "id") or "")
    named = {
        str(value) for value in (
            receipt.get("replacement_task_id"), receipt.get("canonical_live_task"),
        ) if value
    }
    contexts: dict[str, tuple[Any, list[Any], dict[str, Any], Optional[int]]] = {}
    for other_id, context in (tasks or {}).items():
        other_task, other_runs = _context_parts(context)
        other_receipt, other_run_id = reconciliation_receipt(other_runs)
        contexts[str(other_id)] = (other_task, other_runs, other_receipt, other_run_id)
        if str(other_receipt.get("supersedes_task_id") or "") == source_id:
            named.add(str(other_id))

    base = {
        "proven": False,
        "source_task_id": source_id,
        "replacement_task_id": next(iter(named), None) if len(named) == 1 else None,
        "canonical_live_task": None,
        "terminal_receipt": {},
        "reason": None,
    }
    if not named:
        return base
    if len(named) != 1:
        return {**base, "reason": "replacement_identity_ambiguous"}
    replacement_id = next(iter(named))
    base["replacement_task_id"] = replacement_id
    if replacement_id == source_id:
        return {**base, "reason": "replacement_cycle"}
    context = contexts.get(replacement_id)
    if context is None:
        return {**base, "reason": "replacement_not_found"}
    replacement_task, _runs, target_receipt, _run_id = context
    target_status = str(_task_field(replacement_task, "status") or "")
    if target_status not in {"done", "archived"}:
        return {**base, "reason": "replacement_nonterminal", "status": target_status}
    if str(target_receipt.get("supersedes_task_id") or "") != source_id:
        return {**base, "reason": "replacement_backref_missing", "status": target_status}
    canonical = str(target_receipt.get("canonical_live_task") or replacement_id)
    if canonical != replacement_id:
        return {**base, "reason": "replacement_canonical_mismatch", "status": target_status}
    if str(target_receipt.get("replacement_task_id") or "") == source_id:
        return {**base, "reason": "replacement_cycle", "status": target_status}
    terminal = target_receipt.get("terminal_receipt")
    terminal = dict(terminal) if isinstance(terminal, Mapping) else {}
    state = str(terminal.get("state") or "").strip().lower()
    head = str(terminal.get("head") or "").strip().lower()
    candidate_head = str(target_receipt.get("candidate_head") or "").strip().lower()
    if (
        state not in RECONCILIATION_TERMINAL_STATES
        or str(terminal.get("task_id") or "") != replacement_id
        or not _FULL_SHA_RE.fullmatch(head)
        or (candidate_head and candidate_head != head)
        or (state == "archived" and target_status != "archived")
    ):
        return {
            **base,
            "reason": "replacement_terminal_proof_invalid",
            "status": target_status,
        }
    return {
        **base,
        "proven": True,
        "replacement_task_id": replacement_id,
        "canonical_live_task": canonical,
        "terminal_receipt": terminal,
        "status": target_status,
    }


def _workspace_finding(
    task: Any,
    receipt: Mapping[str, Any],
    candidate: Mapping[str, Any],
    observation: GitProbeResult,
    *,
    git_probe: Any,
    workspace_probe: Optional[Callable[[Any], Any]] = None,
) -> Optional[tuple[str, dict[str, Any]]]:
    kind = str(_task_field(task, "workspace_kind") or "scratch")
    workspace_cfg = receipt.get("workspace")
    persistent = bool(
        isinstance(workspace_cfg, Mapping) and workspace_cfg.get("persistent") is True
    )
    if kind == "scratch" and not persistent:
        return None
    raw_path = _task_field(task, "workspace_path")
    if not raw_path:
        return "workspace_missing", {"path": None}
    path_text = str(raw_path)
    if path_text == "/home/openclaw" or path_text.startswith("/home/openclaw/"):
        return "workspace_retired", {"path": path_text}
    if persistent and (path_text == "/tmp" or path_text.startswith("/tmp/")):
        return "workspace_unsafe_tmp", {"path": path_text}

    probe_fn = workspace_probe
    if probe_fn is None and hasattr(git_probe, "probe_workspace"):
        probe_fn = git_probe.probe_workspace
    if probe_fn is None:
        probe_fn = GitProbeSession().probe_workspace
    try:
        workspace = probe_fn(raw_path)
    except Exception as exc:
        return "workspace_unknown", {"path": path_text, "error": str(exc)}
    if isinstance(workspace, Mapping):
        workspace = WorkspaceProbeResult(
            str(workspace.get("state") or "workspace_unknown"),
            str(workspace.get("path") or path_text),
            head=workspace.get("head"),
            root=workspace.get("root"),
            git_dir=workspace.get("git_dir"),
            common_dir=workspace.get("common_dir"),
            remote=workspace.get("remote"),
            linked_worktree=bool(workspace.get("linked_worktree")),
            evidence=dict(workspace.get("evidence") or {}),
        )
    if not isinstance(workspace, WorkspaceProbeResult):
        return "workspace_unknown", {"path": path_text, "reason": "invalid_probe_result"}
    if workspace.state != "workspace_current":
        return workspace.state, dict(workspace.evidence)
    if kind == "worktree" and not workspace.linked_worktree:
        return "workspace_not_linked", {"path": workspace.path}

    declared_repo = str(candidate.get("repo") or "")
    if _looks_like_local_repo(declared_repo):
        if observation.common_dir and (
            _normalize_repo_identity(workspace.common_dir) !=
            _normalize_repo_identity(observation.common_dir)
        ):
            return "workspace_wrong_repo", {
                "path": workspace.path,
                "declared_repo": declared_repo,
                "workspace_common_dir": workspace.common_dir,
                "expected_common_dir": observation.common_dir,
            }
    elif (
        workspace.remote is None
        or _normalize_repo_identity(workspace.remote) != _normalize_repo_identity(declared_repo)
    ):
        return "workspace_wrong_repo", {
            "path": workspace.path,
            "declared_repo": declared_repo,
            "workspace_remote": workspace.remote,
        }
    expected_head = str(candidate.get("candidate_head") or "").strip().lower()
    if not workspace.head:
        return "workspace_unknown", {"path": workspace.path, "reason": "workspace_head_unknown"}
    if workspace.head != expected_head:
        return "workspace_wrong_head", {
            "path": workspace.path,
            "workspace_head": workspace.head,
            "expected_head": expected_head,
        }
    return None


def reconcile_task(
    task: Any,
    runs: list[Any],
    *,
    tasks: Optional[Mapping[str, Any]] = None,
    now: Optional[int] = None,
    git_probe: Optional[Callable[..., Any]] = None,
    workspace_probe: Optional[Callable[[Any], Any]] = None,
    profile_roster: Any = None,
) -> ReconciliationResult:
    """Build one deterministic guard receipt without mutating board or Git."""
    now_ts = int(now if now is not None else time.time())
    task_id = str(_task_field(task, "id") or "")
    receipt, receipt_run_id = reconciliation_receipt(runs)
    probe = git_probe or GitProbeSession()
    candidate = {
        "repo": receipt.get("repo"),
        "ref": receipt.get("ref"),
        "target_ref": receipt.get("target_ref") or receipt.get("ref"),
        "candidate_head": receipt.get("candidate_head"),
    }
    result = ReconciliationResult(
        task_id=task_id,
        candidate=candidate,
        review=dict(receipt.get("review") or {})
        if isinstance(receipt.get("review"), Mapping) else {},
        receipt_run_id=receipt_run_id,
        opted_in=bool(receipt),
        last_verified_at=now_ts,
    )
    result.db_fingerprint = reconciliation_state_fingerprint(
        task, runs, tasks=tasks,
    )
    result.replacement = _replacement_identity(task, receipt, tasks)
    if result.replacement.get("replacement_task_id"):
        result.opted_in = True

    findings: list[Diagnostic] = []

    def add(
        code: str,
        *,
        severity: str = "error",
        dispatch_blocked: bool,
        suppressed: bool = False,
        evidence: Optional[dict[str, Any]] = None,
        next_action: str,
        next_owner: str,
    ) -> None:
        blocked_since = (
            _task_field(task, "started_at") or _task_field(task, "created_at") or now_ts
        )
        data = {
            "finding_code": code,
            "dispatch_blocked": dispatch_blocked,
            "suppressed": suppressed,
            "evidence": evidence or {},
            "candidate": candidate,
            "review": result.review,
            "replacement": result.replacement,
            "last_verified_at": now_ts,
            "blocker_class": "auto_actionable" if dispatch_blocked else "observational",
            "blocked_since": blocked_since,
            "age_seconds": max(0, now_ts - int(blocked_since)),
            "sla_seconds": 900,
            "next_action": next_action,
            "next_owner": next_owner,
            "canonical_live_task": result.replacement.get("canonical_live_task"),
            "autonomy_tier": str(receipt.get("autonomy_tier") or "private_reversible"),
        }
        findings.append(Diagnostic(
            kind=code,
            severity=severity,
            title=code.replace("_", " ").capitalize(),
            detail=json.dumps(data["evidence"], sort_keys=True, default=str),
            actions=[DiagnosticAction(
                kind="cli_hint",
                label=next_action,
                payload={"task_id": task_id},
                suggested=True,
            )],
            first_seen_at=int(blocked_since),
            last_seen_at=now_ts,
            data=data,
        ))

    identity_values = (
        candidate.get("repo"), candidate.get("target_ref"), candidate.get("candidate_head"),
    )
    identity_requested = any(value not in (None, "") for value in identity_values)
    observation = GitProbeResult("branch_unknown")
    if identity_requested:
        valid_identity = (
            isinstance(candidate.get("repo"), str)
            and bool(candidate["repo"].strip())
            and _canonical_exact_ref(candidate.get("target_ref")) is not None
            and isinstance(candidate.get("candidate_head"), str)
            and bool(_FULL_SHA_RE.fullmatch(candidate["candidate_head"].strip()))
        )
        if not valid_identity:
            result.head_state = "branch_unknown"
            add(
                "branch_unknown",
                dispatch_blocked=True,
                evidence={"reason": "repo_exact_ref_candidate_head_required"},
                next_action="repair candidate identity proof",
                next_owner="ops",
            )
        else:
            try:
                try:
                    observation = _probe_result(probe(
                        candidate["repo"], candidate["target_ref"],
                        candidate["candidate_head"],
                    ))
                except TypeError:
                    observation = _probe_result(probe(
                        candidate["repo"], candidate["target_ref"],
                    ))
            except Exception as exc:
                observation = GitProbeResult(
                    "branch_unknown", evidence={"reason": "probe_error", "error": str(exc)},
                )
            result.head_state = (
                observation.state
                if observation.state in RECONCILIATION_HEAD_STATES else "branch_unknown"
            )
            candidate["canonical_ref"] = observation.canonical_ref
            candidate["current_head"] = observation.current_head
            candidate["probe_evidence"] = observation.evidence
            if result.head_state != "head_current":
                add(
                    result.head_state,
                    dispatch_blocked=True,
                    evidence={
                        **observation.evidence,
                        "current_head": observation.current_head,
                    },
                    next_action="refresh or repair exact candidate ref/head",
                    next_owner="repair" if result.head_state != "branch_unknown" else "proof",
                )
            workspace_issue = _workspace_finding(
                task,
                receipt,
                candidate,
                observation,
                git_probe=probe,
                workspace_probe=workspace_probe,
            )
            if workspace_issue:
                code, evidence = workspace_issue
                add(
                    code,
                    dispatch_blocked=True,
                    evidence=evidence,
                    next_action="repair the declared workspace identity",
                    next_owner="ops",
                )

    review = result.review
    review_head = str(review.get("head") or "").strip().lower()
    candidate_head = str(candidate.get("candidate_head") or "").strip().lower()
    current_head = str(candidate.get("current_head") or "").strip().lower()
    exact_review = bool(
        _FULL_SHA_RE.fullmatch(review_head)
        and review_head == candidate_head == current_head
    )
    if review:
        result.review["exact_head"] = exact_review
    if review and not exact_review:
        add(
            "review_stale",
            severity="warning",
            dispatch_blocked=False,
            suppressed=False,
            evidence={
                "reviewed_head": review_head or None,
                "candidate_head": candidate_head or None,
                "current_head": current_head or None,
                "verdict": review.get("verdict"),
            },
            next_action="create an exact-head review receipt",
            next_owner="review",
        )

    replacement_reason = result.replacement.get("reason")
    if result.replacement.get("proven"):
        if str(_task_field(task, "status") or "") not in {"done", "archived"}:
            add(
                "replacement_suppressed",
                dispatch_blocked=True,
                suppressed=True,
                evidence=result.replacement.get("terminal_receipt") or {},
                next_action="continue on the proven canonical replacement",
                next_owner="board_hygiene",
            )
    elif replacement_reason:
        add(
            "replacement_unproven",
            severity="warning",
            dispatch_blocked=False,
            evidence={"reason": replacement_reason},
            next_action="obtain target-owned terminal replacement proof",
            next_owner="proof",
        )

    if result.opted_in and _task_field(task, "status") in {"ready", "review"}:
        runnable = _profile_is_runnable(_task_field(task, "assignee"), profile_roster)
        if runnable is False:
            add(
                "assignee_non_runnable",
                dispatch_blocked=True,
                evidence={
                    "assignee": _task_field(task, "assignee"),
                    "status": _task_field(task, "status"),
                },
                next_action="assign a runnable Hermes profile",
                next_owner="ops",
            )

    result.findings = findings
    return result


def reconciliation_diagnostics(*args: Any, **kwargs: Any) -> list[Diagnostic]:
    return reconcile_task(*args, **kwargs).findings


def _negative_verdict(metadata: Any) -> Optional[dict[str, Any]]:
    """Match only explicit structured verdict fields, never prose."""
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except Exception:
            return None
    if not isinstance(metadata, Mapping):
        return None

    verdict = metadata.get("verdict")
    if isinstance(verdict, str) and verdict.strip().lower() in _NEGATIVE_VERDICTS:
        return {
            "matched_field": "verdict",
            "matched_value": verdict,
        }
    if metadata.get("approved") is False:
        return {
            "matched_field": "approved",
            "matched_value": False,
        }
    # Review and QA are the explicit nested namespaces supported by the
    # lifecycle contract. Do not recurse through arbitrary metadata keys.
    for namespace in ("review", "qa"):
        nested = metadata.get(namespace)
        if isinstance(nested, Mapping):
            match = _negative_verdict(nested)
            if match:
                match["matched_field"] = f"{namespace}.{match['matched_field']}"
                return match
    return None


def _chain_inspection_action(task_id: str) -> DiagnosticAction:
    return DiagnosticAction(
        kind="cli_hint",
        label=f"Inspect Kanban task history: {task_id}",
        payload={"command": f"hermes kanban show {task_id}"},
        suggested=True,
    )


def _dependency_wait_loop_diagnostic(
    task: Any,
    events: list[Any],
    now: int,
) -> Optional[Diagnostic]:
    if _task_field(task, "status") in _TERMINAL_STATUSES:
        return None
    waits = 0
    repromotions = 0
    waiting_for_promotion = False
    wait_timestamps: list[int] = []
    promotion_timestamps: list[int] = []
    for ev in events:
        kind = _event_kind(ev)
        if kind == "dependency_wait":
            waits += 1
            wait_timestamps.append(_event_ts(ev))
            waiting_for_promotion = True
        elif kind == "promoted" and waiting_for_promotion:
            repromotions += 1
            promotion_timestamps.append(_event_ts(ev))
            waiting_for_promotion = False
    if waits < 2 or repromotions < 1:
        return None
    task_id = str(_task_field(task, "id") or "")
    first_seen = min(wait_timestamps) if wait_timestamps else now
    last_seen = max(promotion_timestamps or wait_timestamps)
    return Diagnostic(
        kind="dependency_wait_loop",
        severity="error",
        title="Dependency wait was repeatedly re-promoted",
        detail=(
            f"This non-terminal task recorded {waits} dependency waits and "
            f"{repromotions} subsequent promotion attempt(s). Inspect the "
            "parent links and lifecycle events before allowing another retry."
        ),
        actions=[_chain_inspection_action(task_id)] if task_id else [],
        first_seen_at=first_seen,
        last_seen_at=last_seen,
        count=waits,
        data={
            "dependency_wait_count": waits,
            "repromotion_count": repromotions,
            "status": _task_field(task, "status"),
        },
    )


def _terminal_active_run_diagnostic(
    task: Any,
    runs: list[Any],
    now: int,
) -> Optional[Diagnostic]:
    current_run_id = _task_field(task, "current_run_id")
    current_run = next((
        run for run in reversed(runs)
        if _task_field(run, "id") == current_run_id
    ), None)
    if (
        current_run is not None
        and _task_field(current_run, "ended_at") is None
        and _task_field(current_run, "worker_pid") is not None
        and not _task_field(current_run, "worker_identity")
    ):
        return Diagnostic(
            kind="worker_identity_unverifiable",
            severity="critical",
            title="Live worker has only a legacy numeric PID",
            detail=(
                "PID alone is not ownership proof. This run is retained "
                "fail-closed and cannot be signalled or finalized until exact "
                "worker identity is available or an operator resolves it."
            ),
            actions=[],
            first_seen_at=int(_task_field(current_run, "started_at", now) or now),
            last_seen_at=now,
            data={"status": _task_field(task, "status"), "run_id": current_run_id,
                  "reap_state": "identity_unverifiable"},
        )
    reap_run = next((
        run for run in reversed(runs)
        if str(_task_field(run, "reap_state") or "") in {
            "terminal_requested", "reap_pending", "reaping",
            "identity_unverifiable", "manual_recovery_required", "gave_up",
        }
    ), None)
    if reap_run is not None:
        reap_state = str(_task_field(reap_run, "reap_state"))
        kind_by_state = {
            "terminal_requested": "worker_reap_pending",
            "reap_pending": "worker_reap_pending",
            "reaping": "worker_reaping",
            "identity_unverifiable": "worker_identity_unverifiable",
            "manual_recovery_required": "worker_manual_recovery_required",
            "gave_up": "worker_reap_gave_up",
        }
        title_by_state = {
            "terminal_requested": "Terminal outcome is waiting for worker exit",
            "reap_pending": "Worker tree reaping is pending",
            "reaping": "A leased reaper owns this worker tree",
            "identity_unverifiable": "Worker identity cannot be verified",
            "manual_recovery_required": "Manual worker recovery is required",
            "gave_up": "Worker reaper gave up fail-closed",
        }
        run_id = _task_field(reap_run, "id")
        return Diagnostic(
            kind=kind_by_state[reap_state],
            severity=("critical" if reap_state in {
                "identity_unverifiable", "manual_recovery_required", "gave_up",
            }
                      else "warning"),
            title=title_by_state[reap_state],
            detail=(
                "The requested terminal outcome is fenced until the exact owned "
                "worker tree is confirmed gone. No dependency promotion, workspace "
                "release, replacement, or current-run clearing is allowed yet."
            ),
            actions=[],
            first_seen_at=int(
                _task_field(reap_run, "terminal_requested_at", now) or now
            ),
            last_seen_at=now,
            data={
                "status": _task_field(task, "status"),
                "run_id": run_id,
                "reap_state": reap_state,
                "attempt_uuid": _task_field(reap_run, "reap_attempt_uuid"),
                "lease_owner": _task_field(reap_run, "reap_lease_owner"),
                "lease_expires": _task_field(reap_run, "reap_lease_expires"),
                "heartbeat_at": _task_field(reap_run, "reap_heartbeat_at"),
                "attempts": int(_task_field(reap_run, "reap_attempts", 0) or 0),
                "term_sent_at": _task_field(reap_run, "reap_term_sent_at"),
                "kill_sent_at": _task_field(reap_run, "reap_kill_sent_at"),
            },
        )
    status = _task_field(task, "status")
    if status not in _TERMINAL_STATUSES:
        return None
    active_run = next((
        run for run in reversed(runs)
        if _task_field(run, "ended_at") is None
        and _task_field(run, "status") == "running"
    ), None)
    worker_pid = _task_field(task, "worker_pid")
    current_run_id = _task_field(task, "current_run_id")
    if worker_pid is None and current_run_id is None and active_run is None:
        return None
    run_id = current_run_id or _task_field(active_run, "id")
    return Diagnostic(
        kind="terminal_task_active_run",
        severity="critical",
        title="Terminal task still has an active worker run",
        detail=(
            "The task is terminal while its run or worker PID is still active. "
            "Stop/reclaim the worker before trusting the terminal state."
        ),
        actions=[DiagnosticAction(
            kind="reclaim",
            label="Stop and reclaim active run",
            payload={"run_id": run_id, "worker_pid": worker_pid},
            suggested=True,
        )],
        first_seen_at=int(_task_field(active_run, "started_at", now) or now),
        last_seen_at=now,
        data={
            "status": status,
            "run_id": run_id,
            "worker_pid": worker_pid,
        },
    )


def _duplicate_active_execution_diagnostics(
    tasks: Mapping[str, Any],
    runs_by_task: Any,
    now: int,
) -> dict[str, list[Diagnostic]]:
    """Flag explicit duplicate execution keys sharing one workspace."""
    buckets: dict[tuple[str, str], list[tuple[str, Any]]] = {}
    for task_id, task in tasks.items():
        if _task_field(task, "status") != "running":
            continue
        workspace = str(_task_field(task, "workspace_path") or "").strip()
        scope_key = str(_task_field(task, "idempotency_key") or "").strip()
        if workspace and scope_key:
            buckets.setdefault((workspace, scope_key), []).append((task_id, task))

    out: dict[str, list[Diagnostic]] = {}
    for (workspace, scope_key), members in buckets.items():
        if len(members) < 2:
            continue
        task_ids = [task_id for task_id, _ in members]
        run_ids = {
            task_id: (_task_field(task, "current_run_id") or _task_field(
                _latest_run(_data_by_task(runs_by_task, task_id)), "id"
            ))
            for task_id, task in members
        }
        for task_id, task in members:
            other_ids = [candidate for candidate in task_ids if candidate != task_id]
            out.setdefault(task_id, []).append(Diagnostic(
                kind="duplicate_active_execution",
                severity="critical",
                title="Duplicate active execution shares workspace and scope key",
                detail=(
                    "Multiple running tasks share an explicit idempotency key and "
                    "workspace. Stop/reclaim one run before either mutates further."
                ),
                actions=[DiagnosticAction(
                    kind="reclaim",
                    label="Stop and reclaim this duplicate run",
                    payload={"run_id": run_ids[task_id]},
                    suggested=True,
                )],
                first_seen_at=int(_task_field(task, "started_at", now) or now),
                last_seen_at=now,
                data={
                    "workspace_path": workspace,
                    "scope_key": scope_key,
                    "task_id": task_id,
                    "run_id": run_ids[task_id],
                    "conflicting_task_ids": other_ids,
                    "conflicting_run_ids": [run_ids[item] for item in other_ids],
                },
            ))
    return out


def compute_chain_diagnostics(
    tasks: Iterable[Any] | Mapping[str, Any],
    links: Iterable[Any],
    events_by_task: Any,
    runs_by_task: Any,
    *,
    now: Optional[int] = None,
    review_handoffs: Iterable[Any] = (),
) -> dict[str, list[Diagnostic]]:
    """Return diagnostics that require task links and cross-task history.

    ``tasks`` may be task rows, dataclasses, dicts, or an id-keyed mapping.
    ``links`` accepts ``(parent_id, child_id)`` pairs or rows/dicts with
    ``parent_id``/``child_id``. Events and runs may be id-keyed mappings or
    flat rows. Findings are attached to the affected child so the active
    work card carries the operator signal; each payload identifies both ends
    of the link and their current statuses.

    This intentionally diagnoses the v1 status-only dependency contract. It
    does not change promotion semantics or invent a verdict-aware link type.
    """
    now_ts = int(now if now is not None else time.time())
    task_map = _task_map(tasks)
    links = list(links)
    out: dict[str, list[Diagnostic]] = {}
    for link in links:
        parent_id, child_id = _link_ids(link)
        if not parent_id or not child_id:
            continue
        parent = task_map.get(str(parent_id))
        child = task_map.get(str(child_id))
        if parent is None or child is None:
            continue
        parent_status = _task_field(parent, "status")
        child_status = _task_field(child, "status")
        parent_events = _data_by_task(events_by_task, str(parent_id))
        child_id_str = str(child_id)
        parent_id_str = str(parent_id)

        if parent_status == "blocked" and child_status == "todo":
            reason = _review_required_reason(parent_events)
            if reason is not None:
                out.setdefault(child_id_str, []).append(Diagnostic(
                    kind="legacy_review_parent_gates_child",
                    severity="warning",
                    title="Review-required parent is gating this task",
                    detail=(
                        f"Parent {parent_id_str} is blocked for a review-required "
                        f"handoff while this child remains todo. The status-only "
                        "link does not carry a review verdict or release policy."
                    ),
                    actions=[
                        _chain_inspection_action(parent_id_str),
                        DiagnosticAction(
                            kind="cli_hint",
                            label="Register this one review gate after inspecting the chain",
                            payload={
                                "command": (
                                    f"hermes kanban link {parent_id_str} {child_id_str} "
                                    "--relationship review_gate"
                                ),
                                "bounded": True,
                                "bulk_promote": False,
                            },
                        ),
                    ],
                    first_seen_at=_event_ts(_latest_event(parent_events, {"blocked"})) or now_ts,
                    last_seen_at=_event_ts(_latest_event(parent_events, {"blocked"})) or now_ts,
                    data={
                        "parent_id": parent_id_str,
                        "parent_status": parent_status,
                        "child_id": child_id_str,
                        "child_status": child_status,
                        "matched_reason_prefix": _REVIEW_REQUIRED_PREFIX,
                        "reason": reason,
                    },
                ))

        if (
            parent_status in _RELEASE_PARENT_STATUSES
            and child_status in _RELEASED_CHILD_STATUSES
        ):
            parent_run = _latest_run(_data_by_task(runs_by_task, parent_id_str))
            negative = _negative_verdict(
                _task_field(parent_run, "metadata") if parent_run is not None else None
            )
            if negative is not None:
                out.setdefault(child_id_str, []).append(Diagnostic(
                    kind="negative_parent_verdict_released_child",
                    severity="error",
                    title="Negative parent verdict is not represented in the link",
                    detail=(
                        f"Parent {parent_id_str} is {parent_status} with a structured negative "
                        f"verdict, but this child is {child_status}. The status-only "
                        "link has released or is releasing the child without an "
                        "explicit verdict gate."
                    ),
                    actions=[_chain_inspection_action(parent_id_str)],
                    first_seen_at=int(_task_field(parent_run, "ended_at", now_ts) or now_ts),
                    last_seen_at=int(_task_field(parent_run, "ended_at", now_ts) or now_ts),
                    data={
                        "parent_id": parent_id_str,
                        "parent_status": parent_status,
                        "child_id": child_id_str,
                        "child_status": child_status,
                        **negative,
                    },
                ))

    links_set = {
        tuple(str(item) for item in _link_ids(link))
        for link in links
        if all(_link_ids(link))
    }
    for handoff in review_handoffs:
        source_id = str(_task_field(handoff, "source_task_id") or "")
        review_id = str(_task_field(handoff, "review_task_id") or "")
        next_id_raw = _task_field(handoff, "next_task_id")
        next_id = str(next_id_raw) if next_id_raw else None
        state = str(_task_field(handoff, "state") or "")
        source = task_map.get(source_id)
        review = task_map.get(review_id)
        problems: list[str] = []
        if source is None or review is None:
            problems.append("relationship references a missing source or review task")
        else:
            source_status = str(_task_field(source, "status") or "")
            review_status = str(_task_field(review, "status") or "")
            parked = (source_id, review_id) in links_set
            if state == "waiting":
                if not parked or review_status != "todo":
                    problems.append("waiting gate is not parked as a todo child of its source")
            elif state == "active":
                if parked:
                    problems.append("active review gate is still parent-gated")
                if source_status != "blocked":
                    problems.append(f"active source is {source_status}, expected blocked")
                if review_status not in {"review", "running", "blocked"}:
                    problems.append(
                        f"active review gate is {review_status}, expected review/running/blocked"
                    )
            elif state == "changes_requested":
                if source_status not in {"todo", "ready", "running"}:
                    problems.append(f"recut source is {source_status}, expected todo/ready/running")
                if review_status != "done":
                    problems.append(f"closed review gate is {review_status}, expected done")
            elif state == "approved":
                if source_status != "done" or review_status != "done":
                    problems.append("approved handoff did not close source and review gate")
            else:
                problems.append(f"unknown review handoff state {state!r}")
        if next_id and next_id not in task_map:
            problems.append("relationship references a missing next gate")
        if problems:
            out.setdefault(review_id or source_id, []).append(Diagnostic(
                kind="review_handoff_invariant_violation",
                severity="critical",
                title="Explicit review handoff invariant is broken",
                detail="; ".join(problems),
                actions=[DiagnosticAction(
                    kind="cli_hint",
                    label="Inspect the three-card chain; do not bulk-promote",
                    payload={
                        "command": f"hermes kanban show {source_id}",
                        "bounded": True,
                        "bulk_promote": False,
                    },
                    suggested=True,
                )],
                first_seen_at=int(_task_field(handoff, "updated_at", now_ts) or now_ts),
                last_seen_at=now_ts,
                data={
                    "source_task_id": source_id,
                    "review_task_id": review_id,
                    "next_task_id": next_id,
                    "state": state,
                    "problems": problems,
                },
            ))

    for task_id, task in task_map.items():
        diagnostic = _dependency_wait_loop_diagnostic(
            task, _data_by_task(events_by_task, task_id), now_ts,
        )
        if diagnostic is not None:
            out.setdefault(task_id, []).append(diagnostic)
        diagnostic = _terminal_active_run_diagnostic(
            task, _data_by_task(runs_by_task, task_id), now_ts,
        )
        if diagnostic is not None:
            out.setdefault(task_id, []).append(diagnostic)

    for task_id, diagnostics in _duplicate_active_execution_diagnostics(
        task_map, runs_by_task, now_ts,
    ).items():
        out.setdefault(task_id, []).extend(diagnostics)

    severity_idx = {s: i for i, s in enumerate(SEVERITY_ORDER)}
    for diagnostics in out.values():
        diagnostics.sort(
            key=lambda d: (
                -severity_idx.get(d.severity, -1),
                -(d.last_seen_at or 0),
            )
        )
    return out


# ---------------------------------------------------------------------------
# Rule implementations
# ---------------------------------------------------------------------------

# Each rule takes (task, events, runs, now_ts, config) and returns
# zero or more Diagnostic instances. ``events`` / ``runs`` are lists of
# kanban_db.Event / kanban_db.Run (or plain dicts matching the same
# shape — for test convenience).

RuleFn = Callable[[Any, list[Any], list[Any], int, dict], list[Diagnostic]]


def _aux_slot_explicit(slot: Any) -> bool:
    """Return True if the auxiliary slot has user-supplied non-default fields.

    Defaults from ``DEFAULT_CONFIG`` use ``provider: "auto"`` with empty
    model/base_url/api_key — that path falls through to the main model. An
    "explicit" config is one where the user actively set a provider (not
    "auto"), or supplied a model / base_url / api_key.
    """
    if not isinstance(slot, dict):
        return False
    provider = str(slot.get("provider") or "").strip().lower()
    if provider and provider != "auto":
        return True
    for key in ("model", "base_url", "api_key"):
        if str(slot.get(key) or "").strip():
            return True
    return False


def _main_model_visible(raw_config: Any) -> bool:
    """Best-effort check that a main model is configured.

    Diagnostics runs in the dashboard process which may not share the CLI's
    runtime state, so we read the raw config dict. If we cannot prove the
    main model is set, we err on the side of NOT firing the diagnostic.
    """
    if not isinstance(raw_config, dict):
        return False
    model_cfg = raw_config.get("model")
    if isinstance(model_cfg, dict):
        provider = str(model_cfg.get("provider") or "").strip()
        model = str(
            model_cfg.get("default")
            or model_cfg.get("model")
            or model_cfg.get("name")
            or ""
        ).strip()
        return bool(provider and model)
    return bool(str(model_cfg or "").strip())


def triage_aux_status(config: Optional[dict]) -> Optional[dict]:
    """Inspect raw config and report whether triage paths look configured.

    Returns ``None`` when config context is unavailable (suppress diagnostic
    to avoid noisy false positives in tests / low-level callers). Otherwise
    returns a dict with:

      - ``auto_decompose``: bool — whether the dispatcher auto-runs decompose
      - ``decomposer_explicit``: bool — user-supplied decomposer slot
      - ``specifier_explicit``: bool — user-supplied specifier slot
      - ``main_model_visible``: bool — main model can serve as auto fallback
    """
    if not isinstance(config, dict):
        return None

    explicit = config.get("triage_aux_status")
    if isinstance(explicit, dict):
        return explicit

    aux = config.get("auxiliary")
    kanban_cfg = config.get("kanban") if isinstance(config.get("kanban"), dict) else {}

    # Have we been handed any config context at all? When neither auxiliary
    # nor kanban nor model keys are present, the caller is a low-level test
    # passing {} — stay silent.
    if (
        not isinstance(aux, dict)
        and not kanban_cfg
        and "model" not in config
    ):
        return None

    decomposer_explicit = False
    specifier_explicit = False
    if isinstance(aux, dict):
        decomposer_explicit = _aux_slot_explicit(aux.get("kanban_decomposer"))
        specifier_explicit = _aux_slot_explicit(aux.get("triage_specifier"))

    # ``auto_decompose`` defaults to True per kanban DEFAULT_CONFIG.
    auto_decompose = True
    if isinstance(kanban_cfg, dict) and "auto_decompose" in kanban_cfg:
        auto_decompose = bool(kanban_cfg.get("auto_decompose"))

    return {
        "auto_decompose": auto_decompose,
        "decomposer_explicit": decomposer_explicit,
        "specifier_explicit": specifier_explicit,
        "main_model_visible": _main_model_visible(config),
    }


def _positive_int(value: Any, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= 1 else default


def _rule_hallucinated_cards(task, events, runs, now, cfg) -> list[Diagnostic]:
    """Blocked-hallucination gate fires: a worker called kanban_complete
    with created_cards that didn't exist or weren't created by the
    completing profile. Task stayed in its prior state; the operator
    needs to decide how to proceed.

    Auto-clears when a successful completion (or edit) follows the
    blocked event.
    """
    hits = _active_hallucination_events(events, "completion_blocked_hallucination")
    if not hits:
        return []
    phantom_ids: list[str] = []
    first = _event_ts(hits[0])
    last = _event_ts(hits[-1])
    for ev in hits:
        payload = _parse_payload(ev)
        for pid in payload.get("phantom_cards", []) or []:
            if pid not in phantom_ids:
                phantom_ids.append(pid)
    running = _task_field(task, "status") == "running"
    actions: list[DiagnosticAction] = []
    actions.append(DiagnosticAction(
        kind="comment",
        label="Add a comment explaining what to do",
        suggested=False,
    ))
    actions.extend(_generic_recovery_actions(task, running=running))
    return [Diagnostic(
        kind="hallucinated_cards",
        severity="error",
        title="Worker claimed cards that don't exist",
        detail=(
            "The completing worker declared created_cards that either didn't "
            "exist or weren't created by its profile. The completion was "
            "blocked and the task stayed in its prior state. "
            "Usually means the worker hallucinated ids instead of capturing "
            "return values from kanban_create."
        ),
        actions=actions,
        first_seen_at=first,
        last_seen_at=last,
        count=len(hits),
        data={"phantom_ids": phantom_ids},
    )]


def _rule_triage_aux_unavailable(task, events, runs, now, cfg) -> list[Diagnostic]:
    """A triage task cannot leave triage without an auxiliary helper.

    With the auto-decompose dispatcher (kanban.auto_decompose, default True),
    triage tasks fan out via ``auxiliary.kanban_decomposer`` and fall back to
    ``auxiliary.triage_specifier`` when the decomposer returns ``fanout=false``.
    With auto-decompose off, the user must run ``hermes kanban specify``,
    which only needs ``auxiliary.triage_specifier``.

    The default slot is ``provider: auto`` → auto-falls back to the main model,
    so this rule only fires when:

      - the relevant slot is explicitly set to something broken, OR
      - the auto fallback has no main model to fall back to.

    Config context is required; pass {} from tests to keep the rule silent.
    """
    if _task_field(task, "status") != "triage":
        return []

    status = triage_aux_status(cfg)
    if status is None:
        return []

    auto_decompose = bool(status.get("auto_decompose"))
    decomposer_explicit = bool(status.get("decomposer_explicit"))
    specifier_explicit = bool(status.get("specifier_explicit"))
    main_visible = bool(status.get("main_model_visible"))

    # Determine the primary slot and whether it is usable.
    if auto_decompose:
        primary_slot = "auxiliary.kanban_decomposer"
        primary_explicit = decomposer_explicit
        fallback_slot = "auxiliary.triage_specifier"
        fallback_explicit = specifier_explicit
        primary_desc = "decomposer"
        detail_path = (
            "Auto-decompose is on, so the dispatcher needs "
            "auxiliary.kanban_decomposer (with auxiliary.triage_specifier as "
            "a fallback for non-fan-out tasks)."
        )
    else:
        primary_slot = "auxiliary.triage_specifier"
        primary_explicit = specifier_explicit
        fallback_slot = "auxiliary.kanban_decomposer"
        fallback_explicit = decomposer_explicit
        primary_desc = "specifier"
        detail_path = (
            "Auto-decompose is off, so triage tasks need "
            "`hermes kanban specify`, which uses auxiliary.triage_specifier."
        )

    # The primary slot is usable when either: it was explicitly configured by
    # the user, OR the default `provider: auto` can fall back to the main
    # model. If both fail, we have a real configuration gap.
    if primary_explicit or main_visible:
        return []

    task_id = _task_field(task, "id") or "<task_id>"
    actions = [
        DiagnosticAction(
            kind="cli_hint",
            label=f"Configure {primary_slot}",
            payload={
                "command": (
                    f"hermes config set {primary_slot}.provider auto"
                )
            },
            suggested=True,
        ),
    ]
    if not fallback_explicit and not main_visible:
        actions.append(DiagnosticAction(
            kind="cli_hint",
            label=f"Or configure fallback {fallback_slot}",
            payload={
                "command": (
                    f"hermes config set {fallback_slot}.provider auto"
                )
            },
        ))
    if not auto_decompose:
        actions.append(DiagnosticAction(
            kind="cli_hint",
            label=f"Specify manually: hermes kanban specify {task_id}",
            payload={"command": f"hermes kanban specify {task_id}"},
        ))

    return [Diagnostic(
        kind="triage_aux_unavailable",
        severity="warning",
        title=f"Triage {primary_desc} has no usable model",
        detail=(
            f"This task is still in triage and no working auxiliary model is "
            f"visible to the dispatcher. {detail_path} The default slot uses "
            f"`provider: auto` which falls back to the main model, but no main "
            f"model is configured either. Configure the slot directly or set a "
            f"main model so the auto fallback can take over."
        ),
        actions=actions,
        first_seen_at=now,
        last_seen_at=now,
        count=1,
        data={
            "task_id": task_id,
            "auto_decompose": auto_decompose,
            "primary_slot": primary_slot,
            "main_model_visible": main_visible,
        },
    )]


def _rule_prose_phantom_refs(task, events, runs, now, cfg) -> list[Diagnostic]:
    """Advisory prose-scan: the completion summary mentions ``t_<hex>``
    ids that don't resolve. Non-blocking; surfaced as a warning only.

    Auto-clears when a fresh clean completion arrives AFTER the
    suspected event.
    """
    hits = _active_hallucination_events(events, "suspected_hallucinated_references")
    if not hits:
        return []
    phantom_refs: list[str] = []
    for ev in hits:
        for pid in _parse_payload(ev).get("phantom_refs", []) or []:
            if pid not in phantom_refs:
                phantom_refs.append(pid)
    running = _task_field(task, "status") == "running"
    return [Diagnostic(
        kind="prose_phantom_refs",
        severity="warning",
        title="Completion summary references unknown task ids",
        detail=(
            "The completion summary mentions task ids that don't resolve "
            "in this board's database. The completion itself succeeded, "
            "but downstream consumers parsing the summary may be pointed "
            "at cards that never existed."
        ),
        actions=_generic_recovery_actions(task, running=running),
        first_seen_at=_event_ts(hits[0]),
        last_seen_at=_event_ts(hits[-1]),
        count=len(hits),
        data={"phantom_refs": phantom_refs},
    )]


def _rule_repeated_failures(task, events, runs, now, cfg) -> list[Diagnostic]:
    """Task's unified ``consecutive_failures`` counter is climbing —
    something about this task+profile combo is broken and each retry
    fails the same way. Triggers regardless of the specific failure
    mode (spawn error, timeout, crash) because operationally they
    all look the same: the kernel keeps retrying and the operator
    needs to intervene.

    Threshold: cfg["failure_threshold"]. Runtime callers should derive
    this from ``kanban.failure_limit`` unless the user explicitly set a
    diagnostics threshold, so the signal does not lag behind the
    dispatcher's circuit breaker.

    Accepts the legacy ``spawn_failure_threshold`` config key for
    back-compat.

    Terminal statuses are exempt: a done/archived card has nothing left
    to retry, so a lingering failure streak is history, not a signal.
    (``complete_task`` resets the counter, but a manual done — e.g. a
    dashboard drag — ends no run and used to leave the flag stuck.)

    A fresh attempt in flight (``running``) is also exempt: retrying a
    task should clear the stale failure banner until this attempt also
    resolves. Otherwise a card that's actively trying again still shows
    "failed Nx", which reads as a current failure. It re-fires if the new
    run fails too (status leaves ``running`` with a recorded outcome).
    """
    if _task_field(task, "status") in (_TERMINAL_STATUSES | {"running"}):
        return []
    threshold = _positive_int(cfg.get(
        "failure_threshold",
        cfg.get("spawn_failure_threshold", 3),
    ), 3)
    failure_limit = _positive_int(cfg.get("failure_limit"), threshold)
    # Read the new unified counter name, with a fallback to the legacy
    # column name so this rule keeps working against old DB rows the
    # caller somehow materialised without running the migration.
    failures = (
        _task_field(task, "consecutive_failures", None)
        if _task_field(task, "consecutive_failures", None) is not None
        else _task_field(task, "spawn_failures", 0)
    )
    if failures is None or failures < threshold:
        return []
    last_err = (
        _task_field(task, "last_failure_error", None)
        if _task_field(task, "last_failure_error", None) is not None
        else _task_field(task, "last_spawn_error", None)
    )
    assignee = _task_field(task, "assignee")

    # Classify the most recent failure by peeking at run outcomes so
    # the title + suggested action can be specific without a separate
    # per-outcome rule.
    ordered_runs = sorted(runs, key=lambda r: _task_field(r, "id", 0))
    most_recent_outcome = None
    for r in reversed(ordered_runs):
        oc = _task_field(r, "outcome")
        if oc in {"spawn_failed", "timed_out", "crashed"}:
            most_recent_outcome = oc
            break

    actions: list[DiagnosticAction] = []
    if most_recent_outcome == "spawn_failed" and assignee and assignee != "default":
        # Spawn is failing specifically — profile setup issue.
        actions.append(DiagnosticAction(
            kind="cli_hint",
            label=f"Verify profile: hermes -p {assignee} doctor",
            payload={"command": f"hermes -p {assignee} doctor"},
            suggested=True,
        ))
        actions.append(DiagnosticAction(
            kind="cli_hint",
            label=f"Fix profile auth: hermes -p {assignee} auth",
            payload={"command": f"hermes -p {assignee} auth"},
        ))
    elif most_recent_outcome in {"timed_out", "crashed"}:
        # Worker got off the ground but died. Logs are the right place
        # to diagnose; reclaim/reassign are the recovery levers.
        task_id = _task_field(task, "id")
        if task_id:
            actions.append(DiagnosticAction(
                kind="cli_hint",
                label=f"Check logs: hermes kanban log {task_id}",
                payload={"command": f"hermes kanban log {task_id}"},
                suggested=True,
            ))
    actions.extend(_generic_recovery_actions(
        task, running=_task_field(task, "status") == "running",
    ))

    severity = "critical" if failures >= threshold * 2 else "error"
    err_text = (last_err or "").strip() if last_err else ""
    err_snippet = err_text[:500] + ("…" if len(err_text) > 500 else "") if err_text else ""
    outcome_label = {
        "spawn_failed": "spawn",
        "timed_out": "timeout",
        "crashed": "crash",
    }.get(most_recent_outcome or "", "failure")
    if err_snippet:
        title = f"Agent {outcome_label} x{failures}: {err_snippet.splitlines()[0][:160]}"
        detail = (
            f"This task has failed {failures} times in a row "
            f"(most recent: {outcome_label}). Full last error:\n\n"
            f"{err_snippet}\n\n"
            f"The dispatcher circuit breaker is configured for "
            f"{failure_limit} consecutive non-success attempts. Fix the "
            f"root cause and reclaim or unblock the task to retry."
        )
    else:
        title = f"Agent {outcome_label} x{failures} (no error recorded)"
        detail = (
            f"This task has failed {failures} times in a row "
            f"(most recent: {outcome_label}) but no error text was "
            f"captured. Check the suggested command or the worker log."
        )
    return [Diagnostic(
        kind="repeated_failures",
        severity=severity,
        title=title,
        detail=detail,
        actions=actions,
        first_seen_at=now,
        last_seen_at=now,
        count=failures,
        data={
            "consecutive_failures": failures,
            "most_recent_outcome": most_recent_outcome,
            "last_error": last_err,
            "failure_threshold": threshold,
            "failure_limit": failure_limit,
        },
    )]


def _rule_repeated_crashes(task, events, runs, now, cfg) -> list[Diagnostic]:
    """The worker spawns fine but keeps crashing mid-run. Check the last
    N runs' outcomes; N consecutive ``crashed`` without a successful
    ``completed`` means something about the task + profile combo is
    broken (OOM, missing dependency, tool it needs is down).

    Threshold: cfg["crash_threshold"] (default 2).

    Narrower than ``repeated_failures`` — fires earlier (2 crashes vs 3
    total failures) so the operator gets a crash-specific heads-up
    before the unified rule kicks in. Suppresses itself when the
    unified rule is also about to fire, to avoid double-flagging.

    Terminal statuses are exempt for the same reason as
    ``repeated_failures`` — with one extra wrinkle: this rule reads run
    history, and a manual done (dashboard drag) appends no ``completed``
    run to break the crash streak, so the flag was permanent (#kanban
    desktop dogfood). Done means done.

    ``running`` is exempt too: a fresh attempt is in flight, and its
    in-flight run (no outcome yet) doesn't break the trailing crash scan,
    so a retried card kept showing "crashed Nx" over an active run. The
    banner re-fires if the new attempt also crashes.
    """
    if _task_field(task, "status") in (_TERMINAL_STATUSES | {"running"}):
        return []
    failure_threshold = int(cfg.get(
        "failure_threshold",
        cfg.get("spawn_failure_threshold", 3),
    ))
    unified_counter = (
        _task_field(task, "consecutive_failures", 0) or 0
    )
    # Unified rule will catch this — let it handle to avoid double fire.
    if unified_counter >= failure_threshold:
        return []

    threshold = int(cfg.get("crash_threshold", 2))
    ordered = sorted(runs, key=lambda r: _task_field(r, "id", 0))
    # Count trailing consecutive 'crashed' outcomes.
    consecutive = 0
    last_err = None
    for r in reversed(ordered):
        outcome = _task_field(r, "outcome")
        if outcome == "crashed":
            consecutive += 1
            if last_err is None:
                last_err = _task_field(r, "error")
        elif outcome in {"completed", "reclaimed"}:
            # A success (or manual reclaim) breaks the streak.
            break
        else:
            # Other outcomes (timed_out, blocked, spawn_failed, gave_up)
            # aren't crash signals — don't count them, but they also
            # don't break the crash streak.
            continue
    if consecutive < threshold:
        return []
    task_id = _task_field(task, "id")
    actions: list[DiagnosticAction] = []
    if task_id:
        actions.append(DiagnosticAction(
            kind="cli_hint",
            label=f"Check logs: hermes kanban log {task_id}",
            payload={"command": f"hermes kanban log {task_id}"},
            suggested=True,
        ))
    running = _task_field(task, "status") == "running"
    actions.extend(_generic_recovery_actions(task, running=running))
    severity = "critical" if consecutive >= threshold * 2 else "error"
    # Put the actual error up-front so operators see WHAT broke without
    # having to open the logs. Truncate defensively — these can be huge
    # (full tracebacks).
    err_text = (last_err or "").strip() if last_err else ""
    err_snippet = err_text[:500] + ("…" if len(err_text) > 500 else "") if err_text else ""
    if err_snippet:
        title = f"Agent crashed {consecutive}x: {err_snippet.splitlines()[0][:160]}"
        detail = (
            f"The last {consecutive} runs ended with outcome=crashed. "
            f"Full last error:\n\n{err_snippet}"
        )
    else:
        title = f"Agent crashed {consecutive}x (no error recorded)"
        detail = (
            f"The last {consecutive} runs ended with outcome=crashed but "
            f"no error text was captured. Check the worker log for more."
        )
    return [Diagnostic(
        kind="repeated_crashes",
        severity=severity,
        title=title,
        detail=detail,
        actions=actions,
        first_seen_at=now,
        last_seen_at=now,
        count=consecutive,
        data={"consecutive_crashes": consecutive, "last_error": last_err},
    )]


def _rule_stuck_in_blocked(task, events, runs, now, cfg) -> list[Diagnostic]:
    """Task has been in ``blocked`` status for too long without a comment.

    Threshold: cfg["blocked_stale_hours"] (default 24).
    Surfaced as a warning so humans know there's a pending unblock.
    """
    hours = float(cfg.get("blocked_stale_hours", 24))
    status = _task_field(task, "status")
    if status != "blocked":
        return []
    # Find the most recent ``blocked`` event.
    last_blocked_ts = 0
    for ev in events:
        if _event_kind(ev) == "blocked":
            t = _event_ts(ev)
            last_blocked_ts = max(last_blocked_ts, t)
    if last_blocked_ts == 0:
        return []
    age_hours = (now - last_blocked_ts) / 3600.0
    if age_hours < hours:
        return []
    # Any comment / unblock after the block breaks the "stale" signal.
    for ev in events:
        if _event_kind(ev) in {"commented", "unblocked"} and _event_ts(ev) > last_blocked_ts:
            return []
    actions: list[DiagnosticAction] = [
        DiagnosticAction(
            kind="comment",
            label="Add a comment / unblock the task",
            suggested=True,
        ),
    ]
    return [Diagnostic(
        kind="stuck_in_blocked",
        severity="warning",
        title=f"Task has been blocked for {int(age_hours)}h",
        detail=(
            f"This task transitioned to blocked {int(age_hours)}h ago and "
            f"has had no comments or unblock attempts since. Blocked tasks "
            f"are waiting for human input — check the block reason and "
            f"either unblock with feedback or answer with a comment."
        ),
        actions=actions,
        first_seen_at=last_blocked_ts,
        last_seen_at=last_blocked_ts,
        count=1,
        data={"blocked_at": last_blocked_ts, "age_hours": round(age_hours, 1)},
    )]


def _rule_block_unblock_cycling(task, events, runs, now, cfg) -> list[Diagnostic]:
    """Task has cycled through blocked → unblocked many times — the
    ``unblock`` is not fixing the underlying problem and the worker
    keeps re-blocking for substantially the same reason.

    ``_rule_stuck_in_blocked`` resets its timer on any ``commented`` /
    ``unblocked`` event, so a task that cycles every few minutes is
    invisible to it regardless of how many times it cycles (#29747
    gap 1). This rule complements that one by counting block→unblock
    cycles in a sliding window.

    Threshold: cfg["block_cycle_threshold"] (default 3) cycles within
    cfg["block_cycle_window_seconds"] (default 24h).
    """
    threshold = _positive_int(cfg.get("block_cycle_threshold"), 3)
    window_seconds = float(cfg.get("block_cycle_window_seconds", 24 * 3600))
    cycle_cutoff = now - window_seconds

    # Walk events chronologically (arrival order — callers pre-sort by
    # id, which is the canonical chronological order; ``created_at``
    # alone is insufficient because multiple events can share the same
    # second).  Count "blocked after unblocked" transitions: every time
    # a blocked event follows at least one unblocked event since the
    # last cycle was counted, that's a new cycle.
    cycles = 0
    seen_unblock_since_last_cycle = False
    initial_blocked_ts = 0
    last_cycle_blocked_ts = 0
    for ev in events:
        ts = _event_ts(ev)
        if ts < cycle_cutoff:
            continue
        kind = _event_kind(ev)
        if kind == "blocked":
            if initial_blocked_ts == 0:
                initial_blocked_ts = ts
            if seen_unblock_since_last_cycle:
                cycles += 1
                last_cycle_blocked_ts = ts
                seen_unblock_since_last_cycle = False
        elif kind == "unblocked":
            seen_unblock_since_last_cycle = True

    if cycles < threshold:
        return []

    task_id = _task_field(task, "id")
    actions: list[DiagnosticAction] = []
    if task_id:
        actions.append(DiagnosticAction(
            kind="cli_hint",
            label=f"Check block reasons: hermes kanban show {task_id}",
            payload={"command": f"hermes kanban show {task_id}"},
            suggested=True,
        ))
    return [Diagnostic(
        kind="block_unblock_cycling",
        severity="warning",
        title=f"Task block→unblock cycled {cycles}x in {int(window_seconds/3600)}h",
        detail=(
            f"This task has been blocked {cycles} times after being "
            "unblocked, suggesting the unblock is not addressing the "
            "root cause and the worker keeps hitting the same wall. "
            "Review the block reasons in the event history; a different "
            "intervention (reassign, change scope, archive) may be needed."
        ),
        actions=actions,
        first_seen_at=int(initial_blocked_ts) if initial_blocked_ts else int(now),
        last_seen_at=int(last_cycle_blocked_ts) if last_cycle_blocked_ts else int(now),
        count=cycles,
        data={
            "cycles": cycles,
            "window_seconds": int(window_seconds),
        },
    )]


def _rule_stranded_in_ready(task, events, runs, now, cfg) -> list[Diagnostic]:
    """Task has been in ``ready`` status for too long without any worker
    claiming it.

    Threshold: cfg["stranded_threshold_seconds"] (default 1800 = 30 min).

    Catches every "task waiting for a worker that never comes" case
    without caring WHY:

    * Operator typo'd the assignee — no profile or external worker matches.
    * Profile was deleted, leaving its tasks stranded.
    * External worker pool (Codex CLI, Claude Code lane, custom daemon)
      is down, hung, or wasn't started.
    * Dispatcher is misconfigured (wrong board, wrong HERMES_HOME).

    Pre-rule, all of these silently rotted in ``skipped_nonspawnable`` —
    the dispatcher correctly skipped them (good — no respawn loop) but
    nobody surfaced the fact that operator-actionable work was
    accumulating. The rule fires when a ready task's promoted-to-ready
    timestamp is older than the threshold AND the assignee is non-empty
    (truly unassigned tasks have their own ``skipped_unassigned`` signal
    on the dispatcher and a different operator response).

    The signal is age-based on purpose: it's identity-agnostic, so it
    works for Hermes profiles, registered lanes, external workers, and
    typos uniformly. No registry to curate, no per-board allowlist.
    """
    threshold_seconds = float(
        cfg.get("stranded_threshold_seconds", 30 * 60)
    )
    status = _task_field(task, "status")
    if status != "ready":
        return []
    # Skip tasks with a live claim — they're being worked on, even if
    # the worker hasn't reported progress yet (run-level liveness
    # extends the claim TTL; we don't want to second-guess that here).
    if _task_field(task, "claim_lock"):
        return []
    assignee = _task_field(task, "assignee") or ""
    if not assignee.strip():
        # Unassigned tasks: the dispatcher's ``skipped_unassigned`` is
        # already the right signal. A separate diagnostic here would
        # double-flag the same condition.
        return []

    # Find the most recent event that put this task into ready.
    # ``created`` covers tasks born ready; ``promoted`` covers parent-
    # done auto-promotion; ``reclaimed`` covers TTL/crash recovery;
    # ``unblocked`` covers human-driven resumes.
    READY_TRANSITION_KINDS = {
        "created", "promoted", "reclaimed", "unblocked",
    }
    last_ready_ts = 0
    for ev in events:
        if _event_kind(ev) in READY_TRANSITION_KINDS:
            t = _event_ts(ev)
            last_ready_ts = max(last_ready_ts, t)

    # Fallback: if no qualifying event exists (very old task or events
    # truncated), fall back to ``created_at`` on the task row. Better
    # to occasionally over-flag an ancient task than miss a stranded one.
    if last_ready_ts == 0:
        last_ready_ts = int(_task_field(task, "created_at", default=0) or 0)
    if last_ready_ts == 0:
        return []

    age_seconds = now - last_ready_ts
    if age_seconds < threshold_seconds:
        return []

    # Format the age in the largest sensible unit.
    if age_seconds >= 3600:
        age_str = f"{age_seconds / 3600:.1f}h"
    else:
        age_str = f"{int(age_seconds / 60)}m"

    # Severity escalates with age. Below 2x threshold = warning;
    # 2x – 6x = error; beyond 6x = critical (something is clearly
    # broken, not just slow).
    if age_seconds >= threshold_seconds * 6:
        severity = "critical"
    elif age_seconds >= threshold_seconds * 2:
        severity = "error"
    else:
        severity = "warning"

    actions = [
        DiagnosticAction(
            kind="reassign",
            label="Reassign to a different worker",
            payload={"current_assignee": assignee},
        ),
        DiagnosticAction(
            kind="cli_hint",
            label="Check dispatcher status",
            payload={"command": "hermes kanban diagnostics"},
        ),
    ]

    return [Diagnostic(
        kind="stranded_in_ready",
        severity=severity,
        title=f"Ready for {age_str} with no worker",
        detail=(
            f"This task has been ready for {age_str} but nothing has "
            f"claimed it. Common causes: assignee {assignee!r} is "
            f"misspelled, the profile was deleted, or the external "
            f"worker pool for this lane is down. Confirm the assignee "
            f"is correct and that a worker is actually polling for it."
        ),
        actions=actions,
        first_seen_at=last_ready_ts,
        last_seen_at=last_ready_ts,
        count=1,
        data={
            "ready_since": last_ready_ts,
            "age_seconds": int(age_seconds),
            "assignee": assignee,
            "threshold_seconds": int(threshold_seconds),
        },
    )]


# Registry — order matters: rules higher on the list render first when
# severity ties. Add new rules here.
_RULES: list[RuleFn] = [
    _rule_hallucinated_cards,
    _rule_triage_aux_unavailable,
    _rule_prose_phantom_refs,
    _rule_repeated_failures,
    _rule_repeated_crashes,
    _rule_stuck_in_blocked,
    _rule_block_unblock_cycling,
    _rule_stranded_in_ready,
]


# Known kinds (for the UI's filter / legend / i18n keys). Update when
# rules are added.
DIAGNOSTIC_KINDS = (
    "hallucinated_cards",
    "triage_aux_unavailable",
    "prose_phantom_refs",
    "repeated_failures",
    "repeated_crashes",
    "stuck_in_blocked",
    "block_unblock_cycling",
    "stranded_in_ready",
    "legacy_review_parent_gates_child",
    "review_handoff_invariant_violation",
    "negative_parent_verdict_released_child",
    "dependency_wait_loop",
    "terminal_task_active_run",
    "duplicate_active_execution",
    "head_superseded",
    "branch_missing",
    "branch_unknown",
    "review_stale",
    "replacement_suppressed",
    "replacement_unproven",
    "workspace_missing",
    "workspace_not_git",
    "workspace_not_linked",
    "workspace_wrong_repo",
    "workspace_wrong_head",
    "workspace_unknown",
    "workspace_retired",
    "workspace_unsafe_tmp",
    "assignee_non_runnable",
    "reconciliation_changed",
)


DEFAULT_CONFIG = {
    # Match the dispatcher default (kanban.failure_limit) so repeated-failure
    # diagnostics do not lag behind the default auto-block threshold.
    "failure_threshold": 2,
    # Legacy alias accepted at read time by _rule_repeated_failures.
    "spawn_failure_threshold": 2,
    "crash_threshold": 2,
    "blocked_stale_hours": 24,
    # Stranded-task threshold. 30 min by default — below that, the
    # signal is dominated by tasks that are about to be claimed on the
    # next dispatcher tick (default 60s) and would just be noise.
    "stranded_threshold_seconds": 30 * 60,
}


def config_from_kanban_config(kanban_cfg: Optional[dict]) -> dict:
    """Build diagnostics config from the runtime ``kanban`` config section.

    ``kanban.diagnostics.failure_threshold`` remains an explicit override.
    Otherwise, derive the repeated-failure threshold from
    ``kanban.failure_limit`` so CLI/dashboard diagnostics match the
    dispatcher's actual circuit-breaker threshold.
    """
    kanban_cfg = kanban_cfg or {}
    diag_cfg = dict(kanban_cfg.get("diagnostics") or {})
    diag_cfg.setdefault(
        "failure_limit",
        kanban_cfg.get("failure_limit", DEFAULT_CONFIG["failure_threshold"]),
    )
    if (
        "failure_threshold" not in diag_cfg
        and "spawn_failure_threshold" not in diag_cfg
    ):
        diag_cfg["failure_threshold"] = diag_cfg["failure_limit"]
    return diag_cfg


def config_from_runtime_config(raw_config: Optional[dict]) -> dict:
    """Build diagnostics config from the full Hermes runtime config.

    Carries through ``kanban``, ``auxiliary``, and ``model`` keys so triage-
    aware rules can inspect the active aux-helper and main-model state.
    Folds the ``kanban`` block through ``config_from_kanban_config`` so the
    repeated-failure threshold derivation still applies.
    """
    raw_config = raw_config or {}
    if not isinstance(raw_config, dict):
        return {}
    cfg: dict = {}
    kanban_cfg = raw_config.get("kanban")
    if isinstance(kanban_cfg, dict):
        cfg.update(config_from_kanban_config(kanban_cfg))
        cfg["kanban"] = kanban_cfg
    for key in ("auxiliary", "model"):
        value = raw_config.get(key)
        if value is not None:
            cfg[key] = value
    return cfg


def compute_task_diagnostics(
    task,
    events: list,
    runs: list,
    *,
    now: Optional[int] = None,
    config: Optional[dict] = None,
    tasks: Optional[Mapping[str, Any]] = None,
    git_probe: Optional[Callable[..., Any]] = None,
    workspace_probe: Optional[Callable[[Any], Any]] = None,
    profile_roster: Any = None,
) -> list[Diagnostic]:
    """Run every rule against a single task's state and return a
    severity-sorted list of active diagnostics.

    Sorting: critical first, then error, then warning; ties broken by
    most-recent ``last_seen_at``.
    """
    now_ts = int(now if now is not None else time.time())
    config = config or {}
    cfg = {**DEFAULT_CONFIG, **config}
    if (
        "failure_threshold" not in config
        and "spawn_failure_threshold" not in config
        and "failure_limit" in config
    ):
        cfg["failure_threshold"] = _positive_int(
            config.get("failure_limit"),
            DEFAULT_CONFIG["failure_threshold"],
        )
    out: list[Diagnostic] = reconciliation_diagnostics(
        task,
        runs,
        tasks=tasks,
        now=now_ts,
        git_probe=git_probe,
        workspace_probe=workspace_probe,
        profile_roster=profile_roster,
    )
    for rule in _RULES:
        try:
            out.extend(rule(task, events, runs, now_ts, cfg))
        except Exception:
            # A broken rule must never crash the dashboard. Rule bugs
            # get caught in tests; in production we'd rather drop the
            # diagnostic than 500 a whole /board request.
            continue
    severity_idx = {s: i for i, s in enumerate(SEVERITY_ORDER)}
    out.sort(
        key=lambda d: (
            -severity_idx.get(d.severity, -1),
            -(d.last_seen_at or 0),
        )
    )
    return out
