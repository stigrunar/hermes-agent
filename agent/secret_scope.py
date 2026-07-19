"""Profile-scoped credential resolution for multi-profile gateway multiplexing.

The multiplexing gateway serves many profiles from one process. Each profile
has its own ``.env`` with its own provider keys and platform tokens, so we
**cannot** union them into the process-global ``os.environ`` (that would leak
profile A's keys to profile B's turns, and to every subprocess spawned with
``env=dict(os.environ)``).

This module provides a fail-closed, context-local secret scope:

- ``set_secret_scope(mapping)`` installs the active profile's secrets for the
  current task (a contextvar, so it propagates into the agent's worker thread
  via ``copy_context()`` exactly like the HERMES_HOME override).
- ``get_secret(name)`` reads from that scope. When multiplexing is **active**
  and no scope is set, it RAISES rather than silently falling back to
  ``os.environ`` — an un-migrated or newly-added call site fails loud at that
  exact line instead of leaking another profile's value. When multiplexing is
  **off** (the default), it transparently reads ``os.environ`` so the
  single-profile gateway and every non-gateway caller behave exactly as before.

Design rationale lives in ``docs/design/multiplexing-gateway.md`` (Workstream A).
"""
from __future__ import annotations

import os
from contextvars import ContextVar, Token
from pathlib import Path
from typing import Dict, Mapping, Optional


# ── multiplex-active flag ────────────────────────────────────────────────
# Process-global: set once at gateway startup when gateway.multiplex_profiles
# is true. Governs whether get_secret() fails closed on an unscoped read.
# A plain module global (not a contextvar): it describes the deployment mode,
# not a per-task value.
_MULTIPLEX_ACTIVE: bool = False


def set_multiplex_active(active: bool) -> None:
    """Mark whether the process is running as a profile multiplexer.

    Called once at gateway startup. When True, ``get_secret`` fails closed on
    an unscoped read instead of falling back to ``os.environ``.
    """
    global _MULTIPLEX_ACTIVE
    _MULTIPLEX_ACTIVE = bool(active)


def is_multiplex_active() -> bool:
    """Return whether the process is running as a profile multiplexer."""
    return _MULTIPLEX_ACTIVE


# ── the secret scope contextvar ──────────────────────────────────────────
_SECRET_SCOPE: ContextVar[Optional[Mapping[str, str]]] = ContextVar(
    "_SECRET_SCOPE", default=None
)


class UnscopedSecretError(RuntimeError):
    """Raised when a secret is read in multiplex mode with no scope installed.

    This is the fail-closed signal: it means a credential read reached
    ``get_secret`` without a profile scope active, which in a multiplexer would
    otherwise leak whichever profile's value happened to be in ``os.environ``.
    The fix is to wrap the call path in ``set_secret_scope(...)`` (the per-turn
    / per-adapter profile scope), not to widen the allowlist.
    """


def set_secret_scope(secrets: Optional[Mapping[str, str]]) -> Token:
    """Install the active profile's secret mapping for the current context.

    Returns a token for ``reset_secret_scope``. Pass ``None`` to clear.
    """
    return _SECRET_SCOPE.set(secrets)


def reset_secret_scope(token: Token) -> None:
    """Restore the previous secret scope."""
    _SECRET_SCOPE.reset(token)


def current_secret_scope() -> Optional[Mapping[str, str]]:
    """Return the active secret mapping, or None when no scope is installed."""
    return _SECRET_SCOPE.get()


