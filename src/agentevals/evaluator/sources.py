"""Evaluator source backends: discover and fetch evaluators from various registries."""

from __future__ import annotations

import abc
import json
import logging
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

_DEFAULT_REPO = "agentevals-dev/evaluators"
_DEFAULT_BRANCH = "main"
_DEFAULT_INDEX = "index.yaml"


@dataclass
class EvaluatorInfo:
    """Metadata for a single evaluator, regardless of where it comes from."""

    name: str
    description: str
    source: str
    language: str | None = None
    ref: str | None = None
    tags: list[str] = field(default_factory=list)
    author: str | None = None
    last_updated: str | None = None


class EvaluatorSource(abc.ABC):
    """Registry backend that can list and fetch evaluators."""

    @property
    @abc.abstractmethod
    def source_name(self) -> str: ...

    @abc.abstractmethod
    async def list_evaluators(self) -> list[EvaluatorInfo]: ...

    @abc.abstractmethod
    async def fetch_evaluator(self, ref: str, dest: Path) -> Path:
        """Download an evaluator identified by *ref* and write it to *dest*.

        Returns the path to the downloaded file.
        """


_CACHE_TTL_SECONDS = 86400  # 24 hours


def _cache_dir() -> Path:
    base = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    d = base / "agentevals"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _read_cache(key: str, ttl: int = _CACHE_TTL_SECONDS) -> list[EvaluatorInfo] | None:
    cache_file = _cache_dir() / f"{key}.json"
    if not cache_file.exists():
        return None
    try:
        data = json.loads(cache_file.read_text())
        if time.time() - data.get("ts", 0) > ttl:
            return None
        return [EvaluatorInfo(**item) for item in data["evaluators"]]
    except Exception:
        return None


def _write_cache(key: str, evaluators: list[EvaluatorInfo]) -> None:
    cache_file = _cache_dir() / f"{key}.json"
    try:
        cache_file.write_text(
            json.dumps(
                {
                    "ts": time.time(),
                    "evaluators": [asdict(g) for g in evaluators],
                }
            )
        )
    except Exception:
        pass


class BuiltinEvaluatorSource(EvaluatorSource):
    """Wraps ADK's built-in metric registry as an evaluator source."""

    @property
    def source_name(self) -> str:
        return "builtin"

    async def list_evaluators(self) -> list[EvaluatorInfo]:
        import asyncio
        import warnings

        cached = _read_cache("builtin")
        if cached is not None:
            return cached

        def _load() -> list[EvaluatorInfo]:
            infos: list[EvaluatorInfo] = []
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    from google.adk.evaluation.metric_evaluator_registry import (
                        DEFAULT_METRIC_EVALUATOR_REGISTRY,
                    )

                for m in DEFAULT_METRIC_EVALUATOR_REGISTRY.get_registered_metrics():
                    infos.append(
                        EvaluatorInfo(
                            name=m.metric_name,
                            description=m.description or "No description",
                            source=self.source_name,
                            language=None,
                            ref=None,
                        )
                    )
            except ImportError:
                from google.adk.evaluation.eval_metrics import PrebuiltMetrics

                for pm in PrebuiltMetrics:
                    infos.append(
                        EvaluatorInfo(
                            name=pm.value,
                            description="(install google-adk[eval] for full details)",
                            source=self.source_name,
                        )
                    )
            return infos

        result = await asyncio.to_thread(_load)
        _write_cache("builtin", result)
        return result

    async def fetch_evaluator(self, ref: str, dest: Path) -> Path:
        raise NotImplementedError("Built-in evaluators are part of ADK and cannot be fetched as files.")


