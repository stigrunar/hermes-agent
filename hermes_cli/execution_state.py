"""Deterministic execution-state projection for Hermes Kanban.

Repository project artifacts remain the durable source of project truth.  This
module does not write repositories or invent a second workflow database.  It
projects the execution-bearing fields already stored on one Kanban task into a
single typed state so the existing Hermes dispatcher can make the same
currentness/resume decision at every transition boundary.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping, Optional


NON_EXECUTABLE_HYGIENE = frozenset({"obsolete", "superseded"})
_TERMINAL_STATUSES = frozenset({"done", "archived"})
_CONTRACT_RE = re.compile(r"(?mi)^\s*contract_id\s*[:=]\s*([A-Za-z0-9._:/-]+)\s*$")
_REVISION_RE = re.compile(r"(?mi)^\s*revision\s*[:=]\s*([A-Za-z0-9._:/-]+)\s*$")
_TERMINAL_REASON_RE = re.compile(
    r"(?i)(?:iteration\s+budget|iterations?\s+exhausted|max(?:imum)?\s+iterations?"
    r"|goal(?:[- ]mode)?\s+(?:turn|iteration)\s+budget|terminal\s+exhaust(?:ion|ed))"
)


class BlockerType(str, Enum):
    NONE = "none"
    DEPENDENCY = "dependency"
    MACHINE = "machine"
    MANUAL = "manual"
    TERMINAL = "terminal"


class ResumePolicy(str, Enum):
    NONE = "none"
    AUTO_WHEN_RESOLVED = "auto_when_resolved"
    BOUNDED_RETRY = "bounded_retry"
    MANUAL_ONCE = "manual_once"
    NEVER = "never"


@dataclass(frozen=True)
class ReconciledExecutionState:
    task_id: str
    contract_id: str
    revision: str
    blocker_type: BlockerType
    blocked_reason: str
    unblock_condition: str
    unblock_owner: str
    resume_policy: ResumePolicy
    resume_action: str
    retry_count: int
    max_retries: int
    blocker_fingerprint: str
    workspace_path: str
    executable: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "contract_id": self.contract_id,
            "revision": self.revision,
            "blocker_type": self.blocker_type.value,
            "blocked_reason": self.blocked_reason,
            "unblock_condition": self.unblock_condition,
            "unblock_owner": self.unblock_owner,
            "resume_policy": self.resume_policy.value,
            "resume_action": self.resume_action,
            "retry_count": self.retry_count,
            "max_retries": self.max_retries,
            "blocker_fingerprint": self.blocker_fingerprint,
            "workspace_path": self.workspace_path,
            "executable": self.executable,
        }


def is_non_executable_hygiene(value: object) -> bool:
    return str(value or "").strip().casefold() in NON_EXECUTABLE_HYGIENE


def _field(task: Any, name: str, default: Any = None) -> Any:
    if isinstance(task, Mapping):
        return task.get(name, default)
    return getattr(task, name, default)


def _explicit_marker(pattern: re.Pattern[str], body: str) -> Optional[str]:
    match = pattern.search(body or "")
    return match.group(1).strip() if match else None


def resolve_contract_identity(task: Any) -> tuple[str, str]:
    """Return a stable contract id and protected-field revision fingerprint.

    Explicit ``contract_id:`` / ``revision:`` markers win when a project uses
    them.  Legacy cards receive deterministic identities derived from the
    existing task id and protected execution-bearing fields; no schema or
    hidden mutable authority is introduced.
    """
    task_id = str(_field(task, "id", "") or "")
    body = str(_field(task, "body", "") or "")
    contract_id = _explicit_marker(_CONTRACT_RE, body) or f"task:{task_id}"
    explicit_revision = _explicit_marker(_REVISION_RE, body)
    if explicit_revision:
        return contract_id, explicit_revision
    protected = {
        "title": str(_field(task, "title", "") or ""),
        "body": body,
        "assignee": str(_field(task, "assignee", "") or ""),
        "workspace_kind": str(_field(task, "workspace_kind", "") or ""),
        "workspace_path": str(_field(task, "workspace_path", "") or ""),
        "branch_name": str(_field(task, "branch_name", "") or ""),
    }
    digest = hashlib.sha256(
        json.dumps(protected, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:16]
    return contract_id, f"r-{digest}"



def has_explicit_contract_identity(task: Any) -> bool:
    body = str(_field(task, "body", "") or "")
    return bool(_CONTRACT_RE.search(body) and _REVISION_RE.search(body))

def blocker_fingerprint(
    *,
    task_id: str,
    contract_id: str,
    revision: str,
    workspace_path: str,
    block_kind: str,
    reason: str,
) -> str:
    payload = "\0".join(
        [task_id, contract_id, revision, workspace_path, block_kind, reason.strip()]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def build_reconciled_state(
    task: Any,
    *,
    latest_block_reason: str = "",
    dependency_open: bool = False,
    failure_limit: int = 5,
    same_fingerprint_auto_resumes: int = 0,
) -> ReconciledExecutionState:
    task_id = str(_field(task, "id", "") or "")
    status = str(_field(task, "status", "") or "").casefold()
    block_kind = str(_field(task, "block_kind", "") or "").casefold()
    hygiene = str(_field(task, "hygiene_class", "") or "").casefold()
    superseded_by = str(_field(task, "superseded_by", "") or "")
    reason = (latest_block_reason or str(_field(task, "last_failure_error", "") or "")).strip()
    workspace_path = str(_field(task, "workspace_path", "") or "")
    contract_id, revision = resolve_contract_identity(task)
    configured_limit = _field(task, "max_retries", None)
    max_retries = int(configured_limit if configured_limit is not None else failure_limit)
    retry_count = int(_field(task, "consecutive_failures", 0) or 0)
    fp = blocker_fingerprint(
        task_id=task_id,
        contract_id=contract_id,
        revision=revision,
        workspace_path=workspace_path,
        block_kind=block_kind,
        reason=reason,
    )

    recurrence_escalated = (
        status == "triage"
        and int(_field(task, "block_recurrences", 0) or 0) >= 2
        and bool(block_kind)
    )
    if (
        status in _TERMINAL_STATUSES
        or is_non_executable_hygiene(hygiene)
        or bool(superseded_by)
        or recurrence_escalated
    ):
        detail = reason or (
            f"hygiene={hygiene or 'none'} superseded_by={superseded_by or 'none'}"
        )
        return ReconciledExecutionState(
            task_id, contract_id, revision, BlockerType.TERMINAL, detail,
            "new authoritative revision required", "dolly/default",
            ResumePolicy.NEVER, "do_not_resume", retry_count, max_retries, fp,
            workspace_path, False,
        )

    if dependency_open or block_kind == "dependency":
        return ReconciledExecutionState(
            task_id, contract_id, revision, BlockerType.DEPENDENCY, reason,
            "all accepted dependencies are terminal", "hermes-reconciler",
            ResumePolicy.AUTO_WHEN_RESOLVED, "recompute_ready", retry_count,
            max_retries, fp, workspace_path, True,
        )

    if status == "blocked":
        if _TERMINAL_REASON_RE.search(reason) or retry_count >= max_retries:
            return ReconciledExecutionState(
                task_id, contract_id, revision, BlockerType.TERMINAL, reason,
                "new authoritative revision or explicit operator recovery required",
                "dolly/default", ResumePolicy.NEVER, "do_not_resume", retry_count,
                max_retries, fp, workspace_path, False,
            )
        if block_kind == "transient":
            if not has_explicit_contract_identity(task):
                return ReconciledExecutionState(
                    task_id, contract_id, revision, BlockerType.MANUAL, reason,
                    "legacy transient block needs an explicit current contract/revision before retry",
                    "dolly/default", ResumePolicy.MANUAL_ONCE, "wait_for_owner_once",
                    retry_count, max_retries, fp, workspace_path, True,
                )
            # Exactly one automatic retry is allowed for one unchanged blocker
            # fingerprint. A repeated same-fingerprint failure is terminal for
            # automation and stays visible for owner review.
            action = "unblock_same_revision_once" if same_fingerprint_auto_resumes == 0 else "do_not_resume"
            policy = ResumePolicy.BOUNDED_RETRY if same_fingerprint_auto_resumes == 0 else ResumePolicy.NEVER
            return ReconciledExecutionState(
                task_id, contract_id, revision, BlockerType.MACHINE, reason,
                "transient retry window elapsed and task/contract/revision/workspace are unchanged",
                "hermes-reconciler", policy, action,
                same_fingerprint_auto_resumes, 1, fp, workspace_path,
                same_fingerprint_auto_resumes == 0,
            )
        return ReconciledExecutionState(
            task_id, contract_id, revision, BlockerType.MANUAL, reason,
            "named human/capability decision resolves the blocker",
            "dolly/default", ResumePolicy.MANUAL_ONCE, "wait_for_owner_once",
            retry_count, max_retries, fp, workspace_path, True,
        )

    return ReconciledExecutionState(
        task_id, contract_id, revision, BlockerType.NONE, "", "", "",
        ResumePolicy.NONE, "continue", retry_count, max_retries, fp,
        workspace_path, True,
    )

# Optional machine-readable repo-canon marker. The surrounding markdown remains
# human project truth; this one-line comment gives the reconciler a deterministic
# current-revision signal without parsing prose or inventing a second database.
_REPO_STATE_RE = re.compile(
    r"<!--\s*HERMES_EXECUTION_STATE\s+(\{.*?\})\s*-->", re.IGNORECASE
)
_CANON_PATH_RE = re.compile(r"(?mi)^\s*canon_path\s*[:=]\s*(\S+)\s*$")
_ALLOWED_CANON_FILENAMES = frozenset({"TASKS.md", "ROADMAP.md", "BACKLOG.md", "CHANGELOG.md"})


def resolve_canon_path(task: Any) -> Optional[str]:
    body = str(_field(task, "body", "") or "")
    match = _CANON_PATH_RE.search(body)
    if not match:
        return None
    from pathlib import Path
    path = Path(match.group(1)).expanduser()
    if not path.is_absolute() or path.name not in _ALLOWED_CANON_FILENAMES:
        return None
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    if resolved.name not in _ALLOWED_CANON_FILENAMES or not resolved.is_file():
        return None
    return str(resolved)


def read_repo_execution_states(path: str) -> dict[str, dict[str, Any]]:
    from pathlib import Path
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    states: dict[str, dict[str, Any]] = {}
    for match in _REPO_STATE_RE.finditer(text):
        try:
            value = json.loads(match.group(1))
        except json.JSONDecodeError:
            continue
        if not isinstance(value, dict):
            continue
        contract_id = str(value.get("contract_id") or "").strip()
        revision = str(value.get("revision") or "").strip()
        status = str(value.get("status") or "").strip().casefold()
        if not contract_id or not revision or status not in {
            "active", "blocked", "done", "superseded", "cancelled", "archived"
        }:
            continue
        states[contract_id] = value
    return states


def upsert_repo_execution_state(path: str, state: Mapping[str, Any]) -> None:
    """Deterministically update one execution-state marker in project canon.

    This is a repo writer helper for Hermes/direct tooling, never called by the
    Kanban reconciler.  The reconciler is read-only toward the repository and
    remains the sole reconciliation writer toward Kanban.
    """
    from pathlib import Path
    p = Path(path).expanduser().resolve(strict=True)
    if p.name not in _ALLOWED_CANON_FILENAMES or not p.is_file():
        raise ValueError("canon path must be an existing TASKS/ROADMAP/BACKLOG/CHANGELOG markdown file")
    payload = dict(state)
    contract_id = str(payload.get("contract_id") or "").strip()
    revision = str(payload.get("revision") or "").strip()
    status = str(payload.get("status") or "").strip().casefold()
    if not contract_id or not revision:
        raise ValueError("repo execution state requires contract_id and revision")
    if status not in {"active", "blocked", "done", "superseded", "cancelled", "archived"}:
        raise ValueError("invalid repo execution status")
    payload["contract_id"] = contract_id
    payload["revision"] = revision
    payload["status"] = status
    line = "<!-- HERMES_EXECUTION_STATE " + json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ) + " -->"
    text = p.read_text(encoding="utf-8")
    matches = list(_REPO_STATE_RE.finditer(text))
    target = None
    for match in matches:
        try:
            current = json.loads(match.group(1))
        except json.JSONDecodeError:
            continue
        if isinstance(current, dict) and str(current.get("contract_id") or "").strip() == contract_id:
            target = match
            break
    if target is None:
        if text and not text.endswith("\n"):
            text += "\n"
        if "## Hermes execution state" not in text:
            text += "\n## Hermes execution state\n\n"
        text += line + "\n"
    else:
        text = text[: target.start()] + line + text[target.end() :]
    tmp = p.with_name(p.name + ".execution-state.tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(p)
