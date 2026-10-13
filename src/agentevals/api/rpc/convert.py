"""Conversions between the v1alpha1 messages and the Pydantic models.

Pydantic stays the only validator: a request becomes a dict with the model's field names and goes
through ``model_validate``. Enum spellings differ per field (``queued``, ``PASSED``,
``otlp-json``), so each enum has an explicit map. ``json_format`` is used for ``Struct`` and
``Value`` payloads only.
"""

from __future__ import annotations

import itertools
import json
import logging
import math
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from google.protobuf import json_format, struct_pb2, timestamp_pb2
from pydantic import ValidationError

from ...config import BuiltinMetricDef, CodeEvaluatorDef, EvalParams, RemoteEvaluatorDef
from ...loader import load_telemetry_text
from ...otel.decode import decode_logs_proto, decode_traces_proto
from ...otel.encode import encode_traces
from ...otel.model import Trace, build_traces
from ...runner import MetricResult, RunResult, TraceResult
from ...storage.models import Result, ResultStatus, Run, RunSpec, RunStatus
from ..redact import redact_run
from ..v1alpha1 import common_pb2, evaluation_pb2, runs_pb2
from ..views import json_safe
from .policy import ApiError

logger = logging.getLogger(__name__)

RUN_STATUS = {
    RunStatus.QUEUED: runs_pb2.RUN_STATUS_QUEUED,
    RunStatus.RUNNING: runs_pb2.RUN_STATUS_RUNNING,
    RunStatus.SUCCEEDED: runs_pb2.RUN_STATUS_SUCCEEDED,
    RunStatus.FAILED: runs_pb2.RUN_STATUS_FAILED,
    RunStatus.CANCELLED: runs_pb2.RUN_STATUS_CANCELLED,
}
RUN_STATUS_FROM_PB = {v: k for k, v in RUN_STATUS.items()}
RESULT_STATUS = {
    ResultStatus.PASSED: runs_pb2.RESULT_STATUS_PASSED,
    ResultStatus.FAILED: runs_pb2.RESULT_STATUS_FAILED,
    ResultStatus.ERRORED: runs_pb2.RESULT_STATUS_ERRORED,
    ResultStatus.SKIPPED: runs_pb2.RESULT_STATUS_SKIPPED,
}
EVALUATOR_TYPE = {
    "builtin": runs_pb2.EVALUATOR_TYPE_BUILTIN,
    "code": runs_pb2.EVALUATOR_TYPE_CODE,
    "remote": runs_pb2.EVALUATOR_TYPE_REMOTE,
}
EVAL_STATUS = {
    "PASSED": evaluation_pb2.EVAL_STATUS_PASSED,
    "FAILED": evaluation_pb2.EVAL_STATUS_FAILED,
    "NOT_EVALUATED": evaluation_pb2.EVAL_STATUS_NOT_EVALUATED,
}
GROUP_BY = {
    "auto": common_pb2.GROUP_BY_AUTO,
    "trace": common_pb2.GROUP_BY_TRACE,
    "conversation": common_pb2.GROUP_BY_CONVERSATION,
}
GROUP_BY_FROM_PB = {v: k for k, v in GROUP_BY.items()}
TRAJECTORY_MATCH = {
    "EXACT": evaluation_pb2.TRAJECTORY_MATCH_TYPE_EXACT,
    "IN_ORDER": evaluation_pb2.TRAJECTORY_MATCH_TYPE_IN_ORDER,
    "ANY_ORDER": evaluation_pb2.TRAJECTORY_MATCH_TYPE_ANY_ORDER,
}
TRAJECTORY_MATCH_FROM_PB = {v: k for k, v in TRAJECTORY_MATCH.items()}
# A JSON lines document is OTLP/JSON for the loaders, which split the lines themselves.
TRACE_FORMAT_FROM_PB = {
    common_pb2.TRACE_FORMAT_UNSPECIFIED: None,
    common_pb2.TRACE_FORMAT_OTLP_JSON: "otlp-json",
    common_pb2.TRACE_FORMAT_OTLP_JSONL: "otlp-json",
    common_pb2.TRACE_FORMAT_JAEGER_JSON: "jaeger-json",
}
TRACE_FORMAT = {"otlp-json": common_pb2.TRACE_FORMAT_OTLP_JSON, "jaeger-json": common_pb2.TRACE_FORMAT_JAEGER_JSON}

MAX_DOCUMENT_BYTES = 10 * 1024 * 1024


# ---------------------------------------------------------------- Struct and Value


def to_struct(data: Mapping[str, Any]) -> struct_pb2.Struct:
    struct = struct_pb2.Struct()
    json_format.ParseDict(json_safe(dict(data)), struct)
    return struct


