"""Connections and transactions.

Every statement pgharbor sends runs inside a transaction opened here, and every
transaction follows the same script:

1. ``BEGIN TRANSACTION READ ONLY`` (``READ WRITE`` only for the write tools of a profile
   that allows writing), followed by ``SET LOCAL`` for the timeouts and for the settings
   the SQL scanner depends on, all in one message built from constants.
2. A ``SELECT`` that reads the settings back. It proves the mode, and because it takes the
   transaction snapshot the mode can no longer be changed afterwards.
3. The work.
4. ``ROLLBACK`` (or ``COMMIT`` for a confirmed write), then ``DISCARD ALL`` so that no
   session state (prepared statements, advisory locks, settings) survives into the next
   call. A connection that fails any of these steps is closed, never reused.

SQL that came from outside (the agent) is only ever executed through :meth:`Db.raw`,
which insists on bound parameters. With parameters psycopg uses the extended query
protocol, where the server itself refuses more than one statement per message.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from typing import Any

import psycopg
from psycopg import pq
from psycopg.abc import Buffer
from psycopg.adapt import Loader
from psycopg.rows import dict_row
from psycopg.types.json import Json
from psycopg.types.string import TextLoader

from .config import Config, ConnectionConfig
from .errors import ConfigError, NotAvailable, PgHarborError, Rejected
from .guard import quote_ident

__all__ = ["Db", "ExtensionInfo", "ServerFacts", "Sessions", "describe_db_error"]

MIN_SERVER_VERSION = 90600
_FACTS_TTL_SECONDS = 600.0
# A typed parameter no statement refers to. See Db.raw().
_PROTOCOL_SENTINEL = 0

# Types returned as their text form, exactly as the server prints them.
_TEXT_TYPES = (
    "date",
    "time",
    "timetz",
    "timestamp",
    "timestamptz",
    "interval",
    "bytea",
    "uuid",
    "inet",
    "cidr",
    "macaddr",
    "money",
    "xml",
    "bit",
    "varbit",
)


class _NumericLoader(Loader):
    """numeric -> int when integral, float otherwise, text for NaN and infinities."""

    def load(self, data: Buffer) -> int | float | str:
        text = bytes(data).decode()
        if text.lstrip("-").isdigit():
            return int(text)
        try:
            value = float(text)
        except ValueError:
            return text
        return value if value == value and abs(value) != float("inf") else text


@dataclass(frozen=True, slots=True)
class ExtensionInfo:
    name: str
    version: str
    schema: str


@dataclass(frozen=True, slots=True)
class ServerFacts:
    version_num: int
    version: str
    database: str
    user: str
    session_user: str
    is_superuser: bool
    in_recovery: bool
    encoding: str
    extensions: dict[str, ExtensionInfo]
    catalog: frozenset[str]
    jit: bool = False  # the session default; pgharbor's own queries run with it off
    jit_above_cost: float = -1.0
    loaded_at: float = field(default_factory=time.monotonic)

    @property
    def major(self) -> int:
        """9 for 9.6, otherwise the major version (10, 11, ... 19)."""
        return self.version_num // 10000

    @property
    def label(self) -> str:
        return self.version.split()[0]


class Db:
    """One open transaction. Tools receive a ``Db`` and never see the connection."""

    def __init__(
        self,
        profile: ConnectionConfig,
        facts: ServerFacts,
        conn: psycopg.AsyncConnection[Any],
        *,
        read_only: bool,
    ) -> None:
        self.profile = profile
        self.facts = facts
        self.read_only = read_only
        self._conn = conn
        self.statements = 0
        # Set by a tool that wants its changes discarded (a dry run): the transaction
        # then ends in ROLLBACK even though no error happened.
        self.rollback_only = False

    # ------------------------------------------------------------------ server facts

    @property
    def v(self) -> int:
        """``server_version_num``: 90624, 130023, 180006, ..."""
        return self.facts.version_num

    def has(self, relation: str) -> bool:
        """True when ``pg_catalog.<relation>`` exists on this server."""
        return relation in self.facts.catalog

    def ext(self, name: str) -> ExtensionInfo | None:
        return self.facts.extensions.get(name)

    def need_ext(self, name: str) -> ExtensionInfo:
        info = self.ext(name)
        if info is None:
            raise NotAvailable(
                f"The extension '{name}' is not installed in database "
                f"'{self.facts.database}'. A database owner can install it with: "
                f"CREATE EXTENSION {name};"
            )
        return info

    def need_version(self, minimum: int, feature: str) -> None:
        """``minimum`` is a ``server_version_num`` such as 100000."""
        if self.v < minimum:
            wanted = "9.6" if minimum < 100000 else str(minimum // 10000)
            raise NotAvailable(
                f"{feature} needs PostgreSQL {wanted} or newer; this server is {self.facts.label}."
            )

    # ------------------------------------------------------------- trusted statements

    async def rows(self, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
        """Run SQL written by pgharbor itself. Values always travel as parameters."""
        self.statements += 1
        async with psycopg.AsyncRawCursor(self._conn, row_factory=dict_row) as cur:
            await cur.execute(sql, _adapt_params(params) or None)
            if cur.description is None:
                return []
            return await cur.fetchall()

    async def row(self, sql: str, params: Sequence[Any] = ()) -> dict[str, Any] | None:
        rows = await self.rows(sql, params)
        return rows[0] if rows else None

    async def value(self, sql: str, params: Sequence[Any] = ()) -> Any:
        row = await self.row(sql, params)
        return next(iter(row.values())) if row else None

    # ----------------------------------------------------------- untrusted statements

    async def raw(self, sql: str, params: Sequence[Any] = ()) -> pq.abc.PGresult:
        """Run SQL that contains text from outside. Returns the low-level result.

        One extra parameter is always appended. The statement never refers to it; its
        only job is to make the parameter list non-empty, because that is what makes
        psycopg use the extended query protocol, in which the server itself rejects a
        message holding more than one statement. Never bypass this method for SQL that
        did not come from pgharbor's own source code.
        """
        bound = [*_adapt_params(params), _PROTOCOL_SENTINEL]
        self.statements += 1
        async with psycopg.AsyncRawCursor(self._conn) as cur:
            await cur.execute(sql, bound)
            result = cur.pgresult
            if result is None:  # pragma: no cover - execute() always leaves a result
                raise PgHarborError("the server returned no result")
            return result

    async def restore_jit(self, estimated_cost: float | None) -> None:
        """Give the server's JIT setting back to a statement of the caller.

        The transaction starts with JIT off for pgharbor's own catalog queries. A
        statement whose estimated cost reaches ``jit_above_cost`` is one the server
        would compile, so it gets the session default back: what the caller measures is
        what the server does. Cheaper statements never use JIT and need nothing.
        """
        facts = self.facts
        if facts.jit and estimated_cost is not None and 0 <= facts.jit_above_cost <= estimated_cost:
            await self.rows("SET LOCAL jit TO DEFAULT")

    async def copy_in(self, sql: str, chunks: AsyncIterator[bytes]) -> int:
        """Feed ``COPY ... FROM STDIN`` and return the number of rows loaded.

        ``sql`` must be built by pgharbor from catalog names and validated options; this
        method is not for statements written by the agent.
        """
        if self.read_only:
            raise AssertionError("copy_in needs a write transaction")
        self.statements += 1
        async with self._conn.cursor() as cur:
            async with cur.copy(sql) as copy:
                async for chunk in chunks:
                    await copy.write(chunk)
            return cur.rowcount

    @property
    def encoding(self) -> str:
        return self._conn.info.encoding


def _adapt_params(params: Sequence[Any]) -> list[Any]:
    out: list[Any] = []
    for value in params:
        if isinstance(value, dict) or (
            isinstance(value, list) and any(isinstance(v, (dict, list)) for v in value)
        ):
            out.append(Json(value))
        else:
            out.append(value)
    return out


# --------------------------------------------------------------------------------------- pool


class _Pool:
    """A very small connection pool for one profile.

    Kept deliberately simple: connection errors surface as they are, idle connections
    are closed on a timer, and nothing is reused after a failure.
    """

    def __init__(self, profile: ConnectionConfig) -> None:
        self.profile = profile
        self._slots = asyncio.Semaphore(profile.pool_size)
        self._idle: list[psycopg.AsyncConnection[Any]] = []
        self._timers: dict[int, asyncio.TimerHandle] = {}
        self.use_startup_options = True

    async def get(self) -> psycopg.AsyncConnection[Any]:
        wait = max(self.profile.statement_timeout / 1000 + 5, 15)
        try:
            await asyncio.wait_for(self._slots.acquire(), timeout=wait)
        except TimeoutError:
            raise PgHarborError(
                f"Connection '{self.profile.name}' is busy: all {self.profile.pool_size} "
                "connection(s) are in use. Retry in a moment."
            ) from None
        try:
            while self._idle:
                conn = self._idle.pop()
                timer = self._timers.pop(id(conn), None)
                if timer:
                    timer.cancel()
                if not conn.closed and conn.info.transaction_status == pq.TransactionStatus.IDLE:
                    return conn
                await _close_quietly(conn)
            return await self._connect()
        except BaseException:
            self._slots.release()
            raise

    async def put(self, conn: psycopg.AsyncConnection[Any], *, reusable: bool) -> None:
        try:
            keep = (
                reusable
                and self.profile.idle_close > 0
                and not conn.closed
                and conn.info.transaction_status == pq.TransactionStatus.IDLE
            )
            if keep:
                self._idle.append(conn)
                loop = asyncio.get_running_loop()
                self._timers[id(conn)] = loop.call_later(
                    self.profile.idle_close / 1000, self._expire, conn
                )
            else:
                await _close_quietly(conn)
        finally:
            self._slots.release()

    def _expire(self, conn: psycopg.AsyncConnection[Any]) -> None:
        self._timers.pop(id(conn), None)
        if conn in self._idle:
            self._idle.remove(conn)
            task = asyncio.ensure_future(_close_quietly(conn))
            task.add_done_callback(lambda _t: None)

    async def close(self) -> None:
        for timer in self._timers.values():
            timer.cancel()
        self._timers.clear()
        idle, self._idle = self._idle, []
        for conn in idle:
            await _close_quietly(conn)

    async def _connect(self) -> psycopg.AsyncConnection[Any]:
        profile = self.profile
        dsn, kwargs = profile.conninfo()
        if self.use_startup_options:
            try:
                return await self._open(dsn, {**kwargs, **self._startup(kwargs)})
            except psycopg.OperationalError as exc:
                # Connection poolers such as PgBouncer refuse the "options" startup
                # parameter. The same settings are applied per transaction anyway.
                if "options" not in str(exc) and "startup parameter" not in str(exc):
                    raise
                self.use_startup_options = False
        return await self._open(dsn, kwargs)

    def _startup(self, kwargs: dict[str, Any]) -> dict[str, Any]:
        p = self.profile
        extra: dict[str, Any] = {
            "options": (
                "-c default_transaction_read_only=on"
                f" -c statement_timeout={p.statement_timeout}"
                f" -c lock_timeout={p.lock_timeout}"
                f" -c idle_in_transaction_session_timeout={p.idle_in_transaction_session_timeout}"
                " -c standard_conforming_strings=on"
            )
        }
        if p.read_only and "target_session_attrs" not in kwargs:
            # libpq then checks, at connection time, that the session is read-only.
            extra["target_session_attrs"] = "read-only"
        return extra

    async def _open(self, dsn: str, kwargs: dict[str, Any]) -> psycopg.AsyncConnection[Any]:
        conn = await psycopg.AsyncConnection.connect(dsn, autocommit=True, **kwargs)
        try:
            conn.prepare_threshold = None  # never create named prepared statements
            if conn.info.server_version < MIN_SERVER_VERSION:
                raise NotAvailable(
                    f"PostgreSQL {conn.info.server_version} is older than 9.6, the oldest "
                    "version pgharbor supports."
                )
            if conn.info.encoding.lower().replace("-", "") not in ("utf8", "utf_8"):
                await conn.execute("SET client_encoding = 'UTF8'")
            for type_name in _TEXT_TYPES:
                conn.adapters.register_loader(type_name, TextLoader)
            conn.adapters.register_loader("numeric", _NumericLoader)
        except BaseException:
            await _close_quietly(conn)
            raise
        return conn


async def _close_quietly(conn: psycopg.AsyncConnection[Any]) -> None:
    with contextlib.suppress(Exception):
        await conn.close()


# ----------------------------------------------------------------------------------- sessions


class Sessions:
    """Opens transactions on the configured connections."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self._pools: dict[str, _Pool] = {}
        self._facts: dict[str, ServerFacts] = {}

    def profile(self, name: str | None) -> ConnectionConfig:
        return self.config.connection(name)

    def cached_facts(self, name: str) -> ServerFacts | None:
        return self._facts.get(name)

    def forget_facts(self, name: str) -> None:
        self._facts.pop(name, None)

    async def close(self) -> None:
        for pool in self._pools.values():
            await pool.close()
        self._pools.clear()

    @contextlib.asynccontextmanager
    async def transaction(
        self, name: str | None, *, write: bool = False, timeout_ms: int | None = None
    ) -> AsyncIterator[Db]:
        """Open a transaction on connection ``name``.

        Read-only unless ``write`` is true, and writing is only possible on a profile
        whose ``access`` is not ``read-only``. A write transaction commits when the block
        ends normally and rolls back when it raises.
        """
        profile = self.profile(name)
        if write and profile.read_only:
            raise Rejected(
                f"connection '{profile.name}' is read-only",
                rule="read-only-connection",
                hint='Writing needs a connection configured with access = "read-write" or "admin".',
            )
        pool = self._pools.get(profile.name)
        if pool is None:
            pool = self._pools[profile.name] = _Pool(profile)

        conn = await self._connected(pool, profile, write=write, timeout_ms=timeout_ms)
        reusable = False
        try:
            facts = await self._facts_for(profile, conn)
            db = Db(profile, facts, conn, read_only=not write)
            try:
                yield db
            except BaseException:
                reusable = await _finish(conn, commit=False)
                raise
            reusable = await _finish(conn, commit=write and not db.rollback_only)
        finally:
            await pool.put(conn, reusable=reusable)

    @contextlib.asynccontextmanager
    async def autocommit(
        self, name: str | None, *, timeout_ms: int | None = None
    ) -> AsyncIterator[Db]:
        """A session with no transaction block, for the few commands that refuse one.

        ``VACUUM``, ``CREATE INDEX CONCURRENTLY``, ``ALTER SYSTEM`` and a handful of others
        cannot run inside ``BEGIN``. Only the administrative tools use this, and only on a
        profile whose ``access`` is ``admin``. Every statement commits on its own, so there
        is nothing to roll back and no dry run. The session is wiped afterwards like any
        other.
        """
        profile = self.profile(name)
        if profile.access.rank < 2:
            raise Rejected(
                f"connection '{profile.name}' is not an admin connection",
                rule="access-mode",
            )
        pool = self._pools.get(profile.name)
        if pool is None:
            pool = self._pools[profile.name] = _Pool(profile)
        conn = await pool.get()
        reusable = False
        try:
            facts = await self._facts_for(profile, conn)
            await conn.execute(_autocommit_sql(profile, timeout_ms=timeout_ms))
            db = Db(profile, facts, conn, read_only=False)
            try:
                yield db
            finally:
                reusable = await _finish(conn, commit=False)
        finally:
            await pool.put(conn, reusable=reusable)

    async def _connected(
        self, pool: _Pool, profile: ConnectionConfig, *, write: bool, timeout_ms: int | None
    ) -> psycopg.AsyncConnection[Any]:
        """Get a connection with an open, verified transaction."""
        last: Exception | None = None
        for _attempt in range(2):
            conn = await pool.get()
            try:
                await _begin(conn, profile, write=write, timeout_ms=timeout_ms)
                return conn
            except psycopg.OperationalError as exc:
                # A pooled connection the server already closed: try once with a new one.
                last = exc
                await pool.put(conn, reusable=False)
            except BaseException:
                await pool.put(conn, reusable=False)
                raise
        assert last is not None
        raise last

    async def _facts_for(
        self, profile: ConnectionConfig, conn: psycopg.AsyncConnection[Any]
    ) -> ServerFacts:
        facts = self._facts.get(profile.name)
        if facts and time.monotonic() - facts.loaded_at < _FACTS_TTL_SECONDS:
            return facts
        facts = await _load_facts(conn)
        self._facts[profile.name] = facts
        return facts


