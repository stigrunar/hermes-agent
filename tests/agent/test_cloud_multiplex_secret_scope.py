"""Hostile-ambient regressions for cloud SDK credential boundaries."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from agent import secret_scope as ss


@pytest.fixture(autouse=True)
def _reset_multiplex():
    ss.set_multiplex_active(False)
    yield
    ss.set_multiplex_active(False)


def test_bedrock_clients_are_explicit_and_separated_by_profile(monkeypatch):
    from agent import bedrock_adapter as bedrock

    created = []

    class FakeBoto3:
        def client(self, service_name, **kwargs):
            client = SimpleNamespace(service=service_name, kwargs=kwargs)
            created.append(client)
            return client

    monkeypatch.setattr(bedrock, "_require_boto3", lambda: FakeBoto3())
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "synthetic-hostile-aws-access")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "synthetic-hostile-aws-secret")
    bedrock.reset_client_cache()
    ss.set_multiplex_active(True)

    runtime_clients = []
    control_clients = []
    for suffix in ("a", "b"):
        token = ss.set_secret_scope(
            {
                "AWS_ACCESS_KEY_ID": f"synthetic-profile-{suffix}-aws-access",
                "AWS_SECRET_ACCESS_KEY": f"synthetic-profile-{suffix}-aws-secret",
                "AWS_SESSION_TOKEN": f"synthetic-profile-{suffix}-aws-session",
            }
        )
        try:
            runtime_clients.append(
                bedrock._get_bedrock_runtime_client("eu-north-1")
            )
            control_clients.append(
                bedrock._get_bedrock_control_client("eu-north-1")
            )
        finally:
            ss.reset_secret_scope(token)

    assert runtime_clients[0] is not runtime_clients[1]
    assert control_clients[0] is not control_clients[1]
    assert [c.kwargs["aws_access_key_id"] for c in created] == [
        "synthetic-profile-a-aws-access",
        "synthetic-profile-a-aws-access",
        "synthetic-profile-b-aws-access",
        "synthetic-profile-b-aws-access",
    ]
    assert all(c.kwargs["region_name"] == "eu-north-1" for c in created)


def test_bedrock_runtime_rejects_ambient_chain_and_region(monkeypatch):
    from hermes_cli.auth import AuthError
    from hermes_cli.runtime_provider import resolve_runtime_provider

    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "synthetic-hostile-aws-access")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "synthetic-hostile-aws-secret")
    monkeypatch.setenv("AWS_REGION", "hostile-region-1")
    ss.set_multiplex_active(True)
    token = ss.set_secret_scope({})
    try:
        with pytest.raises(AuthError, match="profile-owned"):
            resolve_runtime_provider(requested="bedrock")
    finally:
        ss.reset_secret_scope(token)


def test_anthropic_bedrock_receives_profile_credentials(monkeypatch):
    from agent import anthropic_adapter

    observed = {}
    sdk = SimpleNamespace(
        AnthropicBedrock=lambda **kwargs: observed.update(kwargs) or object()
    )
    monkeypatch.setattr(anthropic_adapter, "_get_anthropic_sdk", lambda: sdk)
    ss.set_multiplex_active(True)
    token = ss.set_secret_scope(
        {
            "AWS_ACCESS_KEY_ID": "synthetic-profile-aws-access",
            "AWS_SECRET_ACCESS_KEY": "synthetic-profile-aws-secret",
            "AWS_SESSION_TOKEN": "synthetic-profile-aws-session",
        }
    )
    try:
        anthropic_adapter.build_anthropic_bedrock_client("ap-southeast-2")
    finally:
        ss.reset_secret_scope(token)

    assert observed["aws_access_key"] == "synthetic-profile-aws-access"
    assert observed["aws_secret_key"] == "synthetic-profile-aws-secret"
    assert observed["aws_session_token"] == "synthetic-profile-aws-session"
    assert observed["aws_region"] == "ap-southeast-2"


def test_azure_service_principal_cache_is_profile_safe(monkeypatch):
    from agent import azure_identity_adapter as azure

    created = []

    def client_secret_credential(**kwargs):
        credential = SimpleNamespace(kwargs=kwargs)
        created.append(credential)
        return credential

    fake = SimpleNamespace(ClientSecretCredential=client_secret_credential)
    monkeypatch.setattr(azure, "_require_azure_identity", lambda: fake)
    monkeypatch.setenv("AZURE_CLIENT_SECRET", "synthetic-hostile-azure-secret")
    azure.reset_credential_cache()
    ss.set_multiplex_active(True)

    credentials = []
    for suffix in ("a", "b"):
        token = ss.set_secret_scope(
            {
                "AZURE_TENANT_ID": f"synthetic-profile-{suffix}-tenant",
                "AZURE_CLIENT_ID": f"synthetic-profile-{suffix}-client",
                "AZURE_CLIENT_SECRET": f"synthetic-profile-{suffix}-secret",
            }
        )
        try:
            config = azure.EntraIdentityConfig.from_active_scope()
            credentials.append(azure.build_credential(config))
        finally:
            ss.reset_secret_scope(token)

    assert credentials[0] is not credentials[1]
    assert [c.kwargs["client_secret"] for c in created] == [
        "synthetic-profile-a-secret",
        "synthetic-profile-b-secret",
    ]


def test_azure_timeout_probe_carries_profile_context(monkeypatch):
    from agent import azure_identity_adapter as azure

    seen = []

    class Credential:
        def get_token(self, _scope):
            seen.append(ss.get_secret("AZURE_CLIENT_SECRET"))
            return SimpleNamespace(token="synthetic-token")

    monkeypatch.setattr(azure, "has_azure_identity_installed", lambda: True)
    monkeypatch.setattr(azure, "build_credential", lambda _config: Credential())
    ss.set_multiplex_active(True)
    token = ss.set_secret_scope(
        {"AZURE_CLIENT_SECRET": "synthetic-profile-timeout-secret"}
    )
    try:
        assert azure.has_azure_identity_credentials(
            config=azure.EntraIdentityConfig(), timeout_seconds=1.0
        )
    finally:
        ss.reset_secret_scope(token)

    assert seen == ["synthetic-profile-timeout-secret"]


def test_azure_status_uses_profile_identity_not_hostile_ambient(monkeypatch):
    from agent import azure_identity_adapter as azure

    class Credential:
        def get_token(self, _scope):
            return SimpleNamespace(token="synthetic-profile-token", expires_on=123)

    monkeypatch.setattr(azure, "has_azure_identity_installed", lambda: True)
    monkeypatch.setattr(azure, "build_credential", lambda _config: Credential())
    monkeypatch.setenv("AZURE_TENANT_ID", "synthetic-hostile-tenant")
    monkeypatch.setenv("AZURE_CLIENT_ID", "synthetic-hostile-client")
    monkeypatch.setenv("AZURE_CLIENT_SECRET", "synthetic-hostile-secret")
    monkeypatch.setenv("IDENTITY_ENDPOINT", "https://synthetic-hostile-msi.invalid")
    ss.set_multiplex_active(True)
    token = ss.set_secret_scope(
        {
            "AZURE_TENANT_ID": "synthetic-profile-tenant",
            "AZURE_CLIENT_ID": "synthetic-profile-client",
            "AZURE_CLIENT_SECRET": "synthetic-profile-secret",
        }
    )
    try:
        info = azure.describe_active_credential(timeout_seconds=1.0)
    finally:
        ss.reset_secret_scope(token)

    assert info["ok"] is True
    assert info["tenant_id_env"] == "synthetic-profile-tenant"
    assert info["env_sources"] == ["EnvironmentCredential (client secret)"]


def test_azure_status_propagates_unscoped_boundary(monkeypatch):
    from agent import azure_identity_adapter as azure

    monkeypatch.setattr(azure, "has_azure_identity_installed", lambda: True)
    monkeypatch.setenv("AZURE_CLIENT_SECRET", "synthetic-hostile-secret")
    ss.set_multiplex_active(True)

    with pytest.raises(ss.UnscopedSecretError):
        azure.describe_active_credential(timeout_seconds=1.0)


def test_azure_rejects_default_chain_in_multiplex(monkeypatch):
    from agent import azure_identity_adapter as azure

    monkeypatch.setenv("AZURE_CLIENT_SECRET", "synthetic-hostile-azure-secret")
    ss.set_multiplex_active(True)
    token = ss.set_secret_scope({})
    try:
        with pytest.raises(azure.AzureCredentialScopeError, match="ambient chain"):
            azure.EntraIdentityConfig.from_active_scope()
    finally:
        ss.reset_secret_scope(token)


def test_azure_does_not_reuse_legacy_default_chain_after_multiplex(monkeypatch):
    from agent import azure_identity_adapter as azure

    legacy = object()
    fake = SimpleNamespace(DefaultAzureCredential=lambda **_kwargs: legacy)
    monkeypatch.setattr(azure, "_require_azure_identity", lambda: fake)
    azure.reset_credential_cache()
    config = azure.EntraIdentityConfig()
    assert azure.build_credential(config) is legacy

    ss.set_multiplex_active(True)
    token = ss.set_secret_scope({})
    try:
        with pytest.raises(azure.AzureCredentialScopeError):
            azure.build_credential(config)
    finally:
        ss.reset_secret_scope(token)


def test_vertex_service_accounts_are_profile_cached_and_adc_is_disabled(
    monkeypatch, tmp_path
):
    from agent import vertex_adapter as vertex

    paths = []
    adc_calls = []

    class Credentials:
        def __init__(self, path):
            self.token = f"synthetic-token-{path.name}"
            self.project_id = f"synthetic-project-{path.stem}"
            self.expired = False
            self.expiry = datetime.now(timezone.utc) + timedelta(hours=1)

    def from_file(path, **_kwargs):
        resolved = tmp_path / str(path).split("/")[-1]
        paths.append(str(path))
        return Credentials(resolved)

    monkeypatch.setattr(
        vertex,
        "service_account",
        SimpleNamespace(Credentials=SimpleNamespace(from_service_account_file=from_file)),
    )
    monkeypatch.setattr(
        vertex,
        "google",
        SimpleNamespace(
            auth=SimpleNamespace(
                default=lambda **_kwargs: adc_calls.append(True) or (None, None),
                transport=SimpleNamespace(requests=SimpleNamespace(Request=object)),
            )
        ),
    )
    monkeypatch.setenv(
        "GOOGLE_APPLICATION_CREDENTIALS", str(tmp_path / "hostile.json")
    )
    vertex._creds_cache.clear()
    ss.set_multiplex_active(True)

    results = []
    for suffix in ("a", "b"):
        path = tmp_path / f"profile-{suffix}.json"
        path.write_text("{}", encoding="utf-8")
        token = ss.set_secret_scope({"VERTEX_CREDENTIALS_PATH": str(path)})
        try:
            results.append(vertex.get_vertex_credentials())
        finally:
            ss.reset_secret_scope(token)

    assert results[0] != results[1]
    assert paths == [str(tmp_path / "profile-a.json"), str(tmp_path / "profile-b.json")]

    token = ss.set_secret_scope({})
    try:
        assert vertex.get_vertex_credentials() == (None, None)
    finally:
        ss.reset_secret_scope(token)
    assert adc_calls == []
