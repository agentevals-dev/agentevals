"""Migration runner tests.

The pure helpers (file discovery + schema substitution) are tested directly.
Live PG behavior is tested only when AGENTEVALS_TEST_DATABASE_URL is set;
otherwise those tests skip so the suite stays runnable in pure-Python sandboxes.
"""

from __future__ import annotations

import logging
import os
import re

import pytest

from agentevals.storage.postgres.migrator import (
    ADVISORY_LOCK_KEY,
    CONNECT_RETRY_DEADLINE_S,
    Migration,
    Migrator,
    _apply_schema,
    _discover_migrations,
    connect_deadline_seconds,
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


class TestConnectDeadlineSeconds:
    """``connect_deadline_seconds`` resolves AGENTEVALS_DB_CONNECT_TIMEOUT_S
    to a float, falling back to CONNECT_RETRY_DEADLINE_S on any input the
    retry loop cannot consume. Each failure mode logs at WARNING so the
    cause is diagnosable from pod logs."""

    @pytest.fixture(autouse=True)
    def _clean_env(self, monkeypatch):
        monkeypatch.delenv("AGENTEVALS_DB_CONNECT_TIMEOUT_S", raising=False)

    def test_unset_returns_default(self):
        assert connect_deadline_seconds() == CONNECT_RETRY_DEADLINE_S

    def test_empty_returns_default(self, monkeypatch):
        monkeypatch.setenv("AGENTEVALS_DB_CONNECT_TIMEOUT_S", "")
        assert connect_deadline_seconds() == CONNECT_RETRY_DEADLINE_S

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("42", 42.0),
            ("120.5", 120.5),
            ("0.1", 0.1),
            ("3600", 3600.0),
        ],
    )
    def test_parses_valid_positive_values(self, monkeypatch, raw, expected):
        monkeypatch.setenv("AGENTEVALS_DB_CONNECT_TIMEOUT_S", raw)
        assert connect_deadline_seconds() == expected

    @pytest.mark.parametrize(
        ("raw", "reason_substring"),
        [
            ("foo", "not a number"),
            ("nan", "must be finite"),
            ("inf", "must be finite"),
            ("-inf", "must be finite"),
            ("0", "must be positive"),
            ("-5", "must be positive"),
        ],
    )
    def test_invalid_values_fall_back_with_warning(self, monkeypatch, caplog, raw, reason_substring):
        """Bad inputs return the default and log exactly one warning that
        names the specific validation branch. The cardinality check
        guards against a refactor that double-logs (e.g. emits both a
        generic and a specific message)."""
        monkeypatch.setenv("AGENTEVALS_DB_CONNECT_TIMEOUT_S", raw)
        with caplog.at_level(logging.WARNING, logger="agentevals.storage.postgres.migrator"):
            result = connect_deadline_seconds()
        assert result == CONNECT_RETRY_DEADLINE_S
        warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
        assert len(warnings) == 1, f"expected one warning, got {warnings}"
        assert reason_substring in warnings[0]


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