def _begin_sql(
    profile: ConnectionConfig, *, write: bool, timeout_ms: int | None, server_version: int = 0
) -> str:
    """The opening script. Built only from validated integers and constants."""
    statement_timeout = int(timeout_ms or profile.statement_timeout)
    parts = [
        "BEGIN TRANSACTION READ WRITE" if write else "BEGIN TRANSACTION READ ONLY",
        f"SET LOCAL statement_timeout = {statement_timeout}",
        f"SET LOCAL lock_timeout = {int(profile.lock_timeout)}",
        "SET LOCAL idle_in_transaction_session_timeout = "
        f"{int(profile.idle_in_transaction_session_timeout)}",
        "SET LOCAL standard_conforming_strings = on",
        "SET LOCAL datestyle = 'ISO'",
        "SET LOCAL intervalstyle = 'postgres'",
        "SET LOCAL bytea_output = 'hex'",
        "SET LOCAL extra_float_digits = 3",
    ]
    if not write:
        parts.append("SET LOCAL default_transaction_read_only = on")
    if server_version >= 110000:
        # The catalog queries behind every check are short; compiling them costs a
        # quarter of a second each. Db.restore_jit gives the setting back to a
        # statement of the caller that is expensive enough for the server to use it.
        parts.append("SET LOCAL jit = off")
    if profile.role:
        parts.append(f"SET LOCAL ROLE {quote_ident(profile.role)}")
    parts.append(
        "SELECT current_setting('transaction_read_only') AS tro,"
        " current_setting('default_transaction_read_only') AS dtro,"
        " current_setting('standard_conforming_strings') AS scs"
    )
    return "; ".join(parts)


