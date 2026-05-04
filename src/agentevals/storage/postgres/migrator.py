"""SQL migration runner.

Applies sequentially numbered migrations under
``src/agentevals/storage/postgres/migrations/``. Holds a Postgres advisory
lock for the duration so multi-replica installs can safely call ``migrate
up`` from any process. The tracking table is golang-migrate compatible
(``schema_migrations`` with ``version`` BIGINT PRIMARY KEY and ``dirty``
BOOLEAN), so external migration tooling can adopt the same files later
without translation.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from importlib.resources import files
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import asyncpg

logger = logging.getLogger(__name__)

ADVISORY_LOCK_KEY = 7259820376655812345
"""Fixed int8 used by pg_try_advisory_lock during migration runs.
Chosen at random; collision-free for any sane application."""

_FILE_PATTERN = re.compile(r"^(?P<version>\d{6})_(?P<name>[a-z0-9_]+)\.(?P<dir>up|down)\.sql$")


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    up_sql: str
    down_sql: str | None


def _discover_migrations() -> list[Migration]:
    """Read all NNNNNN_name.up.sql / .down.sql pairs from the package.

    importlib.resources resolves correctly inside a wheel, in editable
    installs, and from a zipped package.
    """
    pkg = files("agentevals.storage.postgres.migrations")
    ups: dict[int, tuple[str, str]] = {}
    downs: dict[int, str] = {}

    for entry in pkg.iterdir():
        match = _FILE_PATTERN.match(entry.name)
        if not match:
            continue
        version = int(match.group("version"))
        name = match.group("name")
        sql = entry.read_text(encoding="utf-8")
        if match.group("dir") == "up":
            ups[version] = (name, sql)
        else:
            downs[version] = sql

    migrations = []
    for version in sorted(ups):
        name, up_sql = ups[version]
        migrations.append(Migration(version=version, name=name, up_sql=up_sql, down_sql=downs.get(version)))
    return migrations


def _apply_schema(sql: str, schema: str) -> str:
    """Substitute the {schema} placeholder. Doubled braces in SQL literals
    (``'{{}}'``) collapse back to single braces."""
    if not re.match(r"^[a-zA-Z_][a-zA-Z0-9_]*$", schema):
        raise ValueError(f"invalid schema name '{schema}'; must be a SQL identifier")
    return sql.replace("{schema}", schema).replace("{{}}", "{}")


@dataclass
class MigrationStatus:
    version: int | None
    dirty: bool


class Migrator:
    """Applies and rolls back migrations against a single Postgres database.

    One advisory lock is held for the lifetime of any apply/rollback call so
    concurrent migrators (multiple agentevals replicas booting at once) wait
    rather than racing.
    """

    def __init__(self, dsn: str, schema: str = "agentevals", lock_timeout_s: int = 60) -> None:
        self._dsn = dsn
        self._schema = schema
        self._lock_timeout_s = lock_timeout_s

    async def _connect(self) -> "asyncpg.Connection":
        try:
            import asyncpg
        except ImportError as exc:
            raise ImportError(
                "agentevals migrate requires the 'postgres' extra. Install with: uv sync --extra postgres"
            ) from exc
        return await connect_with_retry(self._dsn, asyncpg)

    async def _acquire_lock(self, conn: "asyncpg.Connection") -> None:
        deadline = asyncio.get_event_loop().time() + self._lock_timeout_s
        attempt = 0
        while True:
            acquired = await conn.fetchval("SELECT pg_try_advisory_lock($1)", ADVISORY_LOCK_KEY)
            if acquired:
                return
            if asyncio.get_event_loop().time() >= deadline:
                raise TimeoutError(
                    f"Could not acquire migration advisory lock within {self._lock_timeout_s}s. "
                    "Another migration is likely in progress."
                )
            attempt += 1
            wait = min(2.0, 0.2 * attempt)
            logger.info("Waiting for migration lock (attempt %d, sleeping %.1fs)...", attempt, wait)
            await asyncio.sleep(wait)

    async def _release_lock(self, conn: "asyncpg.Connection") -> None:
        await conn.execute("SELECT pg_advisory_unlock($1)", ADVISORY_LOCK_KEY)

    async def _ensure_tracking_table(self, conn: "asyncpg.Connection") -> None:
        await conn.execute(f'CREATE SCHEMA IF NOT EXISTS "{self._schema}"')
        await conn.execute(
            f'CREATE TABLE IF NOT EXISTS "{self._schema}".schema_migrations '
            "(version BIGINT NOT NULL PRIMARY KEY, dirty BOOLEAN NOT NULL)"
        )

    async def _read_status(self, conn: "asyncpg.Connection") -> MigrationStatus:
        row = await conn.fetchrow(f'SELECT version, dirty FROM "{self._schema}".schema_migrations LIMIT 1')
        if row is None:
            return MigrationStatus(version=None, dirty=False)
        return MigrationStatus(version=int(row["version"]), dirty=bool(row["dirty"]))

    async def _write_status(self, conn: "asyncpg.Connection", version: int | None, dirty: bool) -> None:
        await conn.execute(f'DELETE FROM "{self._schema}".schema_migrations')
        if version is not None:
            await conn.execute(
                f'INSERT INTO "{self._schema}".schema_migrations (version, dirty) VALUES ($1, $2)',
                version,
                dirty,
            )

    async def status(self) -> MigrationStatus:
        conn = await self._connect()
        try:
            await self._ensure_tracking_table(conn)
            return await self._read_status(conn)
        finally:
            await conn.close()

    async def up(self, *, dry_run: bool = False) -> list[int]:
        migrations = _discover_migrations()
        applied: list[int] = []
        conn = await self._connect()
        try:
            await self._ensure_tracking_table(conn)
            await self._acquire_lock(conn)
            try:
                status = await self._read_status(conn)
                if status.dirty:
                    raise RuntimeError(
                        f"schema_migrations is dirty at version {status.version}. "
                        "Resolve manually, then run: agentevals migrate force <version>"
                    )
                pending = [m for m in migrations if status.version is None or m.version > status.version]
                if not pending:
                    logger.info("Nothing to apply (current version: %s)", status.version)
                    return []
                for m in pending:
                    sql = _apply_schema(m.up_sql, self._schema)
                    if dry_run:
                        logger.info("Would apply migration %06d_%s", m.version, m.name)
                        applied.append(m.version)
                        continue
                    logger.info("Applying migration %06d_%s", m.version, m.name)
                    await self._write_status(conn, m.version, dirty=True)
                    try:
                        async with conn.transaction():
                            await conn.execute(sql)
                            await self._write_status(conn, m.version, dirty=False)
                    except Exception:
                        logger.exception("Migration %06d_%s failed; schema_migrations left dirty", m.version, m.name)
                        raise
                    applied.append(m.version)
            finally:
                await self._release_lock(conn)
        finally:
            await conn.close()
        return applied

    async def down(self, *, steps: int) -> list[tuple[int, str]]:
        if steps < 1:
            raise ValueError("steps must be >= 1")
        migrations = _discover_migrations()
        by_version = {m.version: m for m in migrations}
        rolled_back: list[tuple[int, str]] = []
        conn = await self._connect()
        try:
            await self._ensure_tracking_table(conn)
            await self._acquire_lock(conn)
            try:
                status = await self._read_status(conn)
                if status.dirty or status.version is None:
                    raise RuntimeError(
                        f"refusing to roll back from dirty/empty state (version={status.version}, dirty={status.dirty})"
                    )
                applied_versions = sorted((v for v in by_version if v <= status.version), reverse=True)
                target_versions = applied_versions[:steps]
                for version in target_versions:
                    m = by_version[version]
                    if not m.down_sql:
                        raise RuntimeError(f"migration {version:06d}_{m.name} has no down.sql")
                    sql = _apply_schema(m.down_sql, self._schema)
                    logger.warning("Rolling back %06d_%s\n--- SQL ---\n%s\n--- end ---", m.version, m.name, sql)
                    next_version = max((v for v in by_version if v < version), default=None)
                    await self._write_status(conn, version, dirty=True)
                    try:
                        async with conn.transaction():
                            await conn.execute(sql)
                            await self._write_status(conn, next_version, dirty=False)
                    except Exception:
                        logger.exception(
                            "Down migration %06d_%s failed; schema_migrations left dirty", m.version, m.name
                        )
                        raise
                    rolled_back.append((m.version, m.name))
                    if next_version is None:
                        break
            finally:
                await self._release_lock(conn)
        finally:
            await conn.close()
        return rolled_back

    async def force(self, version: int) -> None:
        conn = await self._connect()
        try:
            await self._ensure_tracking_table(conn)
            await self._write_status(conn, version, dirty=False)
        finally:
            await conn.close()


def discover_migrations() -> list[Migration]:
    """Public alias for the migration discovery helper, used by ``migrate create``."""
    return _discover_migrations()


CONNECT_RETRY_DEADLINE_S = 60.0
"""Total wall-clock budget for the initial Postgres connection. Bundled PG
in Kubernetes typically takes 5-15s to be ready (PVC bind, initdb, listener
bind), so the agentevals lifespan can race the database on a fresh deploy.
Retrying tolerates that gap rather than failing pod startup and relying on
CrashLoopBackOff timing to eventually line up."""


async def connect_with_retry(dsn: str, asyncpg_module) -> "asyncpg.Connection":
    """Open a single asyncpg connection, retrying on connection-refused or
    server-not-ready errors for up to ``CONNECT_RETRY_DEADLINE_S`` seconds.

    Connection-time errors are tolerated; once a connection has been
    established and a query returned, all subsequent failures propagate
    normally.
    """
    deadline = asyncio.get_event_loop().time() + CONNECT_RETRY_DEADLINE_S
    delay = 0.5
    while True:
        try:
            return await asyncpg_module.connect(dsn)
        except (OSError, asyncpg_module.PostgresError) as exc:
            now = asyncio.get_event_loop().time()
            if now >= deadline:
                raise
            sleep_for = min(delay, deadline - now)
            logger.info(
                "Database not ready (%s); retrying in %.1fs",
                type(exc).__name__,
                sleep_for,
            )
            await asyncio.sleep(sleep_for)
            delay = min(delay * 2, 5.0)
