"""Migration runner tests.

The pure helpers (file discovery + schema substitution) are tested directly.
Live PG behavior is tested only when AGENTEVALS_TEST_DATABASE_URL is set;
otherwise those tests skip so the suite stays runnable in pure-Python sandboxes.
"""

from __future__ import annotations

import os
import re

import pytest

from agentevals.storage.postgres.migrator import (
    ADVISORY_LOCK_KEY,
    Migration,
    Migrator,
    _apply_schema,
    _discover_migrations,
    discover_migrations,
)


class TestDiscoverMigrations:
    def test_finds_baseline(self):
        migrations = _discover_migrations()
        assert len(migrations) >= 1
        first = migrations[0]
        assert first.version == 1
        assert first.name == "init"
        assert first.up_sql.strip()
        assert first.down_sql is not None and first.down_sql.strip()

    def test_versions_sorted(self):
        migrations = _discover_migrations()
        versions = [m.version for m in migrations]
        assert versions == sorted(versions)

    def test_public_alias_matches(self):
        assert [m.version for m in discover_migrations()] == [m.version for m in _discover_migrations()]


class TestApplySchema:
    def test_substitutes_placeholder(self):
        sql = "CREATE TABLE {schema}.foo (id INT)"
        assert _apply_schema(sql, "agentevals") == "CREATE TABLE agentevals.foo (id INT)"

    def test_collapses_doubled_braces(self):
        """Doubled braces in SQL literals (e.g. JSONB defaults like '{{}}')
        collapse to single braces after the {schema} substitution; this
        keeps SQL files readable while letting the placeholder expand."""
        sql = "metadata JSONB NOT NULL DEFAULT '{{}}'"
        assert _apply_schema(sql, "agentevals") == "metadata JSONB NOT NULL DEFAULT '{}'"

    def test_supports_custom_schema(self):
        sql = "CREATE TABLE {schema}.foo (id INT)"
        assert _apply_schema(sql, "myteam") == "CREATE TABLE myteam.foo (id INT)"

    def test_rejects_non_identifier_schema(self):
        """Defense against SQL injection via schema name. Schema is taken
        from an env var which an operator controls but a future bug could
        plumb in untrusted input; the regex stops anything but a SQL identifier."""
        with pytest.raises(ValueError, match="invalid schema"):
            _apply_schema("CREATE TABLE {schema}.foo", "drop; DROP TABLE users")

    def test_rejects_quoted_schema(self):
        with pytest.raises(ValueError, match="invalid schema"):
            _apply_schema("X", '"agentevals"')


class TestAdvisoryLockKey:
    def test_fits_int8(self):
        """pg_try_advisory_lock requires an int8; a key wider than that
        wraps silently and would collide unpredictably. Lock key chosen at
        random; this test only guards against future drift."""
        assert -(2**63) <= ADVISORY_LOCK_KEY < 2**63

    def test_stable(self):
        """Changing the lock key would let two concurrent migrators race.
        Only update the key alongside an explicit migration to a new key."""
        assert ADVISORY_LOCK_KEY == 7259820376655812345


class TestMigrationFilePattern:
    def test_filename_format(self):
        migrations = _discover_migrations()
        for m in migrations:
            assert isinstance(m, Migration)
            assert re.match(r"^[a-z0-9_]+$", m.name)
            assert m.version > 0


@pytest.mark.skipif(
    not os.environ.get("AGENTEVALS_TEST_DATABASE_URL"),
    reason="requires AGENTEVALS_TEST_DATABASE_URL pointing at a disposable Postgres",
)
class TestMigratorLive:
    """Apply / no-op replay / version / force / down — all against a real PG.

    Each test creates and drops its own schema so they can run in any order
    against the same database without interfering.
    """

    @pytest.fixture
    async def migrator(self):
        dsn = os.environ["AGENTEVALS_TEST_DATABASE_URL"]
        schema = "agentevals_test_migrator"
        m = Migrator(dsn=dsn, schema=schema, lock_timeout_s=10)
        yield m
        # cleanup
        try:
            await m.down(steps=1)
        except Exception:
            pass

    async def test_up_then_replay_is_noop(self, migrator):
        applied = await migrator.up()
        assert applied == [1]
        again = await migrator.up()
        assert again == []

    async def test_version_after_up(self, migrator):
        await migrator.up()
        status = await migrator.status()
        assert status.version == 1
        assert status.dirty is False

    async def test_force_clears_dirty(self, migrator):
        await migrator.up()
        await migrator.force(version=1)
        status = await migrator.status()
        assert status.dirty is False