# ── genuinely-global env vars (NOT per-profile secrets) ──────────────────
# These are process/deployment-level settings, not profile credentials. They
# legitimately live in os.environ and must keep reading from it even in
# multiplex mode — routing them through the fail-closed path would wrongly
# crash. Anything matching is read from os.environ regardless of scope.
#
# Membership is exact-name only. Keep this list tight: when in doubt a value
# is profile-scoped, not global; broad prefixes can silently exempt future
# credentials from the multiplex boundary.
_GLOBAL_ENV_EXACT = frozenset({
    # Hermes runtime / deployment
    "HERMES_HOME", "HERMES_PROFILE", "HERMES_GATEWAY_LOCK_DIR",
    "HERMES_MAX_ITERATIONS", "HERMES_MAX_TOKENS", "HERMES_API_TIMEOUT",
    "HERMES_REDACT_SECRETS", "HERMES_NOUS_TIMEOUT_SECONDS",
    "HERMES_NOUS_MIN_KEY_TTL_SECONDS",
    "HERMES_CODEX_REFRESH_TIMEOUT_SECONDS", "HERMES_XAI_REFRESH_TIMEOUT_SECONDS",
    "HERMES_OAUTH_TRACE", "HERMES_SHARED_AUTH_DIR",
    # Trusted hosted-deployment routing. Unlike NOUS_PORTAL_BASE_URL, this is
    # stamped by the container deployment and intentionally wins across
    # profiles (see the staging-token routing contract in hermes_cli.auth).
    "HERMES_PORTAL_BASE_URL",
    "HERMES_COPILOT_ACP_COMMAND", "HERMES_COPILOT_ACP_ARGS",
    "_HERMES_GATEWAY",
    # OS / interpreter
    "PATH", "HOME", "USER", "LANG", "LC_ALL", "TZ", "PWD", "SHELL", "TMPDIR",
    "VIRTUAL_ENV", "PYTHONPATH", "SSL_CERT_FILE", "HERMES_CA_BUNDLE",
    "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "CODEX_HOME", "COPILOT_CLI_PATH",
    "SSH_CLIENT", "SSH_TTY", "BROWSER", "DISPLAY", "WAYLAND_DISPLAY", "LOGNAME",
    "CODESPACES", "CODESPACE_NAME", "GITPOD_WORKSPACE_ID", "CLOUD_SHELL",
    "CLOUD_SHELL_ENVIRONMENT", "REPL_ID", "STACKBLITZ",
    # Process-wide network policy. HTTP clients also consume these directly.
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
    "http_proxy", "https_proxy", "all_proxy", "no_proxy",
    # Terminal backend mechanics. Credential-bearing SSH members and
    # SUDO_PASSWORD are deliberately absent: those belong to the active
    # profile and must resolve through get_secret().
    "TERMINAL_ENV", "TERMINAL_CWD", "TERMINAL_TIMEOUT",
    "TERMINAL_HOME_MODE", "TERMINAL_LIFETIME_SECONDS",
    "TERMINAL_MAX_FOREGROUND_TIMEOUT", "TERMINAL_DISK_WARNING_GB",
    "TERMINAL_MODAL_MODE", "TERMINAL_DOCKER_IMAGE",
    "TERMINAL_DOCKER_FORWARD_ENV", "TERMINAL_SINGULARITY_IMAGE",
    "TERMINAL_MODAL_IMAGE", "TERMINAL_DAYTONA_IMAGE",
    "TERMINAL_CONTAINER_CPU", "TERMINAL_CONTAINER_MEMORY",
    "TERMINAL_CONTAINER_DISK", "TERMINAL_CONTAINER_PERSISTENT",
    "TERMINAL_DOCKER_VOLUMES", "TERMINAL_DOCKER_ENV",
    "TERMINAL_DOCKER_EXTRA_ARGS", "TERMINAL_DOCKER_MOUNT_CWD_TO_WORKSPACE",
    "TERMINAL_DOCKER_NETWORK", "TERMINAL_DOCKER_RUN_AS_HOST_USER",
    "TERMINAL_DOCKER_PERSIST_ACROSS_PROCESSES",
    "TERMINAL_DOCKER_ORPHAN_REAPER", "TERMINAL_SANDBOX_DIR",
    "TERMINAL_PERSISTENT_SHELL", "TERMINAL_SSH_PERSISTENT",
    "TERMINAL_LOCAL_PERSISTENT", "TERMINAL_SECURITY_MODE",
    "TERMINAL_TIMEOUT_GRACE_SECONDS", "TERMINAL_SCRATCH_DIR",
    "TERMINAL_MANAGED_MODAL_CONNECT_TIMEOUT_SECONDS",
    "TERMINAL_MANAGED_MODAL_POLL_READ_TIMEOUT_SECONDS",
    "TERMINAL_MANAGED_MODAL_CANCEL_READ_TIMEOUT_SECONDS",
    # Kanban worker identity, paths, and process tuning (not profile secrets).
    "HERMES_KANBAN_ATTACHMENTS_ROOT", "HERMES_KANBAN_BOARD",
    "HERMES_KANBAN_BRANCH", "HERMES_KANBAN_BUSY_TIMEOUT_MS",
    "HERMES_KANBAN_CLAIM_LOCK", "HERMES_KANBAN_CLAIM_TTL_SECONDS",
    "HERMES_KANBAN_CRASH_GRACE_SECONDS", "HERMES_KANBAN_DB",
    "HERMES_KANBAN_DISPATCH_IN_GATEWAY", "HERMES_KANBAN_GOAL_MAX_TURNS",
    "HERMES_KANBAN_GOAL_MODE", "HERMES_KANBAN_HOME",
    "HERMES_KANBAN_LOGS_ROOT", "HERMES_KANBAN_RATE_LIMIT_COOLDOWN_SECONDS",
    "HERMES_KANBAN_ROOT", "HERMES_KANBAN_RUN_ID",
    "HERMES_KANBAN_SPECIFY_MAX_TOKENS", "HERMES_KANBAN_STOP_NUDGE",
    "HERMES_KANBAN_TASK", "HERMES_KANBAN_WORKSPACE",
    "HERMES_KANBAN_WORKSPACES_ROOT",
    # Telegram transport tuning. Bot tokens/allowlists are deliberately absent.
    "HERMES_TELEGRAM_DISABLE_FALLBACK_IPS",
    "HERMES_TELEGRAM_FOLLOWUP_GRACE_SECONDS",
    "HERMES_TELEGRAM_HTTP_CONNECT_TIMEOUT", "HERMES_TELEGRAM_HTTP_POOL_SIZE",
    "HERMES_TELEGRAM_HTTP_POOL_TIMEOUT", "HERMES_TELEGRAM_HTTP_READ_TIMEOUT",
    "HERMES_TELEGRAM_HTTP_WRITE_TIMEOUT", "HERMES_TELEGRAM_INIT_TIMEOUT",
    "HERMES_TELEGRAM_MEDIA_BATCH_DELAY_SECONDS",
    "HERMES_TELEGRAM_NOTIFICATIONS", "HERMES_TELEGRAM_TEXT_BATCH_DELAY_SECONDS",
    "HERMES_TELEGRAM_TEXT_BATCH_SPLIT_DELAY_SECONDS",
})

