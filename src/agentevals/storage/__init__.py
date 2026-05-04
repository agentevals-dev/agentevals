"""Storage abstractions for agentevals.

Two backends ship: ``memory`` (default, preserves zero-config developer
experience) and ``postgres`` (durable runs/results, enables ``/api/runs``).

The public surface is :class:`Repos`, a small bundle of repository
implementations selected by :class:`StorageSettings.backend`.
"""

from __future__ import annotations

from .config import StorageSettings
from .models import Result, ResultStatus, Run, RunSpec, RunStatus, TraceTarget
from .repos import Repos, ResultRepository, RunRepository, SessionRepository

__all__ = [
    "Repos",
    "Result",
    "ResultRepository",
    "ResultStatus",
    "Run",
    "RunRepository",
    "RunSpec",
    "RunStatus",
    "SessionRepository",
    "StorageSettings",
    "TraceTarget",
    "build_repos",
]


async def build_repos(settings: StorageSettings) -> Repos:
    """Construct the repository bundle for ``settings.backend``.

    Memory backend instantiates dict-backed repos eagerly. Postgres backend
    creates an asyncpg pool, applies pending migrations, then wires repos
    against that pool.
    """
    if settings.backend == "memory":
        from .repos.memory import MemoryRepos

        return MemoryRepos.create()

    from .postgres.pool import create_pool
    from .repos.postgres import PostgresRepos

    pool = await create_pool(settings)
    return await PostgresRepos.create(pool=pool, schema=settings.schema_name)
