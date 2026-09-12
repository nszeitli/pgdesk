"""Credential boundary regressions using synthetic secrets and an isolated HTTP transport."""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

import httpx
import pytest
from openai import AsyncOpenAI

import pgdesk.ai as ai_module
from pgdesk.ai import SqlAssistant
from pgdesk.aws_secrets import AwsSecret
from pgdesk.catalog import Catalog
from pgdesk.config import Settings, load_config

REFERENCE = AwsSecret("test-profile", "test-secret", "us-west-2")
SENTINEL = "synthetic-sensitive-value-never-display"


@pytest.mark.parametrize("failure", ["denied", "invalid_json", "timeout"])
def test_secret_failures_do_not_expose_response_payloads(monkeypatch, failure: str) -> None:
    """AWS stderr, malformed JSON and timeouts must not disclose captured secret material."""

    def fail(*args, **kwargs):
        """Simulate distinct AWS CLI failure boundaries containing a synthetic secret marker."""
        if failure == "timeout":
            raise subprocess.TimeoutExpired("aws", 25, output=SENTINEL, stderr=SENTINEL)
        return subprocess.CompletedProcess(
            "aws",
            1 if failure == "denied" else 0,
            stdout=SENTINEL,
            stderr=f"An error occurred (AccessDeniedException): {SENTINEL}",
        )

    monkeypatch.setattr(subprocess, "run", fail)
    with pytest.raises(ValueError) as caught:
        REFERENCE.read()
    assert SENTINEL not in str(caught.value)
    assert caught.value.__suppress_context__ or caught.value.__context__ is None


def test_aws_credentials_cannot_be_combined_with_another_source(tmp_path: Path) -> None:
    """A cluster cannot silently prefer an AWS account over an independently configured DSN."""
    path = tmp_path / ".env"
    path.write_text(
        "PGDESK_CLUSTERS=a\nPGDESK_A_DSN_ENV=DATABASE_DSN\n"
        "PGDESK_A_AWS_PROFILE=test-profile\nPGDESK_A_AWS_REGION=us-west-2\n"
        "PGDESK_A_SECRET_ID=test-secret\n"
    )
    with pytest.raises(ValueError):
        load_config(path)


def test_missing_secret_key_never_falls_back_to_ambient_openai_credentials(monkeypatch) -> None:
    """An explicit secret with a missing field cannot authorize a request using ambient credentials."""
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-unrelated-account-key")
    monkeypatch.setattr(
        AwsSecret, "read", lambda self: {"slack_agent": {"different_field": SENTINEL}}
    )
    sent = []

    def make_client(**kwargs):
        """Use a real SDK client with all attempted HTTP effects captured locally."""

        def respond(request):
            """Fail explicitly if the wrong-account fallback crosses the HTTP boundary."""
            sent.append(request)
            return httpx.Response(
                401, json={"error": {"message": "Unexpected credential fallback"}}
            )

        return AsyncOpenAI(
            **kwargs, http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond))
        )

    monkeypatch.setattr(ai_module, "AsyncOpenAI", make_client)

    async def scenario() -> None:
        """Exercise the public suggestion path, not merely the nested-field helper."""
        assistant = SqlAssistant(secret=REFERENCE, key_path=("slack_agent", "openai_api_key"))
        try:
            with pytest.raises(ValueError):
                await assistant.suggest(Settings(), Catalog((), ()), [], "SELECT 1")
            assert sent == []
        finally:
            await assistant.close()

    asyncio.run(scenario())


def test_concurrent_first_requests_leave_no_unowned_http_clients(monkeypatch) -> None:
    """Simultaneous first requests from two tabs must not lose an initialized transport on close."""
    monkeypatch.setattr(AwsSecret, "read", lambda self: {"key": "synthetic-api-key"})
    clients = []

    def make_client(**kwargs):
        """Capture real SDK transports so cleanup can be observed after concurrent requests."""

        def respond(request):
            """Return a minimal complete Responses reply without leaving the process."""
            body = json.loads(request.content)
            return httpx.Response(
                200,
                json={
                    "id": "resp_synthetic",
                    "object": "response",
                    "created_at": 0,
                    "model": body["model"],
                    "status": "completed",
                    "service_tier": "priority",
                    "output": [
                        {
                            "id": "msg_synthetic",
                            "type": "message",
                            "role": "assistant",
                            "status": "completed",
                            "content": [
                                {
                                    "type": "output_text",
                                    "text": "```sql\nSELECT 1;\n```",
                                    "annotations": [],
                                }
                            ],
                        }
                    ],
                },
            )

        client = AsyncOpenAI(
            **kwargs, http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond))
        )
        clients.append(client)
        return client

    monkeypatch.setattr(ai_module, "AsyncOpenAI", make_client)

    async def scenario() -> None:
        """Create overlapping credential resolution and check every created HTTP client closes."""
        assistant = SqlAssistant(secret=REFERENCE, key_path=("key",))
        try:
            await asyncio.gather(
                *(assistant.suggest(Settings(), Catalog((), ()), [], "SELECT 1") for _ in range(2))
            )
        finally:
            await assistant.close()
        assert all(client.is_closed() for client in clients)

    asyncio.run(scenario())
