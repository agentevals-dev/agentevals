import datetime

from agentevals.api.v1alpha1 import common_pb2 as _common_pb2
from agentevals.api.v1alpha1 import evaluation_pb2 as _evaluation_pb2
from google.protobuf import struct_pb2 as _struct_pb2
from google.protobuf import timestamp_pb2 as _timestamp_pb2
from google.protobuf.internal import containers as _containers
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class RunStatus(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    RUN_STATUS_UNSPECIFIED: _ClassVar[RunStatus]
    RUN_STATUS_QUEUED: _ClassVar[RunStatus]
    RUN_STATUS_RUNNING: _ClassVar[RunStatus]
    RUN_STATUS_SUCCEEDED: _ClassVar[RunStatus]
    RUN_STATUS_FAILED: _ClassVar[RunStatus]
    RUN_STATUS_CANCELLED: _ClassVar[RunStatus]

class ResultStatus(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    RESULT_STATUS_UNSPECIFIED: _ClassVar[ResultStatus]
    RESULT_STATUS_PASSED: _ClassVar[ResultStatus]
    RESULT_STATUS_FAILED: _ClassVar[ResultStatus]
    RESULT_STATUS_ERRORED: _ClassVar[ResultStatus]
    RESULT_STATUS_SKIPPED: _ClassVar[ResultStatus]

class EvaluatorType(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    EVALUATOR_TYPE_UNSPECIFIED: _ClassVar[EvaluatorType]
    EVALUATOR_TYPE_BUILTIN: _ClassVar[EvaluatorType]
    EVALUATOR_TYPE_CODE: _ClassVar[EvaluatorType]
    EVALUATOR_TYPE_REMOTE: _ClassVar[EvaluatorType]

class RunApproach(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    RUN_APPROACH_UNSPECIFIED: _ClassVar[RunApproach]
    RUN_APPROACH_TRACE_REPLAY: _ClassVar[RunApproach]
RUN_STATUS_UNSPECIFIED: RunStatus
RUN_STATUS_QUEUED: RunStatus
RUN_STATUS_RUNNING: RunStatus
RUN_STATUS_SUCCEEDED: RunStatus
RUN_STATUS_FAILED: RunStatus
RUN_STATUS_CANCELLED: RunStatus
RESULT_STATUS_UNSPECIFIED: ResultStatus
RESULT_STATUS_PASSED: ResultStatus
RESULT_STATUS_FAILED: ResultStatus
RESULT_STATUS_ERRORED: ResultStatus
RESULT_STATUS_SKIPPED: ResultStatus
EVALUATOR_TYPE_UNSPECIFIED: EvaluatorType
EVALUATOR_TYPE_BUILTIN: EvaluatorType
EVALUATOR_TYPE_CODE: EvaluatorType
EVALUATOR_TYPE_REMOTE: EvaluatorType
RUN_APPROACH_UNSPECIFIED: RunApproach
RUN_APPROACH_TRACE_REPLAY: RunApproach

class HttpTraceTarget(_message.Message):
    __slots__ = ("base_url", "trace_id", "format")
    BASE_URL_FIELD_NUMBER: _ClassVar[int]
    TRACE_ID_FIELD_NUMBER: _ClassVar[int]
    FORMAT_FIELD_NUMBER: _ClassVar[int]
    base_url: str
    trace_id: str
    format: _common_pb2.TraceFormat
    def __init__(self, base_url: _Optional[str] = ..., trace_id: _Optional[str] = ..., format: _Optional[_Union[_common_pb2.TraceFormat, str]] = ...) -> None: ...

class UploadedTraces(_message.Message):
    __slots__ = ("trace_count", "trace_files")
    TRACE_COUNT_FIELD_NUMBER: _ClassVar[int]
    TRACE_FILES_FIELD_NUMBER: _ClassVar[int]
    trace_count: int
    trace_files: _containers.RepeatedScalarFieldContainer[str]
    def __init__(self, trace_count: _Optional[int] = ..., trace_files: _Optional[_Iterable[str]] = ...) -> None: ...

class TraceTarget(_message.Message):
    __slots__ = ("inline", "otlp", "http", "uploaded")
    INLINE_FIELD_NUMBER: _ClassVar[int]
    OTLP_FIELD_NUMBER: _ClassVar[int]
    HTTP_FIELD_NUMBER: _ClassVar[int]
    UPLOADED_FIELD_NUMBER: _ClassVar[int]
    inline: _common_pb2.TraceDocument
    otlp: _common_pb2.OtlpTelemetry
    http: HttpTraceTarget
    uploaded: UploadedTraces
    def __init__(self, inline: _Optional[_Union[_common_pb2.TraceDocument, _Mapping]] = ..., otlp: _Optional[_Union[_common_pb2.OtlpTelemetry, _Mapping]] = ..., http: _Optional[_Union[HttpTraceTarget, _Mapping]] = ..., uploaded: _Optional[_Union[UploadedTraces, _Mapping]] = ...) -> None: ...

class RunContext(_message.Message):
    __slots__ = ("headers",)
    class HeadersEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: str
        def __init__(self, key: _Optional[str] = ..., value: _Optional[str] = ...) -> None: ...
    HEADERS_FIELD_NUMBER: _ClassVar[int]
    headers: _containers.ScalarMap[str, str]
    def __init__(self, headers: _Optional[_Mapping[str, str]] = ...) -> None: ...

class RunSpec(_message.Message):
    __slots__ = ("approach", "target", "adk_eval_set", "eval_config", "sinks", "context", "credential_refs")
    class CredentialRefsEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: _struct_pb2.Struct
        def __init__(self, key: _Optional[str] = ..., value: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ...) -> None: ...
    APPROACH_FIELD_NUMBER: _ClassVar[int]
    TARGET_FIELD_NUMBER: _ClassVar[int]
    ADK_EVAL_SET_FIELD_NUMBER: _ClassVar[int]
    EVAL_CONFIG_FIELD_NUMBER: _ClassVar[int]
    SINKS_FIELD_NUMBER: _ClassVar[int]
    CONTEXT_FIELD_NUMBER: _ClassVar[int]
    CREDENTIAL_REFS_FIELD_NUMBER: _ClassVar[int]
    approach: RunApproach
    target: TraceTarget
    adk_eval_set: _struct_pb2.Struct
    eval_config: _evaluation_pb2.EvalParams
    sinks: _containers.RepeatedCompositeFieldContainer[_struct_pb2.Struct]
    context: RunContext
    credential_refs: _containers.MessageMap[str, _struct_pb2.Struct]
    def __init__(self, approach: _Optional[_Union[RunApproach, str]] = ..., target: _Optional[_Union[TraceTarget, _Mapping]] = ..., adk_eval_set: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ..., eval_config: _Optional[_Union[_evaluation_pb2.EvalParams, _Mapping]] = ..., sinks: _Optional[_Iterable[_Union[_struct_pb2.Struct, _Mapping]]] = ..., context: _Optional[_Union[RunContext, _Mapping]] = ..., credential_refs: _Optional[_Mapping[str, _struct_pb2.Struct]] = ...) -> None: ...

class Run(_message.Message):
    __slots__ = ("run_id", "status", "spec", "attempt", "worker_id", "error", "summary", "created_at", "started_at", "finished_at", "cancel_requested")
    RUN_ID_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    SPEC_FIELD_NUMBER: _ClassVar[int]
    ATTEMPT_FIELD_NUMBER: _ClassVar[int]
    WORKER_ID_FIELD_NUMBER: _ClassVar[int]
    ERROR_FIELD_NUMBER: _ClassVar[int]
    SUMMARY_FIELD_NUMBER: _ClassVar[int]
    CREATED_AT_FIELD_NUMBER: _ClassVar[int]
    STARTED_AT_FIELD_NUMBER: _ClassVar[int]
    FINISHED_AT_FIELD_NUMBER: _ClassVar[int]
    CANCEL_REQUESTED_FIELD_NUMBER: _ClassVar[int]
    run_id: str
    status: RunStatus
    spec: RunSpec
    attempt: int
    worker_id: str
    error: str
    summary: _struct_pb2.Struct
    created_at: _timestamp_pb2.Timestamp
    started_at: _timestamp_pb2.Timestamp
    finished_at: _timestamp_pb2.Timestamp
    cancel_requested: bool
    def __init__(self, run_id: _Optional[str] = ..., status: _Optional[_Union[RunStatus, str]] = ..., spec: _Optional[_Union[RunSpec, _Mapping]] = ..., attempt: _Optional[int] = ..., worker_id: _Optional[str] = ..., error: _Optional[str] = ..., summary: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ..., created_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., started_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., finished_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., cancel_requested: _Optional[bool] = ...) -> None: ...

class Result(_message.Message):
    __slots__ = ("result_id", "run_id", "eval_set_item_id", "eval_set_item_name", "evaluator_name", "evaluator_type", "status", "score", "per_invocation", "trace_id", "span_id", "details", "error_text", "tokens_used", "latency_ms", "created_at")
    RESULT_ID_FIELD_NUMBER: _ClassVar[int]
    RUN_ID_FIELD_NUMBER: _ClassVar[int]
    EVAL_SET_ITEM_ID_FIELD_NUMBER: _ClassVar[int]
    EVAL_SET_ITEM_NAME_FIELD_NUMBER: _ClassVar[int]
    EVALUATOR_NAME_FIELD_NUMBER: _ClassVar[int]
    EVALUATOR_TYPE_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    SCORE_FIELD_NUMBER: _ClassVar[int]
    PER_INVOCATION_FIELD_NUMBER: _ClassVar[int]
    TRACE_ID_FIELD_NUMBER: _ClassVar[int]
    SPAN_ID_FIELD_NUMBER: _ClassVar[int]
    DETAILS_FIELD_NUMBER: _ClassVar[int]
    ERROR_TEXT_FIELD_NUMBER: _ClassVar[int]
    TOKENS_USED_FIELD_NUMBER: _ClassVar[int]
    LATENCY_MS_FIELD_NUMBER: _ClassVar[int]
    CREATED_AT_FIELD_NUMBER: _ClassVar[int]
    result_id: str
    run_id: str
    eval_set_item_id: str
    eval_set_item_name: str
    evaluator_name: str
    evaluator_type: EvaluatorType
    status: ResultStatus
    score: float
    per_invocation: _containers.RepeatedCompositeFieldContainer[_evaluation_pb2.InvocationResult]
    trace_id: str
    span_id: str
    details: _struct_pb2.Struct
    error_text: str
    tokens_used: _struct_pb2.Struct
    latency_ms: int
    created_at: _timestamp_pb2.Timestamp
    def __init__(self, result_id: _Optional[str] = ..., run_id: _Optional[str] = ..., eval_set_item_id: _Optional[str] = ..., eval_set_item_name: _Optional[str] = ..., evaluator_name: _Optional[str] = ..., evaluator_type: _Optional[_Union[EvaluatorType, str]] = ..., status: _Optional[_Union[ResultStatus, str]] = ..., score: _Optional[float] = ..., per_invocation: _Optional[_Iterable[_Union[_evaluation_pb2.InvocationResult, _Mapping]]] = ..., trace_id: _Optional[str] = ..., span_id: _Optional[str] = ..., details: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ..., error_text: _Optional[str] = ..., tokens_used: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ..., latency_ms: _Optional[int] = ..., created_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ...) -> None: ...

class SubmitRunRequest(_message.Message):
    __slots__ = ("run_id", "spec")
    RUN_ID_FIELD_NUMBER: _ClassVar[int]
    SPEC_FIELD_NUMBER: _ClassVar[int]
    run_id: str
    spec: RunSpec
    def __init__(self, run_id: _Optional[str] = ..., spec: _Optional[_Union[RunSpec, _Mapping]] = ...) -> None: ...

class SubmitRunResponse(_message.Message):
    __slots__ = ("run",)
    RUN_FIELD_NUMBER: _ClassVar[int]
    run: Run
    def __init__(self, run: _Optional[_Union[Run, _Mapping]] = ...) -> None: ...

class GetRunRequest(_message.Message):
    __slots__ = ("run_id",)
    RUN_ID_FIELD_NUMBER: _ClassVar[int]
    run_id: str
    def __init__(self, run_id: _Optional[str] = ...) -> None: ...

class GetRunResponse(_message.Message):
    __slots__ = ("run",)
    RUN_FIELD_NUMBER: _ClassVar[int]
    run: Run
    def __init__(self, run: _Optional[_Union[Run, _Mapping]] = ...) -> None: ...

class ListRunsRequest(_message.Message):
    __slots__ = ("statuses", "page")
    STATUSES_FIELD_NUMBER: _ClassVar[int]
    PAGE_FIELD_NUMBER: _ClassVar[int]
    statuses: _containers.RepeatedScalarFieldContainer[RunStatus]
    page: _common_pb2.PageRequest
    def __init__(self, statuses: _Optional[_Iterable[_Union[RunStatus, str]]] = ..., page: _Optional[_Union[_common_pb2.PageRequest, _Mapping]] = ...) -> None: ...

class ListRunsResponse(_message.Message):
    __slots__ = ("runs", "page")
    RUNS_FIELD_NUMBER: _ClassVar[int]
    PAGE_FIELD_NUMBER: _ClassVar[int]
    runs: _containers.RepeatedCompositeFieldContainer[Run]
    page: _common_pb2.PageResponse
    def __init__(self, runs: _Optional[_Iterable[_Union[Run, _Mapping]]] = ..., page: _Optional[_Union[_common_pb2.PageResponse, _Mapping]] = ...) -> None: ...

class CancelRunRequest(_message.Message):
    __slots__ = ("run_id",)
    RUN_ID_FIELD_NUMBER: _ClassVar[int]
    run_id: str
    def __init__(self, run_id: _Optional[str] = ...) -> None: ...

class CancelRunResponse(_message.Message):
    __slots__ = ("run",)
    RUN_FIELD_NUMBER: _ClassVar[int]
    run: Run
    def __init__(self, run: _Optional[_Union[Run, _Mapping]] = ...) -> None: ...

class ListRunResultsRequest(_message.Message):
    __slots__ = ("run_id", "page")
    RUN_ID_FIELD_NUMBER: _ClassVar[int]
    PAGE_FIELD_NUMBER: _ClassVar[int]
    run_id: str
    page: _common_pb2.PageRequest
    def __init__(self, run_id: _Optional[str] = ..., page: _Optional[_Union[_common_pb2.PageRequest, _Mapping]] = ...) -> None: ...

class ListRunResultsResponse(_message.Message):
    __slots__ = ("results", "page")
    RESULTS_FIELD_NUMBER: _ClassVar[int]
    PAGE_FIELD_NUMBER: _ClassVar[int]
    results: _containers.RepeatedCompositeFieldContainer[Result]
    page: _common_pb2.PageResponse
    def __init__(self, results: _Optional[_Iterable[_Union[Result, _Mapping]]] = ..., page: _Optional[_Union[_common_pb2.PageResponse, _Mapping]] = ...) -> None: ...

class WatchRunRequest(_message.Message):
    __slots__ = ("run_id",)
    RUN_ID_FIELD_NUMBER: _ClassVar[int]
    run_id: str
    def __init__(self, run_id: _Optional[str] = ...) -> None: ...

class WatchRunResponse(_message.Message):
    __slots__ = ("run", "results")
    RUN_FIELD_NUMBER: _ClassVar[int]
    RESULTS_FIELD_NUMBER: _ClassVar[int]
    run: Run
    results: _containers.RepeatedCompositeFieldContainer[Result]
    def __init__(self, run: _Optional[_Union[Run, _Mapping]] = ..., results: _Optional[_Iterable[_Union[Result, _Mapping]]] = ...) -> None: ...
