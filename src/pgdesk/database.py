"""Per-workspace PostgreSQL pool, active health maintenance and non-replayed execution."""

from __future__ import annotations

import threading
import time
import weakref
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

import psycopg
from pglast import ast, parse_sql
from pglast.parser import ParseError
from psycopg_pool import ConnectionPool

from pgdesk.catalog import CATALOG_SQL, SCHEMAS_SQL, Catalog, QueryResult, build_catalog, cell_text
from pgdesk.config import Cluster, Config


class QueryCancelled(Exception):
    """The user cancelled before or during a statement; no automatic replay is permitted."""


@dataclass(frozen=True)
class PoolStatus:
    """Measured client pool state; payload counters explicitly exclude protocol overhead."""

    live: int
    idle: int
    target: int
    oldest_seconds: float
    uptime_seconds: float
    last_check_seconds: float | None
    errors: int
    sent_bytes: int
    received_bytes: int
    state: str


class DatabaseSession:
    """Own a tab's connections and maintenance thread until explicit close.

    One user statement at a time, separate metadata borrower, fixed warm pool.
    Borrowing and SQL execution happen off the UI thread. Queries are never retried;
    only connection creation and health maintenance are retried indefinitely.
    """

    def __init__(
        self,
        cluster: Cluster,
        database: str,
        config: Config,
        *,
        health_interval: float = 5,
        reconnect_timeout: float = 30,
    ) -> None:
        """Start warming connections without waiting for the database to be reachable."""
        self.cluster = cluster
        self.database = database
        self.config = config
        self._started = time.monotonic()
        self._stop = threading.Event()
        self._cancel = threading.Event()
        self._query_lock = threading.Lock()
        self._state_lock = threading.RLock()
        self._active: psycopg.Connection | None = None
        self._connections: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()
        self._last_check: float | None = None
        self._maintenance_error = False
        self._sent = 0
        self._received = 0
        self._health_interval = health_interval
        self.pool = ConnectionPool(
            kwargs=self._connection_kwargs,
            min_size=config.pool_size,
            max_size=config.pool_size,
            open=False,
            configure=self._configure,
            check=ConnectionPool.check_connection,
            reset=self._reset,
            timeout=8,
            max_waiting=4,
            reconnect_timeout=reconnect_timeout,
            max_lifetime=3600,
            name="pgdesk",
        )
        self.pool.open()
        self._maintenance = threading.Thread(
            target=self._maintain, name="pgdesk-health", daemon=True
        )
        self._maintenance.start()

    def _connection_kwargs(self) -> dict:
        """Resolve current credentials for each reconnect, with bounded server operations."""
        kwargs = self.cluster.connection_kwargs(self.database)
        kwargs.update(autocommit=True, prepare_threshold=None)
        return kwargs

    def _configure(self, conn: psycopg.Connection) -> None:
        """Install baseline timeouts and record successful physical connection creation."""
        conn.execute(
            "SELECT set_config('statement_timeout', %s, false)",
            (str(self.config.statement_timeout_seconds * 1000),),
        )
        with self._state_lock:
            self._connections[conn] = time.monotonic()

    def _reset(self, conn: psycopg.Connection) -> None:
        """Remove session settings, temp objects and advisory locks before another borrower."""
        conn.execute("DISCARD ALL")
        conn.execute(
            "SELECT set_config('statement_timeout', %s, false)",
            (str(self.config.statement_timeout_seconds * 1000),),
        )

    def _maintain(self) -> None:
        """Keep health checks running even after a complete reconnect window is exhausted."""
        while not self._stop.is_set():
            try:
                # check() grows a depleted pool even after reconnect_timeout gave up.
                self.pool.check()
                with self._state_lock:
                    self._last_check = time.monotonic()
                    self._maintenance_error = False
            except Exception:
                with self._state_lock:
                    self._maintenance_error = True
            self._stop.wait(self._health_interval)

    def status(self) -> PoolStatus:
        """Return a lock-consistent status without issuing database queries."""
        now = time.monotonic()
        stats = self.pool.get_stats()
        with self._state_lock:
            ages = [now - born for conn, born in self._connections.items() if not conn.closed]
            live = len(ages)
            state = (
                "CLOSED"
                if self._stop.is_set()
                else "READY"
                if live >= self.config.pool_size
                else "RECONNECTING"
            )
            if self._maintenance_error:
                state = "HEALTH CHECK RETRY"
            return PoolStatus(
                live,
                stats.get("pool_available", 0),
                self.config.pool_size,
                max(ages, default=0),
                now - self._started,
                None if self._last_check is None else now - self._last_check,
                stats.get("connections_errors", 0) + stats.get("connections_lost", 0),
                self._sent,
                self._received,
                state,
            )

    def catalog(self) -> Catalog:
        """Read a complete accessible catalog in a short independent read-only transaction."""
        with self.pool.connection() as conn, conn.transaction():
            conn.execute("SET TRANSACTION READ ONLY")
            schemas = conn.execute(SCHEMAS_SQL).fetchall()
            rows = conn.execute(CATALOG_SQL).fetchall()
        return build_catalog(schemas, rows)

    @contextmanager
    def _execution(self) -> Iterator[psycopg.Connection]:
        """Serialize user runs and hold cancellation ownership until connection return."""
        if not self._query_lock.acquire(blocking=False):
            raise ValueError("A query is already running in this tab")
        try:
            if self._stop.is_set():
                raise QueryCancelled("Workspace is closed")
            self._cancel.clear()
            with self.pool.connection() as conn:
                with self._state_lock:
                    self._active = conn
                try:
                    if self._cancel.is_set() or self._stop.is_set():
                        raise QueryCancelled("Query cancelled before execution")
                    yield conn
                finally:
                    with self._state_lock:
                        self._active = None
        finally:
            self._query_lock.release()

    def execute(self, statement: str, read_only: bool = True) -> QueryResult:
        """Run one statement atomically; rollback on failure and never retry uncertain effects."""
        try:
            parsed = parse_sql(statement)
        except ParseError as error:
            raise ValueError(f"SQL syntax: {error}") from None
        if len(parsed) != 1:
            raise ValueError("Run one SQL statement at a time")
        if isinstance(parsed[0].stmt, (ast.TransactionStmt, ast.CopyStmt, ast.VariableSetStmt)):
            raise ValueError(
                "Transaction control, SET/RESET and COPY are not supported in pooled runs; use CSV export"
            )
        started = time.monotonic()
        with self._execution() as conn, conn.transaction():
            conn.execute("SET TRANSACTION READ ONLY" if read_only else "SET TRANSACTION READ WRITE")
            with self._state_lock:
                self._sent += len(statement.encode("utf-8"))
            with conn.cursor() as cursor:
                cursor.execute(statement, prepare=False)
                columns = tuple(column.name for column in cursor.description or ())
                rows = cursor.fetchmany(self.config.row_limit + 1) if cursor.description else []
                truncated = len(rows) > self.config.row_limit
                if truncated:
                    rows.pop()
                status = cursor.statusmessage or "Statement completed"
            if self._cancel.is_set():
                raise QueryCancelled("Query cancelled; transaction rolled back")
        result = QueryResult(columns, tuple(rows), status, time.monotonic() - started, truncated)
        with self._state_lock:
            self._received += sum(
                len(cell_text(value).encode("utf-8")) for row in result.rows for value in row
            )
        return result

    def cancel(self) -> None:
        """Cancel the current operation only, synchronized against connection return/reuse."""
        self._cancel.set()
        with self._state_lock:
            if self._active is not None:
                self._active.cancel_safe(timeout=5)

    def close(self) -> None:
        """Stop new work, cancel the current query, and close physical pool resources."""
        self._stop.set()
        try:
            self.cancel()
        finally:
            self._maintenance.join(timeout=20)
            self.pool.close(timeout=10)


def list_databases(cluster: Cluster) -> tuple[str, ...]:
    """Discover connectable databases using a short-lived maintenance connection."""
    if cluster.databases:
        return cluster.databases
    with psycopg.connect(
        **cluster.connection_kwargs(cluster.maintenance_database), autocommit=True
    ) as conn:
        conn.execute("SET statement_timeout = '10s'")
        rows = conn.execute(
            "SELECT datname FROM pg_database WHERE datallowconn AND NOT datistemplate AND has_database_privilege(oid, 'CONNECT') ORDER BY datname"
        ).fetchall()
    return tuple(row[0] for row in rows)


def error_text(error: Exception) -> str:
    """Show SQL diagnostics, but never connection strings, server endpoints or API bodies."""
    if isinstance(error, psycopg.Error):
        if error.sqlstate:
            return f"{error.sqlstate}: {error.diag.message_primary or type(error).__name__}"
        return f"{type(error).__name__}: connection unavailable; pool will keep reconnecting. Query was not retried."
    if isinstance(error, (ValueError, QueryCancelled)):
        return str(error)
    return f"{type(error).__name__}: operation failed"
