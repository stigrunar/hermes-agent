"""Tests for hermes_cli.build_info — baked-in build SHA resolution.

The build SHA is written by the Dockerfile's ``HERMES_GIT_SHA`` build-arg
into ``<project_root>/.hermes_build_sha``.  These tests cover the read-side
helper: missing file, malformed file, truncation, and error tolerance.
"""

import json
from pathlib import Path
from unittest.mock import patch


def test_get_build_sha_returns_none_when_file_absent(tmp_path):
    """Source installs: no file present → None, callers fall back to git."""
    from hermes_cli import build_info

    missing = tmp_path / ".hermes_build_sha"  # never created

    with patch.object(build_info, "_BUILD_SHA_FILE", missing):
        assert build_info.get_build_sha() is None


def test_get_build_sha_respects_short_argument(tmp_path):
    """``short=N`` truncates to N chars; ``short<=0`` returns full SHA."""
    from hermes_cli import build_info

    sha_file = tmp_path / ".hermes_build_sha"
    full_sha = "abcdef1234567890abcdef1234567890abcdef12"
    sha_file.write_text(full_sha + "\n")

    with patch.object(build_info, "_BUILD_SHA_FILE", sha_file):
        assert build_info.get_build_sha(short=12) == "abcdef123456"
        assert build_info.get_build_sha(short=0) == full_sha
        assert build_info.get_build_sha(short=-1) == full_sha


def test_get_code_identity_uses_sealed_private_release_identity_without_git(tmp_path):
    from hermes_cli import build_info

    commit, tree = "a" * 40, "b" * 40
    identity_file = tmp_path / "private-release-identity.json"
    identity_file.write_text(json.dumps({"commit": commit, "tree": tree}))

    with (
        patch.object(build_info, "_PRIVATE_RELEASE_IDENTITY_FILE", identity_file),
        patch.object(build_info, "_resolve_git_head_sha", return_value=None),
        patch.object(build_info, "_BUILD_SHA_FILE", tmp_path / ".hermes_build_sha"),
        patch.object(build_info, "_code_identity_cache", None),
    ):
        identity = build_info.get_code_identity()

    assert identity["sha"] == commit
    assert identity["tree"] == tree
    assert identity["source"] == "private-release"
