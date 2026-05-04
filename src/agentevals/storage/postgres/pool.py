"""asyncpg pool factory.

asyncpg is imported lazily so the base ``agentevals`` install (without the
``[postgres]`` extra) does not require it.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from ..config import StorageSettings

if TYPE_CHECKING:
    import asyncpg

logger = logging.getLogger(__name__)


async def create_pool(settings: StorageSettings) -> "asyncpg.Pool":
    """Build an asyncpg pool sized for the worker fan-out plus headroom.

    The pool needs at least one connection per concurrent worker (claim +
    heartbeat run on the same connection), one for the API request handlers,
    plus a small buffer.

    Pool warmup eagerly opens ``min_size`` connections, which can race with
    Postgres readiness on a fresh deploy. We retry on connection-refused so
    the lifespan tolerates the gap rather than crashing the pod.
    """
    try:
        import asyncpg
    except ImportError as exc:
        raise ImportError(
            "AGENTEVALS_STORAGE_BACKEND=postgres requires the 'postgres' extra. "
            "Install with: uv sync --extra postgres  (or pip install 'agentevals-cli[postgres]')"
        ) from exc

    if not settings.database_url:
        raise ValueError("AGENTEVALS_DATABASE_URL is required for postgres backend")

    min_size = max(2, settings.max_concurrent_runs)
    max_size = settings.max_concurrent_runs * 2 + 4

    logger.info(
        "Creating asyncpg pool (min=%d, max=%d) for schema '%s'",
        min_size,
        max_size,
        settings.schema_name,
    )

    from .migrator import CONNECT_RETRY_DEADLINE_S

    deadline = asyncio.get_event_loop().time() + CONNECT_RETRY_DEADLINE_S
    delay = 0.5
    while True:
        try:
            pool = await asyncpg.create_pool(
                dsn=settings.database_url,
                min_size=min_size,
                max_size=max_size,
                command_timeout=60,
            )
            break
        except (OSError, asyncpg.PostgresError) as exc:
            now = asyncio.get_event_loop().time()
            if now >= deadline:
                raise
            sleep_for = min(delay, deadline - now)
            logger.info(
                "Pool warmup failed (%s); retrying in %.1fs",
                type(exc).__name__,
                sleep_for,
            )
            await asyncio.sleep(sleep_for)
            delay = min(delay * 2, 5.0)
    if pool is None:
        raise RuntimeError("asyncpg.create_pool returned None")
    return pool
