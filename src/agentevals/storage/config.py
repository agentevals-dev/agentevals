"""Storage configuration loaded from AGENTEVALS_* env vars."""

from __future__ import annotations

import os
from typing import Literal

from pydantic import BaseModel, Field, field_validator

Backend = Literal["memory", "postgres"]


class StorageSettings(BaseModel):
    """Runtime storage knobs.

    Read from environment in :meth:`from_env`. Defaults preserve the
    pre-existing in-memory developer experience: no Postgres required, no
    ``/api/runs`` endpoints registered.
    """

    backend: Backend = "memory"
    database_url: str | None = None
    schema_name: str = "agentevals"
    migrate_lock_timeout_s: int = 60

    max_concurrent_runs: int = Field(default=4, ge=1)
    run_deadline_s: int = Field(default=300, ge=1)
    heartbeat_s: int = Field(default=5, ge=1)
    lease_s: int = Field(default=30, ge=1)
    max_run_attempts: int = Field(default=3, ge=1)
    worker_poll_interval_s: float = Field(default=1.0, gt=0)

    @field_validator("backend")
    @classmethod
    def _validate_backend(cls, v: Backend) -> Backend:
        if v not in ("memory", "postgres"):
            raise ValueError(f"unknown storage backend '{v}'; expected 'memory' or 'postgres'")
        return v

    def model_post_init(self, __context: object) -> None:
        if self.lease_s <= self.heartbeat_s:
            raise ValueError(
                f"AGENTEVALS_LEASE_S ({self.lease_s}) must be greater than AGENTEVALS_HEARTBEAT_S ({self.heartbeat_s})"
            )
        if self.backend == "postgres" and not self.database_url:
            raise ValueError("AGENTEVALS_STORAGE_BACKEND=postgres requires AGENTEVALS_DATABASE_URL")

    @classmethod
    def from_env(cls) -> StorageSettings:
        return cls(
            backend=os.environ.get("AGENTEVALS_STORAGE_BACKEND", "memory"),
            database_url=_read_dsn_from_env(),
            schema_name=os.environ.get("AGENTEVALS_DATABASE_SCHEMA", "agentevals"),
            migrate_lock_timeout_s=int(os.environ.get("AGENTEVALS_MIGRATE_LOCK_TIMEOUT", "60")),
            max_concurrent_runs=int(os.environ.get("AGENTEVALS_MAX_CONCURRENT_RUNS", "4")),
            run_deadline_s=int(os.environ.get("AGENTEVALS_RUN_DEADLINE_S", "300")),
            heartbeat_s=int(os.environ.get("AGENTEVALS_HEARTBEAT_S", "5")),
            lease_s=int(os.environ.get("AGENTEVALS_LEASE_S", "30")),
            max_run_attempts=int(os.environ.get("AGENTEVALS_MAX_RUN_ATTEMPTS", "3")),
            worker_poll_interval_s=float(os.environ.get("AGENTEVALS_WORKER_POLL_INTERVAL_S", "1.0")),
        )


def _read_dsn_from_env() -> str | None:
    """Return the DSN with AGENTEVALS_DATABASE_URL_FILE preferred over the
    inline AGENTEVALS_DATABASE_URL. The file path is intended for projected
    workload-identity tokens or other secret rotators that prefer a file
    surface to an env var."""
    file_path = os.environ.get("AGENTEVALS_DATABASE_URL_FILE")
    if file_path:
        try:
            with open(file_path) as f:
                return f.read().strip() or None
        except OSError as exc:
            raise ValueError(f"AGENTEVALS_DATABASE_URL_FILE={file_path!r} is unreadable: {exc}") from exc
    return os.environ.get("AGENTEVALS_DATABASE_URL")
