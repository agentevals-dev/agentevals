"""Resource attributes agentevals reads to route telemetry into sessions.

``agentevals.session_name`` names a session (reruns of a finished name become ``name-2``).
``agentevals.session.run_id`` separates runs that reuse a name; only the SDK sets it.
``agentevals.eval_set_id`` and ``agentevals.metadata.*`` are shown with the session.
"""

SESSION_NAME = "agentevals.session_name"
SESSION_RUN_ID = "agentevals.session.run_id"
EVAL_SET_ID = "agentevals.eval_set_id"
METADATA_PREFIX = "agentevals.metadata."
