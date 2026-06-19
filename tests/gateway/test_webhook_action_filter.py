"""Tests for webhook route action prefilters.

The action filter prevents noisy PR label/admin events from reaching prompt
rendering, skill loading, or the agent loop. That matters for cost: returning
[SILENT] after an LLM call is still a quota-burning run.
"""

import asyncio
import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import PlatformConfig
from gateway.platforms.webhook import WebhookAdapter, _INSECURE_NO_AUTH


def _make_adapter(routes) -> WebhookAdapter:
    config = PlatformConfig(
        enabled=True,
        extra={"host": "127.0.0.1", "port": 0, "routes": routes},
    )
    return WebhookAdapter(config)


def _create_app(adapter: WebhookAdapter) -> web.Application:
    app = web.Application()
    app.router.add_post("/webhooks/{route_name}", adapter._handle_webhook)
    return app


@pytest.mark.asyncio
async def test_ignored_action_bypasses_prompt_and_agent(monkeypatch):
    routes = {
        "github-pr": {
            "secret": _INSECURE_NO_AUTH,
            "events": ["pull_request"],
            "ignored_actions": ["labeled", "unlabeled"],
            "skills": ["github-workflows"],
            "prompt": "PR {pull_request.number}: {pull_request.title}",
        }
    }
    adapter = _make_adapter(routes)

    handle_message_calls = []

    async def _capture(event):
        handle_message_calls.append(event)

    adapter.handle_message = _capture

    # If prompt rendering/skill injection is reached this test should fail;
    # ignored actions must return before either cost-bearing path.
    monkeypatch.setattr(
        adapter,
        "_render_prompt",
        lambda *args, **kwargs: pytest.fail("prompt rendering should be bypassed"),
    )

    app = _create_app(adapter)
    body = json.dumps(
        {
            "action": "labeled",
            "pull_request": {"number": 12, "title": "Example"},
        }
    ).encode()

    async with TestClient(TestServer(app)) as cli:
        resp = await cli.post(
            "/webhooks/github-pr",
            data=body,
            headers={
                "Content-Type": "application/json",
                "X-GitHub-Event": "pull_request",
                "X-GitHub-Delivery": "ignored-action-1",
            },
        )
        assert resp.status == 200
        data = await resp.json()
        assert data == {
            "status": "ignored",
            "event": "pull_request",
            "action": "labeled",
        }

    await asyncio.sleep(0.05)
    assert handle_message_calls == []
