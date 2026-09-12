"""Real PostgreSQL contracts; opt in with PGDESK_TEST_DSN pointing at a disposable local server."""

from __future__ import annotations

import asyncio
import hashlib
import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict

from pgdesk.browsing import BrowsePlan
from pgdesk.config import Cluster, Config
from pgdesk.database import DatabaseSession, QueryCancelled


@pytest.fixture
def session(monkeypatch):
    """Create and remove a uniquely owned synthetic database, refusing remote servers."""
    dsn = os.environ.get("PGDESK_TEST_DSN")
    if not dsn:
        pytest.skip("Set PGDESK_TEST_DSN for disposable localhost PostgreSQL integration tests")
    params = conninfo_to_dict(dsn)
    if params.get("host") not in {"127.0.0.1", "localhost", "::1"}:
        pytest.fail("Database contract tests require an explicit loopback host")
    database = "pgdesk_test_" + uuid.uuid4().hex
    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database)))
    monkeypatch.setenv("PGDESK_TEST_RUNTIME_DSN", dsn)
    workspace = DatabaseSession(
        Cluster("synthetic", dsn_env="PGDESK_TEST_RUNTIME_DSN"),
        database,
        Config(row_limit=3),
        health_interval=0.1,
        reconnect_timeout=1,
    )
    try:
        workspace.pool.wait(10)
        yield workspace
    finally:
        workspace.close()
        with psycopg.connect(dsn, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(database)))


def test_read_only_rollback_and_reset(session: DatabaseSession) -> None:
    """Readonly enforcement, failed writes and session cleanup must survive connection reuse."""
    session.execute("CREATE TABLE sample(id int PRIMARY KEY)", read_only=False)
    with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
        session.execute("INSERT INTO sample VALUES (1)")
    with pytest.raises(psycopg.errors.UniqueViolation):
        session.execute("INSERT INTO sample VALUES (2),(2)", read_only=False)
    assert session.execute("SELECT count(*) FROM sample").rows == ((0,),)
    session.execute("SELECT set_config('search_path', 'pg_catalog', false)")
    # Every available physical connection must be reset, not just the next one by chance.
    for _ in range(4):
        assert session.execute("SELECT current_schema()").rows == (("public",),)
    assert session.execute("INSERT INTO sample VALUES (3) RETURNING id", read_only=False).rows == (
        (3,),
    )


def test_empty_and_truncated_results_preserve_metadata_and_commit(session: DatabaseSession) -> None:
    """Zero-row headers survive and limiting displayed RETURNING rows must not rollback writes."""
    empty = session.execute("SELECT 1 AS retained_header WHERE false")
    assert empty.columns == ("retained_header",)
    assert empty.rows == ()
    session.execute("CREATE TABLE sample(id int)", read_only=False)
    result = session.execute(
        "INSERT INTO sample SELECT generate_series(1,8) RETURNING id", read_only=False
    )
    assert result.truncated
    assert result.rows == ((1,), (2,), (3,))
    assert session.execute("SELECT count(*) FROM sample").rows == ((8,),)


def test_cancellation_releases_query_without_poisoning_pool(session: DatabaseSession) -> None:
    """A cancelled database call returns promptly and the next borrower can execute safely."""
    with ThreadPoolExecutor() as workers:
        future = workers.submit(session.execute, "SELECT pg_sleep(30)")
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            with session.pool.connection() as conn:
                running = conn.execute(
                    "SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() AND query='SELECT pg_sleep(30)' AND state='active'"
                ).fetchone()[0]
            if running:
                break
            time.sleep(0.02)
        assert running == 1
        session.cancel()
        with pytest.raises((psycopg.errors.QueryCanceled, QueryCancelled)):
            future.result(timeout=5)
    assert session.execute("SELECT 7").rows == ((7,),)