def from_struct(struct: struct_pb2.Struct) -> dict[str, Any]:
    """The struct as a dict, with whole numbers as ints: a Struct stores every number as a double."""
    return _ints(json_format.MessageToDict(struct))


def _ints(value: Any) -> Any:
    if isinstance(value, float) and value.is_integer() and abs(value) <= 2**53:
        return int(value)
    if isinstance(value, dict):
        return {k: _ints(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_ints(v) for v in value]
    return value


def _timestamp(value: datetime | None) -> timestamp_pb2.Timestamp | None:
    if value is None:
        return None
    ts = timestamp_pb2.Timestamp()
    ts.FromDatetime(value if value.tzinfo else value.replace(tzinfo=UTC))
    return ts


def _finite(value: float | None) -> float | None:
    return value if value is not None and math.isfinite(value) else None


# ---------------------------------------------------------------- traces


def load_trace_input(trace_input: common_pb2.TraceInput) -> tuple[list[Trace], list[str]]:
    """Traces of the OTLP protobuf and every document, plus a warning per document not read."""
    traces: list[Trace] = []
    warnings: list[str] = []
    if trace_input.HasField("otlp"):
        traces.extend(_otlp_traces(trace_input.otlp))
    for index, document in enumerate(trace_input.documents):
        name = document.name or f"document {index + 1}"
        if len(document.content) > MAX_DOCUMENT_BYTES:
            raise ApiError.invalid(f"{name} exceeds 10MB limit")
        try:
            text = document.content.decode("utf-8")
            traces.extend(load_telemetry_text(text, format=TRACE_FORMAT_FROM_PB[document.format], name=name).traces)
        except (UnicodeDecodeError, ValueError) as exc:
            warnings.append(f"Failed to load {name}: {exc}")
    return traces, warnings


def _otlp_traces(otlp: common_pb2.OtlpTelemetry) -> list[Trace]:
    decoded = decode_traces_proto(otlp.traces)
    logs = decode_logs_proto(otlp.logs).logs if otlp.HasField("logs") else []
    traces, _ = build_traces(decoded.spans, logs)
    return traces


# ---------------------------------------------------------------- evaluation parameters


def eval_params_dict(params: evaluation_pb2.EvalParams) -> dict[str, Any]:
    """The EvalParams fields that are set, under the Pydantic names; unset fields keep their defaults."""
    out: dict[str, Any] = {}
    if params.evaluators:
        out["evaluators"] = [_evaluator_dict(e) for e in params.evaluators]
    for field in ("max_concurrent_traces", "max_concurrent_evals"):
        if params.HasField(field):
            out[field] = getattr(params, field)
    if params.group_by in GROUP_BY_FROM_PB:
        out["group_by"] = GROUP_BY_FROM_PB[params.group_by]
    return out


def eval_params_from_pb(params: evaluation_pb2.EvalParams) -> EvalParams:
    return EvalParams.model_validate(eval_params_dict(params))


def _evaluator_dict(evaluator: evaluation_pb2.Evaluator) -> dict[str, Any]:
    kind = evaluator.WhichOneof("kind")
    if kind is None:
        raise ApiError.invalid("each evaluator needs one of builtin, code or remote")
    message = getattr(evaluator, kind)
    out: dict[str, Any] = {"type": kind, "name": message.name}
    if message.HasField("threshold"):
        out["threshold"] = message.threshold
    if kind == "builtin":
        for field in ("judge_model", "credential_ref", "judge_base_url"):
            if getattr(message, field):
                out[field] = getattr(message, field)
        if message.trajectory_match_type in TRAJECTORY_MATCH_FROM_PB:
            out["trajectory_match_type"] = TRAJECTORY_MATCH_FROM_PB[message.trajectory_match_type]
        return out
    if message.HasField("timeout"):
        out["timeout"] = message.timeout
    if message.HasField("config"):
        out["config"] = from_struct(message.config)
    if message.executor:
        out["executor"] = message.executor
    if kind == "code":
        out["path"] = message.path
    else:
        if message.source:
            out["source"] = message.source
        out["ref"] = message.ref
    return out


def eval_params_to_pb(params: EvalParams | Mapping[str, Any]) -> evaluation_pb2.EvalParams:
    if not isinstance(params, EvalParams):
        params = _stored_eval_params(params)
    return evaluation_pb2.EvalParams(
        evaluators=[_evaluator_to_pb(e) for e in params.evaluators],
        max_concurrent_traces=params.max_concurrent_traces,
        max_concurrent_evals=params.max_concurrent_evals,
        group_by=GROUP_BY.get(params.group_by, common_pb2.GROUP_BY_UNSPECIFIED),
    )


def _stored_eval_params(data: Mapping[str, Any]) -> EvalParams:
    """Parameters of a stored run. Runs recorded by older versions may carry other keys."""
    try:
        return EvalParams.model_validate({k: v for k, v in data.items() if k in EvalParams.model_fields})
    except ValidationError:
        logger.warning("Stored eval_config does not validate; returning defaults")
        return EvalParams()


def _evaluator_to_pb(evaluator: Any) -> evaluation_pb2.Evaluator:
    if isinstance(evaluator, BuiltinMetricDef):
        builtin = evaluation_pb2.BuiltinEvaluator(
            name=evaluator.name,
            judge_model=evaluator.judge_model or "",
            trajectory_match_type=TRAJECTORY_MATCH.get(evaluator.trajectory_match_type or "", 0),
            credential_ref=evaluator.credential_ref or "",
            judge_base_url=evaluator.judge_base_url or "",
        )
        if evaluator.threshold is not None:
            builtin.threshold = evaluator.threshold
        return evaluation_pb2.Evaluator(builtin=builtin)
    common = {
        "name": evaluator.name,
        "threshold": evaluator.threshold,
        "timeout": evaluator.timeout,
        "config": to_struct(evaluator.config),
        "executor": evaluator.executor,
    }
    if isinstance(evaluator, CodeEvaluatorDef):
        return evaluation_pb2.Evaluator(code=evaluation_pb2.CodeEvaluator(path=evaluator.path, **common))
    if isinstance(evaluator, RemoteEvaluatorDef):
        return evaluation_pb2.Evaluator(
            remote=evaluation_pb2.RemoteEvaluator(source=evaluator.source, ref=evaluator.ref, **common)
        )
    raise TypeError(f"unknown evaluator type {type(evaluator).__name__}")


# ---------------------------------------------------------------- evaluation results


def _invocations(scores: list[float | None], statuses: list[str]) -> list[evaluation_pb2.InvocationResult]:
    out = []
    for score, status in itertools.zip_longest(scores, statuses):
        result = evaluation_pb2.InvocationResult(status=EVAL_STATUS.get(status or "", 0))
        if _finite(score) is not None:
            result.score = score
        out.append(result)
    return out


def metric_result_to_pb(result: MetricResult) -> evaluation_pb2.MetricResult:
    out = evaluation_pb2.MetricResult(
        metric_name=result.metric_name,
        eval_status=EVAL_STATUS.get(result.eval_status, 0),
        per_invocation=_invocations(result.per_invocation_scores, result.per_invocation_statuses),
    )
    if _finite(result.score) is not None:
        out.score = result.score
    if result.error is not None:
        out.error = result.error
    if result.error_type is not None:
        out.error_type = result.error_type
    if result.details:
        out.details.CopyFrom(to_struct(result.details))
    if _finite(result.duration_ms) is not None:
        out.duration_ms = result.duration_ms
    return out


def trace_result_to_pb(result: TraceResult) -> evaluation_pb2.TraceResult:
    out = evaluation_pb2.TraceResult(
        trace_id=result.trace_id,
        num_invocations=result.num_invocations,
        metric_results=[metric_result_to_pb(m) for m in result.metric_results],
        conversion_warnings=result.conversion_warnings,
        service_name=result.service_name or "",
        agent_name=result.agent_name or "",
        trace_ids=result.trace_ids,
        conversation_id=result.conversation_id or "",
        group_key=result.group_key or "",
        eval_case_id=result.eval_case_id or "",
        eval_case_match=result.eval_case_match or "",
        turn_refs=[evaluation_pb2.TurnRef(trace_id=r.trace_id, span_id=r.span_id) for r in result.turn_refs],
    )
    if result.performance_metrics:
        out.performance_metrics.CopyFrom(to_struct(result.performance_metrics))
    return out


def run_result_to_pb(result: RunResult) -> evaluation_pb2.RunResult:
    out = evaluation_pb2.RunResult(
        trace_results=[trace_result_to_pb(t) for t in result.trace_results],
        errors=result.errors,
        run_id=result.run_id or "",
    )
    if result.performance_metrics:
        out.performance_metrics.CopyFrom(to_struct(result.performance_metrics))
    return out


# ---------------------------------------------------------------- runs


def run_spec_from_pb(spec: runs_pb2.RunSpec) -> RunSpec:
    if spec.approach not in (runs_pb2.RUN_APPROACH_UNSPECIFIED, runs_pb2.RUN_APPROACH_TRACE_REPLAY):
        raise ApiError.invalid("approach must be RUN_APPROACH_TRACE_REPLAY")
    data: dict[str, Any] = {
        "approach": "trace_replay",
        "target": _target_dict(spec.target),
        "eval_config": eval_params_from_pb(spec.eval_config).model_dump(by_alias=False),
        "sinks": [from_struct(s) for s in spec.sinks],
        "context": {"headers": dict(spec.context.headers)} if spec.context.headers else {},
    }
    if spec.HasField("adk_eval_set"):
        data["eval_set"] = from_struct(spec.adk_eval_set)
    if spec.credential_refs:
        data["credential_refs"] = {name: from_struct(ref) for name, ref in spec.credential_refs.items()}
    return RunSpec.model_validate(data)


def _target_dict(target: runs_pb2.TraceTarget) -> dict[str, Any]:
    kind = target.WhichOneof("kind")
    if kind == "inline":
        if len(target.inline.content) > MAX_DOCUMENT_BYTES:
            raise ApiError.invalid("target.inline exceeds 10MB limit")
        try:
            document = json.loads(target.inline.content)
        except (UnicodeDecodeError, ValueError) as exc:
            raise ApiError.invalid("target.inline must be one JSON document") from exc
        if not isinstance(document, dict):
            raise ApiError.invalid("target.inline must be a JSON object")
        return {"kind": "inline", "inline": document, "trace_format": TRACE_FORMAT_FROM_PB[target.inline.format]}
    if kind == "otlp":
        return {"kind": "inline", "inline": encode_traces(_otlp_traces(target.otlp)), "trace_format": "otlp-json"}
    if kind == "http":
        return {
            "kind": "http",
            "base_url": target.http.base_url,
            "trace_id": target.http.trace_id,
            "trace_format": TRACE_FORMAT_FROM_PB[target.http.format],
        }
    if kind == "uploaded":
        raise ApiError.invalid("target.uploaded is output only")
    raise ApiError.invalid("target needs one of inline, otlp or http")


def run_spec_to_pb(spec: RunSpec) -> runs_pb2.RunSpec:
    target = spec.target
    out = runs_pb2.RunSpec(
        approach=runs_pb2.RUN_APPROACH_TRACE_REPLAY,
        eval_config=eval_params_to_pb(spec.eval_config or {}),
        sinks=[to_struct(s) for s in spec.sinks],
        credential_refs={name: to_struct(ref) for name, ref in (spec.credential_refs or {}).items()},
    )
    trace_format = TRACE_FORMAT.get(target.trace_format or "", common_pb2.TRACE_FORMAT_UNSPECIFIED)
    if target.kind == "inline":
        content = json.dumps(target.inline or {}, separators=(",", ":")).encode()
        out.target.inline.CopyFrom(common_pb2.TraceDocument(content=content, format=trace_format))
    elif target.kind == "http":
        out.target.http.CopyFrom(
            runs_pb2.HttpTraceTarget(
                base_url=target.base_url or "", trace_id=target.trace_id or "", format=trace_format
            )
        )
    else:
        out.target.uploaded.CopyFrom(
            runs_pb2.UploadedTraces(trace_count=target.trace_count or 0, trace_files=target.trace_files or [])
        )
    if spec.eval_set is not None:
        out.adk_eval_set.CopyFrom(to_struct(spec.eval_set))
    headers = spec.context.get("headers") if isinstance(spec.context, dict) else None
    if isinstance(headers, dict):
        out.context.headers.update({str(k): str(v) for k, v in headers.items()})
    return out


def run_to_pb(run: Run) -> runs_pb2.Run:
    """The run as callers see it: redacted."""
    run = redact_run(run)
    out = runs_pb2.Run(
        run_id=str(run.run_id),
        status=RUN_STATUS[run.status],
        spec=run_spec_to_pb(run.spec),
        attempt=run.attempt,
        worker_id=run.worker_id or "",
        error=run.error or "",
        created_at=_timestamp(run.created_at),
        started_at=_timestamp(run.started_at),
        finished_at=_timestamp(run.finished_at),
        cancel_requested=run.cancel_requested,
    )
    if run.summary:
        out.summary.CopyFrom(to_struct(run.summary))
    return out


def result_to_pb(result: Result) -> runs_pb2.Result:
    out = runs_pb2.Result(
        result_id=result.result_id,
        run_id=str(result.run_id),
        eval_set_item_id=result.eval_set_item_id,
        eval_set_item_name=result.eval_set_item_name,
        evaluator_name=result.evaluator_name,
        evaluator_type=EVALUATOR_TYPE[result.evaluator_type],
        status=RESULT_STATUS[result.status],
        per_invocation=_invocations(result.per_invocation_scores, []),
        trace_id=result.trace_id or "",
        span_id=result.span_id or "",
        error_text=result.error_text or "",
        created_at=_timestamp(result.created_at),
    )
    if _finite(result.score) is not None:
        out.score = result.score
    if result.details:
        out.details.CopyFrom(to_struct(result.details))
    if result.tokens_used:
        out.tokens_used.CopyFrom(to_struct(result.tokens_used))
    if result.latency_ms is not None:
        out.latency_ms = result.latency_ms
    return out
