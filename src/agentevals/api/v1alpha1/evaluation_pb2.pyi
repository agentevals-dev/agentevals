from agentevals.api.v1alpha1 import common_pb2 as _common_pb2
from google.protobuf import struct_pb2 as _struct_pb2
from google.protobuf.internal import containers as _containers
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class TrajectoryMatchType(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    TRAJECTORY_MATCH_TYPE_UNSPECIFIED: _ClassVar[TrajectoryMatchType]
    TRAJECTORY_MATCH_TYPE_EXACT: _ClassVar[TrajectoryMatchType]
    TRAJECTORY_MATCH_TYPE_IN_ORDER: _ClassVar[TrajectoryMatchType]
    TRAJECTORY_MATCH_TYPE_ANY_ORDER: _ClassVar[TrajectoryMatchType]

class EvalStatus(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    EVAL_STATUS_UNSPECIFIED: _ClassVar[EvalStatus]
    EVAL_STATUS_PASSED: _ClassVar[EvalStatus]
    EVAL_STATUS_FAILED: _ClassVar[EvalStatus]
    EVAL_STATUS_NOT_EVALUATED: _ClassVar[EvalStatus]
TRAJECTORY_MATCH_TYPE_UNSPECIFIED: TrajectoryMatchType
TRAJECTORY_MATCH_TYPE_EXACT: TrajectoryMatchType
TRAJECTORY_MATCH_TYPE_IN_ORDER: TrajectoryMatchType
TRAJECTORY_MATCH_TYPE_ANY_ORDER: TrajectoryMatchType
EVAL_STATUS_UNSPECIFIED: EvalStatus
EVAL_STATUS_PASSED: EvalStatus
EVAL_STATUS_FAILED: EvalStatus
EVAL_STATUS_NOT_EVALUATED: EvalStatus

class ListMetricsRequest(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class Metric(_message.Message):
    __slots__ = ("name", "category", "description", "requires_eval_set", "requires_llm", "requires_gcp", "requires_rubrics", "working")
    NAME_FIELD_NUMBER: _ClassVar[int]
    CATEGORY_FIELD_NUMBER: _ClassVar[int]
    DESCRIPTION_FIELD_NUMBER: _ClassVar[int]
    REQUIRES_EVAL_SET_FIELD_NUMBER: _ClassVar[int]
    REQUIRES_LLM_FIELD_NUMBER: _ClassVar[int]
    REQUIRES_GCP_FIELD_NUMBER: _ClassVar[int]
    REQUIRES_RUBRICS_FIELD_NUMBER: _ClassVar[int]
    WORKING_FIELD_NUMBER: _ClassVar[int]
    name: str
    category: str
    description: str
    requires_eval_set: bool
    requires_llm: bool
    requires_gcp: bool
    requires_rubrics: bool
    working: bool
    def __init__(self, name: _Optional[str] = ..., category: _Optional[str] = ..., description: _Optional[str] = ..., requires_eval_set: _Optional[bool] = ..., requires_llm: _Optional[bool] = ..., requires_gcp: _Optional[bool] = ..., requires_rubrics: _Optional[bool] = ..., working: _Optional[bool] = ...) -> None: ...

class ListMetricsResponse(_message.Message):
    __slots__ = ("metrics",)
    METRICS_FIELD_NUMBER: _ClassVar[int]
    metrics: _containers.RepeatedCompositeFieldContainer[Metric]
    def __init__(self, metrics: _Optional[_Iterable[_Union[Metric, _Mapping]]] = ...) -> None: ...

class BuiltinEvaluator(_message.Message):
    __slots__ = ("name", "threshold", "judge_model", "trajectory_match_type", "credential_ref", "judge_base_url", "rubrics")
    NAME_FIELD_NUMBER: _ClassVar[int]
    THRESHOLD_FIELD_NUMBER: _ClassVar[int]
    JUDGE_MODEL_FIELD_NUMBER: _ClassVar[int]
    TRAJECTORY_MATCH_TYPE_FIELD_NUMBER: _ClassVar[int]
    CREDENTIAL_REF_FIELD_NUMBER: _ClassVar[int]
    JUDGE_BASE_URL_FIELD_NUMBER: _ClassVar[int]
    RUBRICS_FIELD_NUMBER: _ClassVar[int]
    name: str
    threshold: float
    judge_model: str
    trajectory_match_type: TrajectoryMatchType
    credential_ref: str
    judge_base_url: str
    rubrics: _containers.RepeatedCompositeFieldContainer[Rubric]
    def __init__(self, name: _Optional[str] = ..., threshold: _Optional[float] = ..., judge_model: _Optional[str] = ..., trajectory_match_type: _Optional[_Union[TrajectoryMatchType, str]] = ..., credential_ref: _Optional[str] = ..., judge_base_url: _Optional[str] = ..., rubrics: _Optional[_Iterable[_Union[Rubric, _Mapping]]] = ...) -> None: ...

class Rubric(_message.Message):
    __slots__ = ("id", "text")
    ID_FIELD_NUMBER: _ClassVar[int]
    TEXT_FIELD_NUMBER: _ClassVar[int]
    id: str
    text: str
    def __init__(self, id: _Optional[str] = ..., text: _Optional[str] = ...) -> None: ...

class CodeEvaluator(_message.Message):
    __slots__ = ("name", "threshold", "timeout", "config", "executor", "path")
    NAME_FIELD_NUMBER: _ClassVar[int]
    THRESHOLD_FIELD_NUMBER: _ClassVar[int]
    TIMEOUT_FIELD_NUMBER: _ClassVar[int]
    CONFIG_FIELD_NUMBER: _ClassVar[int]
    EXECUTOR_FIELD_NUMBER: _ClassVar[int]
    PATH_FIELD_NUMBER: _ClassVar[int]
    name: str
    threshold: float
    timeout: int
    config: _struct_pb2.Struct
    executor: str
    path: str
    def __init__(self, name: _Optional[str] = ..., threshold: _Optional[float] = ..., timeout: _Optional[int] = ..., config: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ..., executor: _Optional[str] = ..., path: _Optional[str] = ...) -> None: ...

class RemoteEvaluator(_message.Message):
    __slots__ = ("name", "threshold", "timeout", "config", "executor", "source", "ref")
    NAME_FIELD_NUMBER: _ClassVar[int]
    THRESHOLD_FIELD_NUMBER: _ClassVar[int]
    TIMEOUT_FIELD_NUMBER: _ClassVar[int]
    CONFIG_FIELD_NUMBER: _ClassVar[int]
    EXECUTOR_FIELD_NUMBER: _ClassVar[int]
    SOURCE_FIELD_NUMBER: _ClassVar[int]
    REF_FIELD_NUMBER: _ClassVar[int]
    name: str
    threshold: float
    timeout: int
    config: _struct_pb2.Struct
    executor: str
    source: str
    ref: str
    def __init__(self, name: _Optional[str] = ..., threshold: _Optional[float] = ..., timeout: _Optional[int] = ..., config: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ..., executor: _Optional[str] = ..., source: _Optional[str] = ..., ref: _Optional[str] = ...) -> None: ...

class Evaluator(_message.Message):
    __slots__ = ("builtin", "code", "remote")
    BUILTIN_FIELD_NUMBER: _ClassVar[int]
    CODE_FIELD_NUMBER: _ClassVar[int]
    REMOTE_FIELD_NUMBER: _ClassVar[int]
    builtin: BuiltinEvaluator
    code: CodeEvaluator
    remote: RemoteEvaluator
    def __init__(self, builtin: _Optional[_Union[BuiltinEvaluator, _Mapping]] = ..., code: _Optional[_Union[CodeEvaluator, _Mapping]] = ..., remote: _Optional[_Union[RemoteEvaluator, _Mapping]] = ...) -> None: ...

class EvalParams(_message.Message):
    __slots__ = ("evaluators", "max_concurrent_traces", "max_concurrent_evals", "group_by")
    EVALUATORS_FIELD_NUMBER: _ClassVar[int]
    MAX_CONCURRENT_TRACES_FIELD_NUMBER: _ClassVar[int]
    MAX_CONCURRENT_EVALS_FIELD_NUMBER: _ClassVar[int]
    GROUP_BY_FIELD_NUMBER: _ClassVar[int]
    evaluators: _containers.RepeatedCompositeFieldContainer[Evaluator]
    max_concurrent_traces: int
    max_concurrent_evals: int
    group_by: _common_pb2.GroupBy
    def __init__(self, evaluators: _Optional[_Iterable[_Union[Evaluator, _Mapping]]] = ..., max_concurrent_traces: _Optional[int] = ..., max_concurrent_evals: _Optional[int] = ..., group_by: _Optional[_Union[_common_pb2.GroupBy, str]] = ...) -> None: ...

class InvocationResult(_message.Message):
    __slots__ = ("score", "status")
    SCORE_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    score: float
    status: EvalStatus
    def __init__(self, score: _Optional[float] = ..., status: _Optional[_Union[EvalStatus, str]] = ...) -> None: ...

class MetricResult(_message.Message):
    __slots__ = ("metric_name", "score", "eval_status", "per_invocation", "error", "error_type", "details", "duration_ms")
    METRIC_NAME_FIELD_NUMBER: _ClassVar[int]
    SCORE_FIELD_NUMBER: _ClassVar[int]
    EVAL_STATUS_FIELD_NUMBER: _ClassVar[int]
    PER_INVOCATION_FIELD_NUMBER: _ClassVar[int]
    ERROR_FIELD_NUMBER: _ClassVar[int]
    ERROR_TYPE_FIELD_NUMBER: _ClassVar[int]
    DETAILS_FIELD_NUMBER: _ClassVar[int]
    DURATION_MS_FIELD_NUMBER: _ClassVar[int]
    metric_name: str
    score: float
    eval_status: EvalStatus
    per_invocation: _containers.RepeatedCompositeFieldContainer[InvocationResult]
    error: str
    error_type: str
    details: _struct_pb2.Struct
    duration_ms: float
    def __init__(self, metric_name: _Optional[str] = ..., score: _Optional[float] = ..., eval_status: _Optional[_Union[EvalStatus, str]] = ..., per_invocation: _Optional[_Iterable[_Union[InvocationResult, _Mapping]]] = ..., error: _Optional[str] = ..., error_type: _Optional[str] = ..., details: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ..., duration_ms: _Optional[float] = ...) -> None: ...

class TurnRef(_message.Message):
    __slots__ = ("trace_id", "span_id")
    TRACE_ID_FIELD_NUMBER: _ClassVar[int]
    SPAN_ID_FIELD_NUMBER: _ClassVar[int]
    trace_id: str
    span_id: str
    def __init__(self, trace_id: _Optional[str] = ..., span_id: _Optional[str] = ...) -> None: ...

class TraceResult(_message.Message):
    __slots__ = ("trace_id", "num_invocations", "metric_results", "conversion_warnings", "performance_metrics", "service_name", "agent_name", "trace_ids", "conversation_id", "group_key", "eval_case_id", "eval_case_match", "turn_refs")
    TRACE_ID_FIELD_NUMBER: _ClassVar[int]
    NUM_INVOCATIONS_FIELD_NUMBER: _ClassVar[int]
    METRIC_RESULTS_FIELD_NUMBER: _ClassVar[int]
    CONVERSION_WARNINGS_FIELD_NUMBER: _ClassVar[int]
    PERFORMANCE_METRICS_FIELD_NUMBER: _ClassVar[int]
    SERVICE_NAME_FIELD_NUMBER: _ClassVar[int]
    AGENT_NAME_FIELD_NUMBER: _ClassVar[int]
    TRACE_IDS_FIELD_NUMBER: _ClassVar[int]
    CONVERSATION_ID_FIELD_NUMBER: _ClassVar[int]
    GROUP_KEY_FIELD_NUMBER: _ClassVar[int]
    EVAL_CASE_ID_FIELD_NUMBER: _ClassVar[int]
    EVAL_CASE_MATCH_FIELD_NUMBER: _ClassVar[int]
    TURN_REFS_FIELD_NUMBER: _ClassVar[int]
    trace_id: str
    num_invocations: int
    metric_results: _containers.RepeatedCompositeFieldContainer[MetricResult]
    conversion_warnings: _containers.RepeatedScalarFieldContainer[str]
    performance_metrics: _struct_pb2.Struct
    service_name: str
    agent_name: str
    trace_ids: _containers.RepeatedScalarFieldContainer[str]
    conversation_id: str
    group_key: str
    eval_case_id: str
    eval_case_match: str
    turn_refs: _containers.RepeatedCompositeFieldContainer[TurnRef]
    def __init__(self, trace_id: _Optional[str] = ..., num_invocations: _Optional[int] = ..., metric_results: _Optional[_Iterable[_Union[MetricResult, _Mapping]]] = ..., conversion_warnings: _Optional[_Iterable[str]] = ..., performance_metrics: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ..., service_name: _Optional[str] = ..., agent_name: _Optional[str] = ..., trace_ids: _Optional[_Iterable[str]] = ..., conversation_id: _Optional[str] = ..., group_key: _Optional[str] = ..., eval_case_id: _Optional[str] = ..., eval_case_match: _Optional[str] = ..., turn_refs: _Optional[_Iterable[_Union[TurnRef, _Mapping]]] = ...) -> None: ...

class RunResult(_message.Message):
    __slots__ = ("trace_results", "errors", "performance_metrics", "run_id")
    TRACE_RESULTS_FIELD_NUMBER: _ClassVar[int]
    ERRORS_FIELD_NUMBER: _ClassVar[int]
    PERFORMANCE_METRICS_FIELD_NUMBER: _ClassVar[int]
    RUN_ID_FIELD_NUMBER: _ClassVar[int]
    trace_results: _containers.RepeatedCompositeFieldContainer[TraceResult]
    errors: _containers.RepeatedScalarFieldContainer[str]
    performance_metrics: _struct_pb2.Struct
    run_id: str
    def __init__(self, trace_results: _Optional[_Iterable[_Union[TraceResult, _Mapping]]] = ..., errors: _Optional[_Iterable[str]] = ..., performance_metrics: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ..., run_id: _Optional[str] = ...) -> None: ...

class EvaluateTracesRequest(_message.Message):
    __slots__ = ("traces", "adk_eval_set", "params", "credential_refs")
    class CredentialRefsEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: _struct_pb2.Struct
        def __init__(self, key: _Optional[str] = ..., value: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ...) -> None: ...
    TRACES_FIELD_NUMBER: _ClassVar[int]
    ADK_EVAL_SET_FIELD_NUMBER: _ClassVar[int]
    PARAMS_FIELD_NUMBER: _ClassVar[int]
    CREDENTIAL_REFS_FIELD_NUMBER: _ClassVar[int]
    traces: _common_pb2.TraceInput
    adk_eval_set: _struct_pb2.Struct
    params: EvalParams
    credential_refs: _containers.MessageMap[str, _struct_pb2.Struct]
    def __init__(self, traces: _Optional[_Union[_common_pb2.TraceInput, _Mapping]] = ..., adk_eval_set: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ..., params: _Optional[_Union[EvalParams, _Mapping]] = ..., credential_refs: _Optional[_Mapping[str, _struct_pb2.Struct]] = ...) -> None: ...

class EvaluationProgress(_message.Message):
    __slots__ = ("message",)
    MESSAGE_FIELD_NUMBER: _ClassVar[int]
    message: str
    def __init__(self, message: _Optional[str] = ...) -> None: ...

class GroupPerformance(_message.Message):
    __slots__ = ("trace_id", "performance_metrics", "trace_metadata")
    TRACE_ID_FIELD_NUMBER: _ClassVar[int]
    PERFORMANCE_METRICS_FIELD_NUMBER: _ClassVar[int]
    TRACE_METADATA_FIELD_NUMBER: _ClassVar[int]
    trace_id: str
    performance_metrics: _struct_pb2.Struct
    trace_metadata: _struct_pb2.Struct
    def __init__(self, trace_id: _Optional[str] = ..., performance_metrics: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ..., trace_metadata: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ...) -> None: ...

class EvaluateTracesResponse(_message.Message):
    __slots__ = ("progress", "performance", "trace_result", "done")
    PROGRESS_FIELD_NUMBER: _ClassVar[int]
    PERFORMANCE_FIELD_NUMBER: _ClassVar[int]
    TRACE_RESULT_FIELD_NUMBER: _ClassVar[int]
    DONE_FIELD_NUMBER: _ClassVar[int]
    progress: EvaluationProgress
    performance: GroupPerformance
    trace_result: TraceResult
    done: RunResult
    def __init__(self, progress: _Optional[_Union[EvaluationProgress, _Mapping]] = ..., performance: _Optional[_Union[GroupPerformance, _Mapping]] = ..., trace_result: _Optional[_Union[TraceResult, _Mapping]] = ..., done: _Optional[_Union[RunResult, _Mapping]] = ...) -> None: ...