def test_terminated_idle_connections_recover_without_user_query(session: DatabaseSession) -> None:
    """The health thread must replace every killed idle backend without a checkout triggering it."""
    with psycopg.connect(
        **session.cluster.connection_kwargs(session.database), autocommit=True
    ) as admin:
        original = {
            row[0]
            for row in admin.execute(
                "SELECT pid FROM pg_stat_activity WHERE datname=current_database() AND pid<>pg_backend_pid()"
            ).fetchall()
        }
        assert len(original) == session.config.pool_size
        for pid in original:
            admin.execute("SELECT pg_terminate_backend(%s)", (pid,))
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            current = {
                row[0]
                for row in admin.execute(
                    "SELECT pid FROM pg_stat_activity WHERE datname=current_database() AND pid<>pg_backend_pid()"
                ).fetchall()
            }
            if len(current) == session.config.pool_size and not original & current:
                break
            time.sleep(0.05)
        assert len(current) == session.config.pool_size
        assert not original & current
    assert session.execute("SELECT 9").rows == ((9,),)


def test_catalog_preserves_empty_schemas_views_and_quoted_names(session: DatabaseSession) -> None:
    """Introspection must include views, empty schemas, constraints and unusual identifiers."""
    session.execute('CREATE SCHEMA "empty schema"', read_only=False)
    session.execute(
        'CREATE TABLE "strange table"("key" int PRIMARY KEY, value text NOT NULL)', read_only=False
    )
    session.execute('CREATE VIEW sample_view AS SELECT value FROM "strange table"', read_only=False)
    catalog = session.catalog()
    assert "empty schema" in catalog.schemas
    table = next(r for r in catalog.relations if r.name == "strange table")
    assert table.columns[1].not_null
    assert "PRIMARY KEY" in table.constraints
    assert next(r for r in catalog.relations if r.name == "sample_view").is_view
    assert session.preview(BrowsePlan.sample(table), 5).result.columns == ("key", "value")


@pytest.mark.parametrize(
    "query",
    [
        "COMMIT",
        "SET search_path=public",
        "COPY (SELECT 1) TO STDOUT",
        "SELECT 1; SELECT 2",
    ],
)
def test_pooled_transaction_commands_are_rejected_before_effects(
    session: DatabaseSession, query: str
) -> None:
    """Session/transaction control cannot escape the one-Run/one-transaction contract."""
    with pytest.raises(ValueError):
        session.execute(query, read_only=False)
    assert session.execute("SELECT 11").rows == ((11,),)


def test_overlapping_shutdown_waits_for_pool_even_if_one_caller_is_cancelled(
    session: DatabaseSession, monkeypatch
) -> None:
    """Tab close racing app exit must share cleanup rather than return with live connections."""
    from pgdesk.ai import SqlAssistant
    from pgdesk.workspace import Workspace

    async def scenario() -> None:
        """Hold cancellation briefly to expose the two public shutdown callers deterministically."""
        workspace = Workspace(session.cluster, session.database, session.config, SqlAssistant())
        workspace.session.pool.wait(10)
        entered = threading.Event()
        release = threading.Event()
        original_cancel = workspace.session.cancel

        def delayed_cancel() -> None:
            """Make the cleanup overlap reproducible without altering its eventual database effect."""
            entered.set()
            release.wait(5)
            original_cancel()

        monkeypatch.setattr(workspace.session, "cancel", delayed_cancel)
        first = asyncio.create_task(workspace.shutdown())
        second = None
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            second = asyncio.create_task(workspace.shutdown())
            await asyncio.sleep(0)
            assert not second.done()
            first.cancel()
            await asyncio.sleep(0)
            assert not second.done()
            release.set()
            await asyncio.wait_for(second, 5)
            assert workspace.session.pool.closed
            assert workspace.session.status().live == 0
        finally:
            release.set()
            await asyncio.gather(first, *([second] if second else []), return_exceptions=True)
            await asyncio.to_thread(workspace.session.close)

    asyncio.run(scenario())