def _autocommit_sql(profile: ConnectionConfig, *, timeout_ms: int | None) -> str:
    """Session settings for the no-transaction path. ``DISCARD ALL`` undoes them."""
    parts = [
        "SET default_transaction_read_only = off",
        f"SET statement_timeout = {int(timeout_ms or profile.statement_timeout)}",
        f"SET lock_timeout = {int(profile.lock_timeout)}",
        "SET standard_conforming_strings = on",
    ]
    if profile.role:
        parts.append(f"SET ROLE {quote_ident(profile.role)}")
    return "; ".join(parts)


async def _begin(
    conn: psycopg.AsyncConnection[Any],
    profile: ConnectionConfig,
    *,
    write: bool,
    timeout_ms: int | None,
) -> None:
    async with conn.cursor() as cur:
        await cur.execute(
            _begin_sql(
                profile,
                write=write,
                timeout_ms=timeout_ms,
                server_version=conn.info.server_version,
            )
        )
        check: tuple[Any, ...] | None = None
        while True:
            if cur.description is not None:
                check = await cur.fetchone()
            if not cur.nextset():
                break
    if conn.info.transaction_status != pq.TransactionStatus.INTRANS or check is None:
        raise PgHarborError("could not open a transaction; nothing was executed")
    read_only, default_read_only, conforming = check
    if conforming != "on":
        raise PgHarborError(
            "standard_conforming_strings could not be turned on; nothing was executed"
        )
    if not write and (read_only != "on" or default_read_only != "on"):
        raise PgHarborError(
            "the read-only mode of the transaction could not be confirmed; nothing was executed"
        )


