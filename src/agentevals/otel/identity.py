"""Resource attributes agentevals reads to route telemetry into sessions.

``agentevals.session_name`` names a session (reruns of a finished name become ``name-2``).
``agentevals.session.run_id`` separates runs that reuse a name; only the SDK sets it.
``agentevals.eval_set_id`` and ``agentevals.metadata.*`` are shown with the session.
"""

from __future__ import annotations

from typing import Any

SESSION_NAME = "agentevals.session_name"
SESSION_RUN_ID = "agentevals.session.run_id"
EVAL_SET_ID = "agentevals.eval_set_id"
METADATA_PREFIX = "agentevals.metadata."

# Instrumentation scope of the evaluation result records agentevals emits.
EMITTER_SCOPE = "agentevals"

MAX_KEY_LENGTH = 256


def coerce_key(value: Any) -> str | None:
    """A usable identity value: scalar, printable, at most 256 characters; otherwise ``None``."""
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None
    text = str(value).strip()
    if not text or len(text) > MAX_KEY_LENGTH or not text.isprintable():
        return None
    return text
