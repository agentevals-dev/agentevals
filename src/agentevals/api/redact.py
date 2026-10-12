"""Secrets out of run specs on every read path.

A run is stored with what the worker needs: fetch headers, sink credentials, webhook URLs.
Callers get it back without them. Only the transport edge redacts; repositories and the worker
keep the real values.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit, urlunsplit

from ..storage.models import Run, RunSpec

REDACTED = "REDACTED"

_SECRET_KEY_PARTS = ("authorization", "password", "secret", "token", "api_key", "apikey", "private_key", "dsn")


def redact_run(run: Run) -> Run:
    return run.model_copy(update={"spec": redact_spec(run.spec)})


def redact_spec(spec: RunSpec) -> RunSpec:
    update: dict[str, Any] = {
        "context": _redact(spec.context),
        "sinks": [_redact(sink) for sink in spec.sinks],
    }
    if spec.target.base_url:
        update["target"] = spec.target.model_copy(update={"base_url": _without_credentials(spec.target.base_url)})
    return spec.model_copy(update=update)


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            name = str(key).lower()
            if name == "headers" and isinstance(item, dict):
                out[key] = {header: _header_value(v) for header, v in item.items()}
            elif any(part in name for part in _SECRET_KEY_PARTS) and not isinstance(item, (dict, list)):
                out[key] = REDACTED if item is not None else None
            elif name == "url" and isinstance(item, str):
                out[key] = _origin(item)
            else:
                out[key] = _redact(item)
        return out
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


def _header_value(value: Any) -> Any:
    """A literal header value is a secret; a ``{"from_env": NAME}`` reference is not."""
    if isinstance(value, dict) and set(value) == {"from_env"}:
        return value
    return REDACTED


def _origin(url: str) -> str:
    """Scheme and host only: webhook URLs often carry their token in the path or query."""
    parts = urlsplit(url)
    if not parts.scheme or not parts.hostname:
        return REDACTED
    host = parts.hostname if parts.port is None else f"{parts.hostname}:{parts.port}"
    rest = "/" + REDACTED if (parts.path not in ("", "/") or parts.query) else ""
    return f"{parts.scheme}://{host}{rest}"


def _without_credentials(url: str) -> str:
    """The URL without userinfo or query, either of which can carry a key."""
    parts = urlsplit(url)
    if parts.username is None and parts.password is None and not parts.query:
        return url
    host = parts.hostname or ""
    netloc = host if parts.port is None else f"{host}:{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path, "", ""))
