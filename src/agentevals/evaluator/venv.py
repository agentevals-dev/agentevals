"""Virtual environment management for evaluators with dependencies.

When an evaluator ships a ``requirements.txt`` alongside its entrypoint, we
create a cached venv, install the dependencies (plus the evaluator SDK), and
return the path to that venv's Python interpreter so the evaluator subprocess
runs in isolation.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

_VENV_CACHE_DIR = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "agentevals" / "venvs"
_HASH_FILE = ".requirements_hash"

# Per-evaluator locks to prevent concurrent venv creation for the same evaluator.
_venv_locks: dict[str, asyncio.Lock] = {}


def _venv_python(venv_dir: Path) -> Path:
    if sys.platform == "win32":
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def _venv_key(evaluator_path: Path) -> str:
    """Stable cache directory name derived from evaluator location."""
    resolved = evaluator_path.resolve()
    name = resolved.parent.name
    path_hash = hashlib.sha256(str(resolved.parent).encode()).hexdigest()[:8]
    return f"{name}-{path_hash}"


def _is_venv_valid(venv_dir: Path, req_hash: str) -> bool:
    hash_file = venv_dir / _HASH_FILE
    return (
        _venv_python(venv_dir).exists()
        and hash_file.exists()
        and hash_file.read_text(encoding="utf-8").strip() == req_hash
    )


def _create_venv(venv_dir: Path, uv: str | None) -> None:
    if venv_dir.exists():
        shutil.rmtree(venv_dir)
    cmd = (
        [uv, "venv", str(venv_dir), "--python", sys.executable] if uv else [sys.executable, "-m", "venv", str(venv_dir)]
    )
    subprocess.run(cmd, check=True, capture_output=True)


_VCS_PREFIXES = ("git+", "hg+", "svn+", "bzr+")
_ARCHIVE_SUFFIXES = (".whl", ".zip", ".tar.gz", ".tgz", ".tar.bz2", ".tar.xz")


def _requirement_problem(line: str) -> str | None:
    tokens = line.split()
    name = tokens[0]
    if name.startswith("-"):
        return "installer options are not allowed"
    if any(tok.startswith("-") and not tok.startswith("--hash=") for tok in tokens[1:]):
        return "only --hash options are allowed after a requirement"
    lowered = line.lower()
    if "://" in line or lowered.startswith(_VCS_PREFIXES) or "@" in line:
        return "URL, VCS and direct references are not allowed"
    if name.startswith((".", "/", "~")) or name.lower().endswith(_ARCHIVE_SUFFIXES):
        return "local paths and archives are not allowed"
    if "${" in line:
        return "environment variable references are not allowed"
    return None


def check_requirements(requirements: Path) -> None:
    """Allow only plain index requirements, so a requirements file cannot redirect the installer.

    Rejects installer options (another index, nested files, editable installs),
    URL, VCS and local path requirements, and ``${VAR}`` references, which pip
    expands from the server environment.
    """
    for lineno, raw in enumerate(requirements.read_text(encoding="utf-8").splitlines(), start=1):
        if raw.rstrip().endswith("\\"):
            raise ValueError(f"{requirements.name} line {lineno}: line continuations are not allowed")
        line = raw.strip()
        if line.startswith("#"):
            continue
        line = line.split(" #", 1)[0].split("\t#", 1)[0].strip()
        if not line:
            continue
        problem = _requirement_problem(line)
        if problem:
            raise ValueError(f"{requirements.name} line {lineno}: {problem}")


def _install_deps(venv_dir: Path, requirements: Path, uv: str | None) -> None:
    python = str(_venv_python(venv_dir))
    sdk_spec = "agentevals-evaluator-sdk"

    if uv:
        base = [uv, "pip", "install", "--python", python]
    else:
        base = [python, "-m", "pip", "install"]

    subprocess.run(base + [sdk_spec], check=True, capture_output=True)
    logger.info("Installing dependencies from %s ...", requirements.name)
    subprocess.run(base + ["-r", str(requirements)], check=True, capture_output=True)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def ensure_venv(evaluator_path: Path, *, strict_requirements: bool = False) -> Path | None:
    """Ensure a cached venv exists for *evaluator_path* if it has ``requirements.txt``.

    ``strict_requirements`` applies :func:`check_requirements`; it is set for
    downloaded evaluators, while local evaluators keep full pip syntax.

    Returns the venv Python path, or ``None`` if no venv is needed.
    """
    requirements = evaluator_path.resolve().parent / "requirements.txt"
    if not requirements.exists():
        return None

    if strict_requirements:
        check_requirements(requirements)

    req_hash = hashlib.sha256(requirements.read_bytes()).hexdigest()
    venv_dir = _VENV_CACHE_DIR / _venv_key(evaluator_path)

    if _is_venv_valid(venv_dir, req_hash):
        logger.debug("Using cached venv for %s at %s", evaluator_path.name, venv_dir)
        return _venv_python(venv_dir)

    uv = shutil.which("uv")
    logger.info(
        "Setting up environment for evaluator '%s' (using %s). This may take a while on first run...",
        evaluator_path.stem,
        "uv" if uv else "venv+pip",
    )

    try:
        venv_dir.parent.mkdir(parents=True, exist_ok=True)
        _create_venv(venv_dir, uv)
        _install_deps(venv_dir, requirements, uv)
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.decode() if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        raise RuntimeError(f"Failed to set up environment for evaluator '{evaluator_path.stem}': {stderr}") from exc

    (venv_dir / _HASH_FILE).write_text(req_hash, encoding="utf-8")
    logger.info("Environment ready for '%s'", evaluator_path.stem)
    return _venv_python(venv_dir)


async def ensure_venv_async(evaluator_path: Path, *, strict_requirements: bool = False) -> Path | None:
    """Async wrapper around :func:`ensure_venv` with per-evaluator locking."""
    venv_key = _venv_key(evaluator_path)
    if venv_key not in _venv_locks:
        _venv_locks[venv_key] = asyncio.Lock()

    async with _venv_locks[venv_key]:
        return await asyncio.to_thread(ensure_venv, evaluator_path, strict_requirements=strict_requirements)
