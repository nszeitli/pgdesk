"""Automatic SQL authority and shared inference lifetime regressions with synthetic metadata."""

import asyncio

import httpx
import pytest
from openai import AsyncOpenAI

from pgdesk.ai import SqlAssistant
from pgdesk.browsing import BrowsePlan, BrowsePlanner
from pgdesk.catalog import Column, Relation
from pgdesk.config import Cluster, Settings

RELATION = Relation(
    "public",
    "events",
    "r",
    (Column("id", "integer", True, None), Column("label", "text", False, None)),
)


@pytest.mark.parametrize(
    "statement",
    [
        "WITH removed AS (DELETE FROM public.events RETURNING id) SELECT id FROM removed LIMIT 5",
        "SELECT pg_sleep(10) FROM public.events ORDER BY id DESC LIMIT 5",
        "SELECT id FROM private.events ORDER BY id DESC LIMIT 5",
        "SELECT events.id FROM public.events JOIN public.other USING(id) ORDER BY id DESC LIMIT 5",
        "SELECT id FROM public.events ORDER BY id DESC LIMIT (SELECT count(*) FROM public.events)",
        "SELECT id FROM public.events ORDER BY id DESC LIMIT 5; DELETE FROM public.events",
    ],
)
def test_model_recipe_cannot_expand_automatic_sql_authority(statement: str) -> None:
    """Filters/joins/functions/subqueries/mutation cannot cross the model-to-automatic-read seam."""
    with pytest.raises(ValueError):
        BrowsePlan.from_sql(RELATION, statement)


def test_cancelled_borrower_does_not_cancel_shared_inference_and_refresh_reinfers() -> None:
    """A departing tab cannot break another waiter; explicit refresh must obtain a new recipe."""

    async def scenario() -> None:
        """Exercise real SDK transport cancellation, cached use and invalidation in one loop."""
        entered = asyncio.Event()
        release = asyncio.Event()
        calls = 0

        async def respond(request):
            """Hold the response so two real client callers overlap deterministically."""
            nonlocal calls
            calls += 1
            entered.set()
            await release.wait()
            projection = "id" if calls == 1 else "id, label"
            return httpx.Response(
                200,
                json={
                    "id": "resp_synthetic",
                    "object": "response",
                    "created_at": 0,
                    "model": "synthetic",
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
                                    "text": f"SELECT {projection} FROM public.events ORDER BY id DESC LIMIT 100;",
                                    "annotations": [],
                                }
                            ],
                        }
                    ],
                },
            )

        client = AsyncOpenAI(
            api_key="synthetic-key",
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)),
        )
        assistant = SqlAssistant(client)
        planner = BrowsePlanner(assistant)
        cluster = Cluster("synthetic", service="synthetic-service")
        settings = Settings(model="synthetic")
        first = asyncio.create_task(planner.plan(cluster, "database", RELATION, settings))
        second = asyncio.create_task(planner.plan(cluster, "database", RELATION, settings))
        try:
            await asyncio.wait_for(entered.wait(), 3)
            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first
            release.set()
            original = await asyncio.wait_for(second, 3)
            cached = await planner.plan(cluster, "database", RELATION, settings)
            assert original.sql(5) == cached.sql(5)
            assert calls == 1
            planner.invalidate(cluster, "database")
            refreshed = await planner.plan(cluster, "database", RELATION, settings)
            assert '"label"' not in original.sql(5) and '"label"' in refreshed.sql(5)
            assert calls == 2
            planner.invalidate(cluster, "database")
            entered.clear()
            release.clear()
            pending = asyncio.create_task(planner.plan(cluster, "database", RELATION, settings))
            await asyncio.wait_for(entered.wait(), 3)
            await planner.close()
            with pytest.raises(asyncio.CancelledError):
                await pending
        finally:
            release.set()
            await asyncio.gather(first, second, return_exceptions=True)
            await planner.close()
            await assistant.close()
        assert client.is_closed()

    asyncio.run(scenario())