def test_timestamp_browsing_preserves_latest_rows_nulls_and_heavy_values(
    session: DatabaseSession,
) -> None:
    """Timestamp ties use UUID tie-breakers; light values stay narrow and heavy values stay complete."""
    session.config = replace(session.config, row_limit=200)
    session.execute(
        "CREATE TABLE dated(id uuid PRIMARY KEY, created_at timestamptz, label text, payload jsonb)",
        read_only=False,
    )
    session.execute(
        "INSERT INTO dated SELECT md5(n::text)::uuid, "
        "CASE WHEN n % 31 = 0 THEN NULL ELSE timestamptz '2024-01-01 00:00:00+00' + (n / 2) * interval '1 second' END, "
        "CASE WHEN n = 240 THEN repeat('a',1000) ELSE 'row '||n END, "
        "CASE WHEN n % 7 = 0 THEN NULL ELSE jsonb_build_object('text',repeat('x',10000)) END "
        "FROM generate_series(1,240) n",
        read_only=False,
    )
    session.execute("CREATE INDEX ON dated(created_at DESC NULLS LAST, id DESC)", read_only=False)
    session.execute("ANALYZE dated", read_only=False)
    relation = next(r for r in session.catalog().relations if r.name == "dated")
    with pytest.raises(ValueError):
        BrowsePlan.from_sql(relation, "SELECT id FROM public.dated ORDER BY id DESC LIMIT 100")
    plan = BrowsePlan.from_sql(
        relation,
        "SELECT id, created_at, label, payload FROM public.dated ORDER BY created_at DESC, id DESC LIMIT 100",
    )
    light = session.preview(plan, 5)
    heavy = session.preview(plan, 100, heavy=True)
    expected = sorted(
        (
            datetime(2024, 1, 1, tzinfo=UTC) + timedelta(seconds=n // 2),
            uuid.UUID(hashlib.md5(str(n).encode(), usedforsecurity=False).hexdigest()),
            n,
        )
        for n in range(1, 241)
        if n % 31
    )[::-1]
    assert light.ordered and heavy.ordered
    assert [(row[1], row[0]) for row in light.result.rows] == [
        (at, key) for at, key, _ in expected[:5]
    ]
    assert [(row[1], row[0]) for row in heavy.result.rows] == [
        (at, key) for at, key, _ in expected[:100]
    ]
    assert light.result.columns[-1] == "payload__is_null"
    assert [row[3] for row in light.result.rows] == [n % 7 == 0 for _, _, n in expected[:5]]
    assert len(light.result.rows[0][2]) == 160
    assert len(heavy.result.rows[0][2]) == 1000
    assert heavy.result.rows[0][3] == {"text": "x" * 10000}


def test_expensive_latest_sort_becomes_an_explicit_unordered_sample(
    session: DatabaseSession,
) -> None:
    """A large unindexed order cannot masquerade as a cheap latest-row preview."""
    session.config = replace(session.config, row_limit=200)
    session.execute(
        "CREATE TABLE heap AS SELECT n AS id FROM generate_series(1,100000) n", read_only=False
    )
    session.execute("ANALYZE heap", read_only=False)
    relation = next(r for r in session.catalog().relations if r.name == "heap")
    plan = BrowsePlan.from_sql(relation, "SELECT id FROM public.heap ORDER BY id DESC LIMIT 100")
    sample = session.preview(plan, 5)
    assert not sample.ordered
    assert len(sample.result.rows) == 5
    assert session.execute(sample.statement).rows == sample.result.rows
    assert sample.result.rows != tuple((n,) for n in range(100000, 99995, -1))


def test_preview_lock_deadline_releases_its_borrower(session: DatabaseSession) -> None:
    """Automatic table inspection must not wait indefinitely behind DDL or poison the next run."""
    session.execute("CREATE TABLE locked(id integer PRIMARY KEY)", read_only=False)
    relation = next(r for r in session.catalog().relations if r.name == "locked")
    plan = BrowsePlan.from_sql(relation, "SELECT id FROM public.locked ORDER BY id DESC LIMIT 5")
    with session.pool.connection() as blocker, blocker.transaction():
        blocker.execute("LOCK TABLE locked IN ACCESS EXCLUSIVE MODE")
        with ThreadPoolExecutor() as workers:
            future = workers.submit(session.preview, plan, 5)
            with pytest.raises(psycopg.errors.LockNotAvailable):
                future.result(timeout=5)
    assert session.execute("SELECT 17").rows == ((17,),)