async def _finish(conn: psycopg.AsyncConnection[Any], *, commit: bool) -> bool:
    """End the transaction and wipe the session. Returns False if the connection is unfit."""
    try:
        status = conn.info.transaction_status
        if status == pq.TransactionStatus.ACTIVE:
            return False  # a statement is still running (cancelled call): do not reuse
        if status in (pq.TransactionStatus.INTRANS, pq.TransactionStatus.INERROR):
            if commit and status == pq.TransactionStatus.INTRANS:
                await conn.execute("COMMIT")
            else:
                await conn.execute("ROLLBACK")
        if conn.info.transaction_status != pq.TransactionStatus.IDLE:
            return False
        await conn.execute("DISCARD ALL")
        return conn.info.transaction_status == pq.TransactionStatus.IDLE
    except (psycopg.Error, OSError):
        return False


_FACTS_SQL = """
SELECT current_setting('server_version') AS version,
       current_setting('server_version_num')::int AS num,
       current_database()::text AS database,
       current_user::text AS usr,
       session_user::text AS sess,
       coalesce((SELECT rolsuper FROM pg_catalog.pg_roles WHERE rolname = current_user), false)
           AS super,
       pg_is_in_recovery() AS recovery,
       current_setting('server_encoding') AS encoding,
       (SELECT coalesce(json_agg(json_build_array(e.extname, e.extversion, n.nspname)), '[]')
          FROM pg_catalog.pg_extension e
          JOIN pg_catalog.pg_namespace n ON n.oid = e.extnamespace) AS extensions,
       (SELECT json_agg(c.relname)
          FROM pg_catalog.pg_class c
          JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
         WHERE n.nspname = 'pg_catalog' AND c.relkind IN ('r', 'v')) AS catalog,
       -- reset_val is the session default, whatever SET LOCAL did in this transaction.
       (SELECT s.reset_val FROM pg_catalog.pg_settings s WHERE s.name = 'jit') AS jit,
       (SELECT s.reset_val FROM pg_catalog.pg_settings s WHERE s.name = 'jit_above_cost')
           AS jit_above_cost
"""


