"""Resolve remote evaluator references to local cached files."""

from __future__ import annotations

import logging
import time
from pathlib import Path

from .sources import EvaluatorSource, get_sources

logger = logging.getLogger(__name__)

_DEFAULT_CACHE_DIR = Path.home() / ".cache" / "agentevals" / "evaluators"
_INDEX_TTL_SECONDS = 600

_require_index_membership = False


def require_index_membership(enabled: bool = True) -> None:
    """Restrict remote evaluators to refs listed in their source's index.

    Server processes enable this because their evaluator configs come from
    API callers. The CLI leaves it off so local configs can reference any
    file a source can fetch.
    """
    global _require_index_membership
    _require_index_membership = enabled


class RemoteEvaluatorRejected(ValueError):
    """A remote evaluator ref failed an integrity or policy check."""


def _make_private_dirs(root: Path, target: Path) -> None:
    """Create ``target`` and every directory between it and ``root`` with mode 0700."""
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    root.chmod(0o700)
    current = root
    for part in target.relative_to(root).parts:
        current = current / part
        if current.is_symlink():
            raise RemoteEvaluatorRejected("Refusing to use a symlinked evaluator cache directory")
        current.mkdir(mode=0o700, exist_ok=True)
        current.chmod(0o700)


class EvaluatorResolver:
    """Downloads and caches remote evaluators, converting them to local paths."""

    def __init__(self, cache_dir: Path | None = None):
        self._cache_dir = cache_dir or _DEFAULT_CACHE_DIR
        self._sources: dict[str, EvaluatorSource] = {}
        self._index_cache: dict[str, tuple[float, frozenset[str]]] = {}

    def register_source(self, source: EvaluatorSource) -> None:
        self._sources[source.source_name] = source

    async def _indexed_refs(self, source: EvaluatorSource) -> frozenset[str]:
        cached = self._index_cache.get(source.source_name)
        if cached is not None and time.monotonic() - cached[0] < _INDEX_TTL_SECONDS:
            return cached[1]
        try:
            infos = await source.list_evaluators()
        except Exception:
            logger.warning("Could not load the evaluator index for source '%s'", source.source_name, exc_info=True)
            infos = []
        refs = frozenset(info.ref for info in infos if info.ref)
        # An empty result usually means the index fetch failed; do not pin it for the full TTL.
        if refs:
            self._index_cache[source.source_name] = (time.monotonic(), refs)
        return refs

    async def resolve(self, evaluator_def) -> "CodeEvaluatorDef":  # noqa: F821
        """Download a remote evaluator and return a CodeEvaluatorDef pointing to the cached file."""
        from ..config import CodeEvaluatorDef, RemoteEvaluatorDef, validate_remote_ref

        if not isinstance(evaluator_def, RemoteEvaluatorDef):
            raise TypeError(f"Expected RemoteEvaluatorDef, got {type(evaluator_def).__name__}")

        source = self._sources.get(evaluator_def.source)
        if source is None:
            raise ValueError(
                f"Unknown evaluator source '{evaluator_def.source}'. Available: {sorted(self._sources.keys())}"
            )

        # Re-validate in case the definition was built with model_construct or mutated after validation.
        ref = validate_remote_ref(evaluator_def.ref)

        if _require_index_membership and ref not in await self._indexed_refs(source):
            raise RemoteEvaluatorRejected(
                f"Remote evaluator ref '{ref}' is not listed in the '{source.source_name}' evaluator index "
                "(or the index could not be loaded)"
            )

        root = self._cache_dir.resolve()
        dest = root / source.source_name / ref
        _make_private_dirs(root, dest.parent)
        if not dest.parent.resolve().is_relative_to(root):
            raise RemoteEvaluatorRejected(f"Evaluator cache path for ref '{ref}' escapes the cache directory")

        if dest.is_symlink():
            raise RemoteEvaluatorRejected(f"Refusing to use a symlinked cached evaluator for ref '{ref}'")

        if not dest.exists():
            logger.info(
                "Downloading evaluator '%s' from %s (ref: %s)",
                evaluator_def.name,
                evaluator_def.source,
                ref,
            )
            await source.fetch_evaluator(ref, dest)
        else:
            logger.debug("Using cached evaluator '%s' at %s", evaluator_def.name, dest)

        if dest.is_symlink() or not dest.is_file() or not dest.resolve().is_relative_to(root):
            raise RemoteEvaluatorRejected(f"Cached evaluator for ref '{ref}' is not a regular file inside the cache")

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
