#!/usr/bin/env python3
"""Canary for Stig's Hindsight OAuth memory route.

Purpose:
- Catch Hermes updates that drop the local Hindsight OAuth bridge.
- Catch Hindsight/client updates that change embedded provider expectations.
- Verify the live config remains tools-only and does not contain API-key fallback.

Default mode is offline/structural and safe for update hooks.
Use --live to perform a redacted synthetic retain/recall through local embedded
Hindsight. The live mode stores only a synthetic canary fact in the canary bank.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
import time
from importlib.metadata import PackageNotFoundError, version

EXPECTED_PROVIDER = "hermes_auxiliary/openai-codex"
EXPECTED_MODEL_PREFIXES = ("gpt-", "o")
SAFE_MEMORY_MODE = "tools"
SECRETISH_KEY_PARTS = ("api_key", "apikey", "secret", "password")
# Treat generic "token" carefully; config legitimately has recall_max_tokens.
SECRETISH_TOKEN_KEYS = ("auth_token", "access_token", "refresh_token", "bearer_token")
TESTED_PACKAGE_RANGES = {
    # Hindsight client 0.6.1 + embed/all 0.7.2 verified on 2026-06-06.
    # Keep this as a warning, not a hard fail, because package metadata can vary
    # across installs and we want the structural checks to carry the hard gate.
    "hindsight-client": ("0.6.1", "0.7.0"),
    "hindsight-embed": ("0.7.2", "0.8.0"),
    "hindsight-all": ("0.7.2", "0.8.0"),
}


def _parse_version_tuple(value: str) -> tuple[int, ...]:
    parts: list[int] = []
    for chunk in value.replace("-", ".").split("."):
        if not chunk.isdigit():
            break
        parts.append(int(chunk))
    return tuple(parts or [0])


def _version_in_range(value: str, lower: str, upper: str) -> bool:
    parsed = _parse_version_tuple(value)
    return _parse_version_tuple(lower) <= parsed < _parse_version_tuple(upper)


def _hermes_home() -> pathlib.Path:
    return pathlib.Path(os.environ.get("HERMES_HOME") or "/home/openclaw/.hermes").expanduser()


def _load_config() -> dict:
    path = _hermes_home() / "hindsight" / "config.json"
    if not path.exists():
        raise SystemExit(f"missing Hindsight config: {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - canary should show exact parse failure
        raise SystemExit(f"failed to parse {path}: {exc}") from exc


def _secretish_keys(config: dict) -> list[str]:
    keys: list[str] = []
    for key in config:
        lower = key.lower()
        if any(part in lower for part in SECRETISH_KEY_PARTS):
            keys.append(key)
            continue
        if any(part in lower for part in SECRETISH_TOKEN_KEYS):
            keys.append(key)
    return keys


def structural_check() -> dict:
    config = _load_config()
    failures: list[str] = []
    warnings: list[str] = []

    provider = config.get("llm_provider")
    model = str(config.get("llm_model") or "")
    if provider != EXPECTED_PROVIDER:
        failures.append(f"llm_provider is {provider!r}, expected {EXPECTED_PROVIDER!r}")
    if not model.startswith(EXPECTED_MODEL_PREFIXES):
        failures.append(f"llm_model {model!r} does not look like a GPT/O-series OAuth model")

    if config.get("memory_mode") != SAFE_MEMORY_MODE:
        failures.append(f"memory_mode is {config.get('memory_mode')!r}, expected {SAFE_MEMORY_MODE!r}")
    for key in ("auto_retain", "auto_recall", "retain_async"):
        if config.get(key) is not False:
            failures.append(f"{key} is {config.get(key)!r}, expected False")

    bad_keys = _secretish_keys(config)
    if bad_keys:
        failures.append(f"secret-like keys present in config: {bad_keys}")

    try:
        from plugins.memory.hindsight import (  # noqa: PLC0415
            HindsightMemoryProvider,
            _HERMES_AUXILIARY_DAEMON_API_KEY,
            _build_embedded_profile_env,
            _ensure_hermes_auxiliary_proxy,
        )
    except Exception as exc:  # noqa: BLE001
        raise SystemExit(f"failed to import Hindsight plugin: {exc}") from exc

    # Start only the localhost proxy in structural mode (no retain/recall); this
    # materializes the loopback base_url that the embedded daemon consumes.
    config["_hermes_auxiliary_base_url"] = _ensure_hermes_auxiliary_proxy(config)
    env_values = _build_embedded_profile_env(config)
    embedded_provider = env_values.get("HINDSIGHT_API_LLM_PROVIDER")
    embedded_key = env_values.get("HINDSIGHT_API_LLM_API_KEY")
    embedded_base_url = env_values.get("HINDSIGHT_API_LLM_BASE_URL", "")
    if embedded_provider != "openai":
        failures.append(f"embedded daemon provider is {embedded_provider!r}, expected 'openai'")
    if embedded_key != _HERMES_AUXILIARY_DAEMON_API_KEY:
        failures.append("embedded daemon key is not the dummy local proxy key")
    if not embedded_base_url.startswith("http://127.0.0.1:"):
        failures.append(f"embedded daemon base_url is not loopback: {embedded_base_url!r}")

    provider_obj = HindsightMemoryProvider()
    provider_obj.initialize(
        session_id="hindsight-oauth-structural-canary",
        platform="cli",
        agent_identity="default",
        agent_workspace="update-canary",
    )
    try:
        if getattr(provider_obj, "_mode", None) != "local_embedded":
            failures.append(f"provider mode is {getattr(provider_obj, '_mode', None)!r}, expected local_embedded")
        if getattr(provider_obj, "_config", {}).get("llm_provider") != EXPECTED_PROVIDER:
            failures.append("provider loaded config does not use Hermes auxiliary OAuth provider")
        if getattr(provider_obj, "_memory_mode", None) != SAFE_MEMORY_MODE:
            failures.append("provider memory mode is not tools")
        if getattr(provider_obj, "_auto_retain", None) is not False:
            failures.append("provider auto_retain is not False")
        if getattr(provider_obj, "_auto_recall", None) is not False:
            failures.append("provider auto_recall is not False")
    finally:
        try:
            provider_obj.shutdown()
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"provider shutdown warning: {type(exc).__name__}: {exc}")

    packages: dict[str, str] = {}
    for package, (lower, upper) in TESTED_PACKAGE_RANGES.items():
        try:
            installed = version(package)
        except PackageNotFoundError:
            packages[package] = "not-installed"
            if package == "hindsight-client":
                failures.append("hindsight-client is not installed")
            else:
                warnings.append(f"{package} is not installed; live embedded smoke may fail")
            continue
        packages[package] = installed
        if not _version_in_range(installed, lower, upper):
            warnings.append(f"{package} {installed} is outside tested range [{lower}, {upper})")

    return {
        "ok": not failures,
        "failures": failures,
        "warnings": warnings,
        "config": {
            "llm_provider": provider,
            "llm_model": model,
            "memory_mode": config.get("memory_mode"),
            "auto_retain": config.get("auto_retain"),
            "auto_recall": config.get("auto_recall"),
            "retain_async": config.get("retain_async"),
            "bank_id": config.get("bank_id"),
        },
        "embedded_env_redacted": {
            "HINDSIGHT_API_LLM_PROVIDER": embedded_provider,
            "HINDSIGHT_API_LLM_MODEL": env_values.get("HINDSIGHT_API_LLM_MODEL"),
            "HINDSIGHT_API_LLM_BASE_URL": embedded_base_url,
            "HINDSIGHT_API_LLM_API_KEY": "<dummy-local-proxy-key>" if embedded_key else "",
        },
        "packages": packages,
    }


def live_smoke(max_attempts: int = 3, delay: float = 3.0) -> dict:
    from plugins.memory.hindsight import HindsightMemoryProvider  # noqa: PLC0415

    stamp = str(int(time.time()))
    anchor = f"CANARY-HERMES-OAUTH-ACTIVE-{stamp}"
    semantic_phrase = "Hindsight should route LLM calls through the Hermes OpenAI Codex OAuth proxy instead of API keys"
    content = (
        f"{anchor}: synthetic redacted activation test. {semantic_phrase}. "
        "No private trace."
    )

    provider_obj = HindsightMemoryProvider()
    provider_obj.initialize(
        session_id=f"hindsight-oauth-live-canary-{stamp}",
        platform="cli",
        agent_identity="default",
        agent_workspace="update-canary-live",
    )
    try:
        retain_raw = provider_obj.handle_tool_call(
            "hindsight_retain",
            {
                "content": content,
                "context": "redacted update canary smoke",
                "tags": ["hindsight-oauth-update-canary", "no-private-trace"],
            },
        )
        retain = json.loads(retain_raw)
        if "error" in retain_raw.lower():
            return {"ok": False, "anchor": anchor, "retain": retain_raw, "recall": ""}

        last_recall = ""
        for attempt in range(1, max_attempts + 1):
            time.sleep(delay)
            recall_raw = provider_obj.handle_tool_call("hindsight_recall", {"query": anchor})
            last_recall = recall_raw
            # Hindsight can consolidate exact IDs away; semantic readback is the
            # intended memory behavior. Exact receipts must live in git/knowledge/Kanban.
            if anchor in recall_raw or semantic_phrase.lower() in recall_raw.lower():
                return {
                    "ok": True,
                    "anchor": anchor,
                    "attempt": attempt,
                    "retain": retain,
                    "recall_contains_exact_anchor": anchor in recall_raw,
                    "recall_contains_semantic_phrase": semantic_phrase.lower() in recall_raw.lower(),
                }
        return {"ok": False, "anchor": anchor, "retain": retain, "recall": last_recall}
    finally:
        try:
            provider_obj.shutdown()
        except Exception:
            pass


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="also run redacted synthetic retain/recall")
    parser.add_argument("--json", action="store_true", help="emit JSON only")
    args = parser.parse_args()

    result = structural_check()
    if args.live and result["ok"]:
        result["live_smoke"] = live_smoke()
        result["ok"] = bool(result["live_smoke"].get("ok"))

    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        status = "PASS" if result["ok"] else "FAIL"
        print(f"Hindsight OAuth canary: {status}")
        print(json.dumps(result, indent=2, sort_keys=True))

    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
