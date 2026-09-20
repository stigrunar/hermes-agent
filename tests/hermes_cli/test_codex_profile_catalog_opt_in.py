"""Profile-scoped picker additions for account-approved Codex models."""

from pathlib import Path
from unittest.mock import patch

from agent.model_metadata import strip_codex_context_variant_suffix
from hermes_cli.inventory import build_models_payload, load_picker_context
from hermes_cli.model_switch import switch_model


def _write_config(home: Path, body: str) -> None:
    home.mkdir()
    (home / "config.yaml").write_text(body, encoding="utf-8")


def _codex_models_for(home: Path, monkeypatch) -> list[str]:
    monkeypatch.setenv("HERMES_HOME", str(home))
    payload = build_models_payload(load_picker_context(), max_models=50)
    return next(row["models"] for row in payload["providers"] if row["slug"] == "openai-codex")


def test_codex_catalog_opt_in_is_profile_scoped_and_fail_closed(tmp_path, monkeypatch):
    """A -> B -> A proves config isolation while live discovery keeps omitting Astra."""
    import hermes_cli.model_switch_providers as picker
    from hermes_cli.providers import HERMES_OVERLAYS

    opted_in = tmp_path / "profile-a"
    gated = tmp_path / "profile-b"
    _write_config(
        opted_in,
        """model:\n  provider: openai-codex\n  default: gpt-5.6-sol\nproviders:\n  openai-codex:\n    models:\n      - gpt-6-astra\n      - 42\n      - {id: '   '}\n  openrouter:\n    models: [foreign-provider-only]\n""",
    )
    _write_config(
        gated,
        """model:\n  provider: openai-codex\n  default: gpt-5.6-sol\nproviders:\n  openrouter:\n    models: [gpt-6-astra]\n""",
    )

    monkeypatch.setattr("agent.models_dev.fetch_models_dev", lambda: {})
    monkeypatch.setattr("agent.models_dev.PROVIDER_TO_MODELS_DEV", {})
    monkeypatch.setattr(
        "hermes_cli.providers.HERMES_OVERLAYS",
        {"openai-codex": HERMES_OVERLAYS["openai-codex"]},
    )
    monkeypatch.setattr(picker, "_overlay_has_creds", lambda *_args: True)
    monkeypatch.setattr(
        "hermes_cli.models.cached_provider_model_ids",
        lambda provider, **_kwargs: ["gpt-5.6-sol"] if provider == "openai-codex" else [],
    )

    expected = {"gpt-6-astra", "gpt-6-astra-900k"}
    first = _codex_models_for(opted_in, monkeypatch)
    middle = _codex_models_for(gated, monkeypatch)
    last = _codex_models_for(opted_in, monkeypatch)

    assert expected <= set(first) == set(last)
    assert expected.isdisjoint(middle)
    assert "foreign-provider-only" not in first
    assert all(isinstance(model, str) and model.strip() for model in first)


def test_configured_astra_picker_variant_stays_on_codex_and_normalizes_for_wire():
    configured = {"openai-codex": {"models": ["gpt-6-astra"]}}
    accepted = {"accepted": True, "persist": True, "recognized": True, "message": None}
    with (
        patch("hermes_cli.model_switch.resolve_alias", return_value=None),
        patch(
            "hermes_cli.runtime_provider.resolve_runtime_provider",
            return_value={
                "api_key": "test-token",
                "base_url": "https://chatgpt.com/backend-api/codex",
                "api_mode": "codex_responses",
            },
        ),
        patch("hermes_cli.models_validate.validate_requested_model", return_value=accepted),
        patch("hermes_cli.model_switch.get_model_info", return_value=None),
        patch("hermes_cli.model_switch.get_model_capabilities", return_value=None),
    ):
        result = switch_model(
            raw_input="gpt-6-astra-900k",
            current_provider="openrouter",
            current_model="openai/gpt-5.6-sol",
            explicit_provider="openai-codex",
            user_providers=configured,
        )

    assert result.success is True
    assert result.target_provider == "openai-codex"
    assert result.new_model == "gpt-6-astra-900k"
    assert strip_codex_context_variant_suffix(result.new_model) == "gpt-6-astra"