def get_deployment_env(
    name: str, default: Optional[str] = None
) -> Optional[str]:
    """Read an explicitly classified process/deployment-level value.

    This accessor is intentionally exact-name allowlisted. Provider keys,
    provider-selection signals, and provider endpoint overrides must use
    :func:`get_secret` instead; accepting an arbitrary name here would recreate
    the ambient multiplex bypass this module exists to prevent.
    """
    if name not in _GLOBAL_ENV_EXACT:
        raise ValueError(
            f"{name!r} is profile-scoped and cannot be read through the "
            "deployment-global environment accessor"
        )
    value = os.environ.get(name)
    return value if value is not None else default


def get_secret(name: str, default: Optional[str] = None) -> Optional[str]:
    """Resolve a credential by env-var name, honoring the active profile scope.

    Resolution order:

    1. Exact-allowlisted global vars always read ``os.environ`` —
       they are deployment settings, not profile secrets.
    2. When a secret scope is installed (multiplexed turn), read from it; an
       absent key returns ``default``. The scope is authoritative — we do NOT
       fall through to ``os.environ``, because in a multiplexer ``os.environ``
       may hold another profile's value.
    3. No scope installed:
       - multiplex INACTIVE (default deployment): read ``os.environ`` —
         identical to the legacy ``os.getenv`` behavior every caller had before.
       - multiplex ACTIVE: FAIL CLOSED. Raise ``UnscopedSecretError`` so the
         missing scope is caught loudly instead of leaking a cross-profile value.
    """
    if name in _GLOBAL_ENV_EXACT:
        return get_deployment_env(name, default)
    scope = _SECRET_SCOPE.get()
    if scope is not None:
        val = scope.get(name)
        return val if val is not None else default

    if _MULTIPLEX_ACTIVE:
        raise UnscopedSecretError(
            f"get_secret({name!r}) called with no profile secret scope active "
            f"while multiplexing is on. This credential read must run inside a "
            f"set_secret_scope(...) block (the per-turn / per-adapter profile "
            f"scope). Reading os.environ here would risk leaking another "
            f"profile's value. See docs/design/multiplexing-gateway.md "
            f"(Workstream A)."
        )

    val = os.environ.get(name)
    return val if val is not None else default