class GitHubEvaluatorSource(EvaluatorSource):
    """Fetches evaluators from a GitHub repository with a CI-generated index.yaml."""

    def __init__(
        self,
        repo: str | None = None,
        branch: str | None = None,
        index_path: str | None = None,
        token: str | None = None,
    ):
        self._repo = repo or os.environ.get("AGENTEVALS_EVALUATOR_REPO", _DEFAULT_REPO)
        self._branch = branch or os.environ.get("AGENTEVALS_EVALUATOR_BRANCH", _DEFAULT_BRANCH)
        self._index_path = index_path or _DEFAULT_INDEX
        self._token = token or os.environ.get("AGENTEVALS_GITHUB_TOKEN") or os.environ.get("GITHUB_TOKEN")

    @property
    def source_name(self) -> str:
        return "github"

    def _raw_url(self, path: str) -> str:
        return f"https://raw.githubusercontent.com/{self._repo}/{self._branch}/{path}"

    def _headers(self) -> dict[str, str]:
        if self._token:
            return {"Authorization": f"token {self._token}"}
        return {}

    async def list_evaluators(self) -> list[EvaluatorInfo]:
        import httpx

        url = self._raw_url(self._index_path)
        logger.debug("Fetching evaluator index from %s", url)

        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(url, headers=self._headers(), timeout=15)
                resp.raise_for_status()
        except httpx.HTTPError as exc:
            logger.warning("Failed to fetch evaluator index from %s: %s", url, exc)
            return []

        data = yaml.safe_load(resp.text)
        if not isinstance(data, dict):
            logger.warning("Evaluator index at %s is not a valid YAML mapping", url)
            return []

        infos: list[EvaluatorInfo] = []
        for entry in data.get("evaluators", []):
            infos.append(
                EvaluatorInfo(
                    name=entry.get("name", "unknown"),
                    description=entry.get("description", ""),
                    source=self.source_name,
                    language=entry.get("language"),
                    ref=entry.get("path"),
                    tags=entry.get("tags", []),
                    author=entry.get("author"),
                    last_updated=entry.get("lastUpdated"),
                )
            )
        return infos

    async def fetch_evaluator(self, ref: str, dest: Path) -> Path:
        import httpx

        url = self._raw_url(ref)
        logger.info("Downloading evaluator from %s", url)

        async with httpx.AsyncClient() as client:
            resp = await client.get(url, headers=self._headers(), timeout=30)
            resp.raise_for_status()

        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(resp.text, encoding="utf-8")  # noqa: ASYNC240
        return dest


class FileEvaluatorSource(EvaluatorSource):
    """Reads evaluators from a local index.yaml file (same schema as GitHubEvaluatorSource).

    Useful for testing and local development.  Not registered in the default
    source list — instantiate directly when needed.
    """

    def __init__(self, path: Path):
        self._path = Path(path)
        if not self._path.exists():
            raise FileNotFoundError(f"Evaluator index file not found: {self._path}")

    @property
    def source_name(self) -> str:
        return "file"

    async def list_evaluators(self) -> list[EvaluatorInfo]:
        data = yaml.safe_load(self._path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            logger.warning("Evaluator index at %s is not a valid YAML mapping", self._path)
            return []

        infos: list[EvaluatorInfo] = []
        for entry in data.get("evaluators", []):
            infos.append(
                EvaluatorInfo(
                    name=entry.get("name", "unknown"),
                    description=entry.get("description", ""),
                    source=self.source_name,
                    language=entry.get("language"),
                    ref=entry.get("path"),
                    tags=entry.get("tags", []),
                    author=entry.get("author"),
                    last_updated=entry.get("lastUpdated"),
                )
            )
        return infos

    async def fetch_evaluator(self, ref: str, dest: Path) -> Path:
        src = (self._path.parent / ref).resolve()
        if not src.exists():
            raise FileNotFoundError(f"Evaluator file not found: {src} (ref: {ref}, index: {self._path})")
        import shutil

        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        return dest


# ---------------------------------------------------------------------------
# Source registry
# ---------------------------------------------------------------------------

_SOURCES: list[EvaluatorSource] | None = None


def get_sources() -> list[EvaluatorSource]:
    """Return all registered evaluator sources (lazily initialized)."""
    global _SOURCES
    if _SOURCES is None:
        _SOURCES = [
            BuiltinEvaluatorSource(),
            GitHubEvaluatorSource(),
        ]
    return _SOURCES


def register_source(source: EvaluatorSource) -> None:
    """Add a custom evaluator source to the registry."""
    get_sources().append(source)
