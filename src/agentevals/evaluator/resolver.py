"""Resolve remote evaluator references to local cached files."""

from __future__ import annotations

import logging
from pathlib import Path

from .sources import EvaluatorSource, get_sources

logger = logging.getLogger(__name__)

_DEFAULT_CACHE_DIR = Path.home() / ".cache" / "agentevals" / "evaluators"


class EvaluatorResolver:
    """Downloads and caches remote evaluators, converting them to local paths."""

    def __init__(self, cache_dir: Path | None = None):
        self._cache_dir = cache_dir or _DEFAULT_CACHE_DIR
        self._sources: dict[str, EvaluatorSource] = {}

    def register_source(self, source: EvaluatorSource) -> None:
        self._sources[source.source_name] = source

    async def resolve(self, evaluator_def) -> "CodeEvaluatorDef":  # noqa: F821
        """Download a remote evaluator and return a CodeEvaluatorDef pointing to the cached file."""
        from ..config import CodeEvaluatorDef, RemoteEvaluatorDef

        if not isinstance(evaluator_def, RemoteEvaluatorDef):
            raise TypeError(f"Expected RemoteEvaluatorDef, got {type(evaluator_def).__name__}")

        source = self._sources.get(evaluator_def.source)
        if source is None:
            raise ValueError(
                f"Unknown evaluator source '{evaluator_def.source}'. Available: {sorted(self._sources.keys())}"
            )

        dest = self._cache_dir / evaluator_def.source / evaluator_def.ref
        if not dest.exists():
            logger.info(
                "Downloading evaluator '%s' from %s (ref: %s)",
                evaluator_def.name,
                evaluator_def.source,
                evaluator_def.ref,
            )
            await source.fetch_evaluator(evaluator_def.ref, dest)
        else:
            logger.debug("Using cached evaluator '%s' at %s", evaluator_def.name, dest)

        return CodeEvaluatorDef(
            name=evaluator_def.name,
            path=str(dest),
            threshold=evaluator_def.threshold,
            timeout=evaluator_def.timeout,
            config=evaluator_def.config,
            executor=evaluator_def.executor,
        )


_default_resolver: EvaluatorResolver | None = None


def get_default_resolver() -> EvaluatorResolver:
    """Return a lazily-initialized resolver with all registered sources."""
    global _default_resolver
    if _default_resolver is None:
        _default_resolver = EvaluatorResolver()
        for source in get_sources():
            _default_resolver.register_source(source)
    return _default_resolver