def load_env_file(env_path: Path) -> Dict[str, str]:
    """Parse a ``.env`` file into a plain dict WITHOUT touching ``os.environ``.

    Used to load a profile's secrets into an isolated mapping for
    ``set_secret_scope``. Mirrors python-dotenv's basic parsing (KEY=VALUE,
    ``export`` prefix, ``#`` comments, optional matching quotes) but never
    mutates the process environment — that isolation is the whole point.
    """
    secrets: Dict[str, str] = {}
    try:
        text = env_path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError, UnicodeDecodeError):
        return secrets

    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        secrets[key] = value

    return secrets


def build_profile_secret_scope(hermes_home: Path) -> Dict[str, str]:
    """Build a profile's complete secret mapping without mutating the process.

    The profile's ``.env`` and gitignored ``.op.env`` bootstrap file seed an
    isolated mapping. Enabled external secret sources are then resolved into
    that mapping under a temporary scope, so their auth also comes from this
    profile rather than from another profile's process-global environment.
    Genuinely global vars are intentionally not copied in — ``get_secret``
    reads those from ``os.environ`` directly.
    """
    home = Path(hermes_home)
    secrets = load_env_file(home / ".env")
    for key, value in load_env_file(home / ".op.env").items():
        secrets.setdefault(key, value)

    # SSH connection identity and sudo authentication are profile-owned even
    # though the terminal backend's mechanical settings are deployment-wide.
    # Config.yaml historically stores these fields, so project them into the
    # same isolated transport as .env without mutating os.environ.
    try:
        import yaml

        raw_config = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8")) or {}
        terminal = raw_config.get("terminal") if isinstance(raw_config, dict) else None
        if isinstance(terminal, dict):
            for config_key, env_name in (
                ("ssh_host", "TERMINAL_SSH_HOST"),
                ("ssh_user", "TERMINAL_SSH_USER"),
                ("ssh_port", "TERMINAL_SSH_PORT"),
                ("ssh_key", "TERMINAL_SSH_KEY"),
                ("sudo_password", "SUDO_PASSWORD"),
            ):
                if config_key in terminal and terminal[config_key] is not None:
                    secrets[env_name] = str(terminal[config_key])
    except (FileNotFoundError, OSError, UnicodeDecodeError, yaml.YAMLError):
        pass

    try:
        from hermes_cli.env_loader import _load_secrets_config
        from agent.secret_sources.registry import apply_all

        source_cfg = _load_secrets_config(home)
    except Exception:
        source_cfg = {}

    if source_cfg:
        token = set_secret_scope(secrets)
        try:
            apply_all(source_cfg, home, environ=secrets)
        finally:
            reset_secret_scope(token)
    return secrets