async def _load_facts(conn: psycopg.AsyncConnection[Any]) -> ServerFacts:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(_FACTS_SQL)
        row = await cur.fetchone()
    if row is None:  # pragma: no cover
        raise PgHarborError("could not read the server version")
    return ServerFacts(
        version_num=row["num"],
        version=row["version"],
        database=row["database"],
        user=row["usr"],
        session_user=row["sess"],
        is_superuser=bool(row["super"]),
        in_recovery=bool(row["recovery"]),
        encoding=row["encoding"],
        extensions={
            name: ExtensionInfo(name, version, schema)
            for name, version, schema in (row["extensions"] or [])
        },
        catalog=frozenset(row["catalog"] or []),
        jit=row["jit"] == "on",
        jit_above_cost=float(row["jit_above_cost"]) if row["jit_above_cost"] else -1.0,
    )


# -------------------------------------------------------------------------------- error texts


def describe_db_error(exc: BaseException, profile: ConnectionConfig | None = None) -> str:
    """Turn a driver error into a message that says what happened and what to do."""
    where = f" on connection '{profile.name}'" if profile else ""
    if isinstance(exc, ConfigError):
        return str(exc)
    if isinstance(exc, psycopg.errors.QueryCanceled):
        limit = f" ({profile.statement_timeout} ms)" if profile else ""
        return (
            f"The statement was cancelled by the time limit{limit}{where}. "
            "Narrow the query (filter on indexed columns, fewer rows) and try again."
        )
    if isinstance(exc, psycopg.errors.LockNotAvailable):
        return (
            f"The statement could not get a lock within the lock timeout{where}. "
            "Another session holds a conflicting lock; pg_locks shows which."
        )
    if isinstance(exc, psycopg.errors.ReadOnlySqlTransaction):
        return (
            f"The server refused a write: the transaction is read-only{where}. ({_first_line(exc)})"
        )
    if isinstance(exc, psycopg.errors.InsufficientPrivilege):
        return f"Permission denied{where}: {_first_line(exc)}"
    if isinstance(exc, psycopg.OperationalError) and getattr(exc, "sqlstate", None) is None:
        return f"Could not reach the database{where}: {_first_line(exc)}"
    if isinstance(exc, psycopg.Error):
        state = getattr(exc, "sqlstate", None)
        detail = _diag(exc)
        return f"PostgreSQL error{where}{f' [{state}]' if state else ''}: {detail}"
    return str(exc) or type(exc).__name__


def _first_line(exc: BaseException) -> str:
    text = str(exc).strip()
    return text.splitlines()[0] if text else type(exc).__name__


def _diag(exc: psycopg.Error) -> str:
    diag = exc.diag
    parts = [diag.message_primary or _first_line(exc)]
    if diag.message_detail:
        parts.append(f"Detail: {diag.message_detail}")
    if diag.message_hint:
        parts.append(f"Hint: {diag.message_hint}")
    if diag.statement_position:
        parts.append(f"(at character {diag.statement_position})")
    return " ".join(parts)
