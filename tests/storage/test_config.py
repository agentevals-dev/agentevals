"""StorageSettings env loading and validation."""

from __future__ import annotations

import pytest

from agentevals.storage.config import StorageSettings


class TestStorageSettings:
    def test_defaults(self):
        s = StorageSettings()
        assert s.backend == "memory"
        assert s.database_url is None
        assert s.schema_name == "agentevals"
        assert s.max_concurrent_runs == 4

    def test_lease_must_exceed_heartbeat(self):
        """Catches operator misconfiguration at boot rather than at first
        heartbeat: a lease shorter than the heartbeat interval lets workers
        steal each other's runs."""
        with pytest.raises(ValueError, match="lease"):
            StorageSettings(lease_s=5, heartbeat_s=5)
        with pytest.raises(ValueError, match="lease"):
            StorageSettings(lease_s=3, heartbeat_s=5)

    def test_postgres_requires_dsn(self):
        with pytest.raises(ValueError, match="AGENTEVALS_DATABASE_URL"):
            StorageSettings(backend="postgres", database_url=None)

    def test_postgres_with_dsn_ok(self):
        s = StorageSettings(backend="postgres", database_url="postgresql://h/db")
        assert s.backend == "postgres"

    def test_unknown_backend_rejected(self):
        """Pydantic wraps the field_validator's ValueError in a
        ValidationError; use the broader match on the inner message."""
        with pytest.raises(Exception, match="unknown storage backend|sqlite"):
            StorageSettings(backend="sqlite")

    def test_from_env_reads_defaults(self, monkeypatch):
        for var in [
            "AGENTEVALS_STORAGE_BACKEND",
            "AGENTEVALS_DATABASE_URL",
            "AGENTEVALS_DATABASE_URL_FILE",
            "AGENTEVALS_DATABASE_SCHEMA",
            "AGENTEVALS_MAX_CONCURRENT_RUNS",
        ]:
            monkeypatch.delenv(var, raising=False)
        s = StorageSettings.from_env()
        assert s.backend == "memory"

    def test_from_env_reads_postgres(self, monkeypatch):
        monkeypatch.setenv("AGENTEVALS_STORAGE_BACKEND", "postgres")
        monkeypatch.setenv("AGENTEVALS_DATABASE_URL", "postgresql://h/db")
        monkeypatch.setenv("AGENTEVALS_DATABASE_SCHEMA", "custom_schema")
        monkeypatch.setenv("AGENTEVALS_MAX_CONCURRENT_RUNS", "12")
        s = StorageSettings.from_env()
        assert s.backend == "postgres"
        assert s.database_url == "postgresql://h/db"
        assert s.schema_name == "custom_schema"
        assert s.max_concurrent_runs == 12

    def test_from_env_url_file_takes_precedence(self, tmp_path, monkeypatch):
        dsn_file = tmp_path / "dsn"
        dsn_file.write_text("postgresql://from-file/db\n")
        monkeypatch.setenv("AGENTEVALS_STORAGE_BACKEND", "postgres")
        monkeypatch.setenv("AGENTEVALS_DATABASE_URL", "postgresql://from-env/db")
        monkeypatch.setenv("AGENTEVALS_DATABASE_URL_FILE", str(dsn_file))
        s = StorageSettings.from_env()
        assert s.database_url == "postgresql://from-file/db"

    def test_from_env_url_file_unreadable_raises(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AGENTEVALS_STORAGE_BACKEND", "postgres")
        monkeypatch.setenv("AGENTEVALS_DATABASE_URL_FILE", str(tmp_path / "missing"))
        with pytest.raises(ValueError, match="unreadable"):
            StorageSettings.from_env()
