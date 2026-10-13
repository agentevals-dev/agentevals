import datetime

from google.protobuf import duration_pb2 as _duration_pb2
from google.protobuf import struct_pb2 as _struct_pb2
from google.protobuf import timestamp_pb2 as _timestamp_pb2
from opentelemetry.proto.resource.v1 import resource_pb2 as _resource_pb2
from opentelemetry.proto.trace.v1 import trace_pb2 as _trace_pb2
from google.protobuf.internal import containers as _containers
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class SessionState(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    SESSION_STATE_UNSPECIFIED: _ClassVar[SessionState]
    SESSION_STATE_LIVE: _ClassVar[SessionState]
    SESSION_STATE_COMPLETE: _ClassVar[SessionState]

class ContentCapture(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    CONTENT_CAPTURE_UNSPECIFIED: _ClassVar[ContentCapture]
    CONTENT_CAPTURE_ON: _ClassVar[ContentCapture]
    CONTENT_CAPTURE_OFF: _ClassVar[ContentCapture]
    CONTENT_CAPTURE_PARTIAL: _ClassVar[ContentCapture]
SESSION_STATE_UNSPECIFIED: SessionState
SESSION_STATE_LIVE: SessionState
SESSION_STATE_COMPLETE: SessionState
CONTENT_CAPTURE_UNSPECIFIED: ContentCapture
CONTENT_CAPTURE_ON: ContentCapture
CONTENT_CAPTURE_OFF: ContentCapture
CONTENT_CAPTURE_PARTIAL: ContentCapture

class Usage(_message.Message):
    __slots__ = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_write_input_tokens", "reasoning_output_tokens")
    INPUT_TOKENS_FIELD_NUMBER: _ClassVar[int]
    OUTPUT_TOKENS_FIELD_NUMBER: _ClassVar[int]
    CACHE_READ_INPUT_TOKENS_FIELD_NUMBER: _ClassVar[int]
    CACHE_WRITE_INPUT_TOKENS_FIELD_NUMBER: _ClassVar[int]
    REASONING_OUTPUT_TOKENS_FIELD_NUMBER: _ClassVar[int]
    input_tokens: int
    output_tokens: int
    cache_read_input_tokens: int
    cache_write_input_tokens: int
    reasoning_output_tokens: int
    def __init__(self, input_tokens: _Optional[int] = ..., output_tokens: _Optional[int] = ..., cache_read_input_tokens: _Optional[int] = ..., cache_write_input_tokens: _Optional[int] = ..., reasoning_output_tokens: _Optional[int] = ...) -> None: ...

class Gap(_message.Message):
    __slots__ = ("code", "reason")
    CODE_FIELD_NUMBER: _ClassVar[int]
    REASON_FIELD_NUMBER: _ClassVar[int]
    code: str
    reason: str
    def __init__(self, code: _Optional[str] = ..., reason: _Optional[str] = ...) -> None: ...

class EvaluationResult(_message.Message):
    __slots__ = ("name", "score_value", "score_label", "explanation", "error_type", "trace_id", "span_id", "response_id", "source_service")
    NAME_FIELD_NUMBER: _ClassVar[int]
    SCORE_VALUE_FIELD_NUMBER: _ClassVar[int]
    SCORE_LABEL_FIELD_NUMBER: _ClassVar[int]
    EXPLANATION_FIELD_NUMBER: _ClassVar[int]
    ERROR_TYPE_FIELD_NUMBER: _ClassVar[int]
    TRACE_ID_FIELD_NUMBER: _ClassVar[int]
    SPAN_ID_FIELD_NUMBER: _ClassVar[int]
    RESPONSE_ID_FIELD_NUMBER: _ClassVar[int]
    SOURCE_SERVICE_FIELD_NUMBER: _ClassVar[int]
    name: str
    score_value: float
    score_label: str
    explanation: str
    error_type: str
    trace_id: str
    span_id: str
    response_id: str
    source_service: str
    def __init__(self, name: _Optional[str] = ..., score_value: _Optional[float] = ..., score_label: _Optional[str] = ..., explanation: _Optional[str] = ..., error_type: _Optional[str] = ..., trace_id: _Optional[str] = ..., span_id: _Optional[str] = ..., response_id: _Optional[str] = ..., source_service: _Optional[str] = ...) -> None: ...

class ToolCallView(_message.Message):
    __slots__ = ("span_id", "name", "call_id", "arguments", "result", "is_error", "error_type", "started_at", "duration")
    SPAN_ID_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    CALL_ID_FIELD_NUMBER: _ClassVar[int]
    ARGUMENTS_FIELD_NUMBER: _ClassVar[int]
    RESULT_FIELD_NUMBER: _ClassVar[int]
    IS_ERROR_FIELD_NUMBER: _ClassVar[int]
    ERROR_TYPE_FIELD_NUMBER: _ClassVar[int]
    STARTED_AT_FIELD_NUMBER: _ClassVar[int]
    DURATION_FIELD_NUMBER: _ClassVar[int]
    span_id: str
    name: str
    call_id: str
    arguments: _struct_pb2.Value
    result: _struct_pb2.Value
    is_error: bool
    error_type: str
    started_at: _timestamp_pb2.Timestamp
    duration: _duration_pb2.Duration
    def __init__(self, span_id: _Optional[str] = ..., name: _Optional[str] = ..., call_id: _Optional[str] = ..., arguments: _Optional[_Union[_struct_pb2.Value, _Mapping]] = ..., result: _Optional[_Union[_struct_pb2.Value, _Mapping]] = ..., is_error: _Optional[bool] = ..., error_type: _Optional[str] = ..., started_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., duration: _Optional[_Union[datetime.timedelta, _duration_pb2.Duration, _Mapping]] = ...) -> None: ...

class ModelCallView(_message.Message):
    __slots__ = ("span_id", "operation", "provider", "request_model", "response_model", "response_id", "usage", "finish_reasons", "status", "error_type", "started_at", "duration", "agent_name")
    SPAN_ID_FIELD_NUMBER: _ClassVar[int]
    OPERATION_FIELD_NUMBER: _ClassVar[int]
    PROVIDER_FIELD_NUMBER: _ClassVar[int]
    REQUEST_MODEL_FIELD_NUMBER: _ClassVar[int]
    RESPONSE_MODEL_FIELD_NUMBER: _ClassVar[int]
    RESPONSE_ID_FIELD_NUMBER: _ClassVar[int]
    USAGE_FIELD_NUMBER: _ClassVar[int]
    FINISH_REASONS_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    ERROR_TYPE_FIELD_NUMBER: _ClassVar[int]
    STARTED_AT_FIELD_NUMBER: _ClassVar[int]
    DURATION_FIELD_NUMBER: _ClassVar[int]
    AGENT_NAME_FIELD_NUMBER: _ClassVar[int]
    span_id: str
    operation: str
    provider: str
    request_model: str
    response_model: str
    response_id: str
    usage: Usage
    finish_reasons: _containers.RepeatedScalarFieldContainer[str]
    status: _trace_pb2.Status.StatusCode
    error_type: str
    started_at: _timestamp_pb2.Timestamp
    duration: _duration_pb2.Duration
    agent_name: str
    def __init__(self, span_id: _Optional[str] = ..., operation: _Optional[str] = ..., provider: _Optional[str] = ..., request_model: _Optional[str] = ..., response_model: _Optional[str] = ..., response_id: _Optional[str] = ..., usage: _Optional[_Union[Usage, _Mapping]] = ..., finish_reasons: _Optional[_Iterable[str]] = ..., status: _Optional[_Union[_trace_pb2.Status.StatusCode, str]] = ..., error_type: _Optional[str] = ..., started_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., duration: _Optional[_Union[datetime.timedelta, _duration_pb2.Duration, _Mapping]] = ..., agent_name: _Optional[str] = ...) -> None: ...

class TurnView(_message.Message):
    __slots__ = ("index", "trace_id", "span_id", "status", "error_type", "started_at", "duration", "agent_name", "user_text", "answer_text", "content_captured", "tool_calls", "model_calls", "usage", "external_evaluations", "gaps")
    INDEX_FIELD_NUMBER: _ClassVar[int]
    TRACE_ID_FIELD_NUMBER: _ClassVar[int]
    SPAN_ID_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    ERROR_TYPE_FIELD_NUMBER: _ClassVar[int]
    STARTED_AT_FIELD_NUMBER: _ClassVar[int]
    DURATION_FIELD_NUMBER: _ClassVar[int]
    AGENT_NAME_FIELD_NUMBER: _ClassVar[int]
    USER_TEXT_FIELD_NUMBER: _ClassVar[int]
    ANSWER_TEXT_FIELD_NUMBER: _ClassVar[int]
    CONTENT_CAPTURED_FIELD_NUMBER: _ClassVar[int]
    TOOL_CALLS_FIELD_NUMBER: _ClassVar[int]
    MODEL_CALLS_FIELD_NUMBER: _ClassVar[int]
    USAGE_FIELD_NUMBER: _ClassVar[int]
    EXTERNAL_EVALUATIONS_FIELD_NUMBER: _ClassVar[int]
    GAPS_FIELD_NUMBER: _ClassVar[int]
    index: int
    trace_id: str
    span_id: str
    status: _trace_pb2.Status.StatusCode
    error_type: str
    started_at: _timestamp_pb2.Timestamp
    duration: _duration_pb2.Duration
    agent_name: str
    user_text: str
    answer_text: str
    content_captured: bool
    tool_calls: _containers.RepeatedCompositeFieldContainer[ToolCallView]
    model_calls: _containers.RepeatedCompositeFieldContainer[ModelCallView]
    usage: Usage
    external_evaluations: _containers.RepeatedCompositeFieldContainer[EvaluationResult]
    gaps: _containers.RepeatedCompositeFieldContainer[Gap]
    def __init__(self, index: _Optional[int] = ..., trace_id: _Optional[str] = ..., span_id: _Optional[str] = ..., status: _Optional[_Union[_trace_pb2.Status.StatusCode, str]] = ..., error_type: _Optional[str] = ..., started_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., duration: _Optional[_Union[datetime.timedelta, _duration_pb2.Duration, _Mapping]] = ..., agent_name: _Optional[str] = ..., user_text: _Optional[str] = ..., answer_text: _Optional[str] = ..., content_captured: _Optional[bool] = ..., tool_calls: _Optional[_Iterable[_Union[ToolCallView, _Mapping]]] = ..., model_calls: _Optional[_Iterable[_Union[ModelCallView, _Mapping]]] = ..., usage: _Optional[_Union[Usage, _Mapping]] = ..., external_evaluations: _Optional[_Iterable[_Union[EvaluationResult, _Mapping]]] = ..., gaps: _Optional[_Iterable[_Union[Gap, _Mapping]]] = ...) -> None: ...

class SessionTotals(_message.Message):
    __slots__ = ("turns", "failed_turns", "model_calls", "tool_calls", "usage", "model_calls_with_usage", "tool_calls_with_arguments")
    TURNS_FIELD_NUMBER: _ClassVar[int]
    FAILED_TURNS_FIELD_NUMBER: _ClassVar[int]
    MODEL_CALLS_FIELD_NUMBER: _ClassVar[int]
    TOOL_CALLS_FIELD_NUMBER: _ClassVar[int]
    USAGE_FIELD_NUMBER: _ClassVar[int]
    MODEL_CALLS_WITH_USAGE_FIELD_NUMBER: _ClassVar[int]
    TOOL_CALLS_WITH_ARGUMENTS_FIELD_NUMBER: _ClassVar[int]
    turns: int
    failed_turns: int
    model_calls: int
    tool_calls: int
    usage: Usage
    model_calls_with_usage: int
    tool_calls_with_arguments: int
    def __init__(self, turns: _Optional[int] = ..., failed_turns: _Optional[int] = ..., model_calls: _Optional[int] = ..., tool_calls: _Optional[int] = ..., usage: _Optional[_Union[Usage, _Mapping]] = ..., model_calls_with_usage: _Optional[int] = ..., tool_calls_with_arguments: _Optional[int] = ...) -> None: ...

class SessionSummary(_message.Message):
    __slots__ = ("id", "short_id", "agent", "agent_version", "state", "started_at", "ended_at", "duration", "first_user_text", "content_capture", "totals", "models", "resource", "conversation_id", "trace_ids", "eval_set_id", "span_count", "gaps")
    ID_FIELD_NUMBER: _ClassVar[int]
    SHORT_ID_FIELD_NUMBER: _ClassVar[int]
    AGENT_FIELD_NUMBER: _ClassVar[int]
    AGENT_VERSION_FIELD_NUMBER: _ClassVar[int]
    STATE_FIELD_NUMBER: _ClassVar[int]
    STARTED_AT_FIELD_NUMBER: _ClassVar[int]
    ENDED_AT_FIELD_NUMBER: _ClassVar[int]
    DURATION_FIELD_NUMBER: _ClassVar[int]
    FIRST_USER_TEXT_FIELD_NUMBER: _ClassVar[int]
    CONTENT_CAPTURE_FIELD_NUMBER: _ClassVar[int]
    TOTALS_FIELD_NUMBER: _ClassVar[int]
    MODELS_FIELD_NUMBER: _ClassVar[int]
    RESOURCE_FIELD_NUMBER: _ClassVar[int]
    CONVERSATION_ID_FIELD_NUMBER: _ClassVar[int]
    TRACE_IDS_FIELD_NUMBER: _ClassVar[int]
    EVAL_SET_ID_FIELD_NUMBER: _ClassVar[int]
    SPAN_COUNT_FIELD_NUMBER: _ClassVar[int]
    GAPS_FIELD_NUMBER: _ClassVar[int]
    id: str
    short_id: str
    agent: str
    agent_version: str
    state: SessionState
    started_at: _timestamp_pb2.Timestamp
    ended_at: _timestamp_pb2.Timestamp
    duration: _duration_pb2.Duration
    first_user_text: str
    content_capture: ContentCapture
    totals: SessionTotals
    models: _containers.RepeatedScalarFieldContainer[str]
    resource: _resource_pb2.Resource
    conversation_id: str
    trace_ids: _containers.RepeatedScalarFieldContainer[str]
    eval_set_id: str
    span_count: int
    gaps: _containers.RepeatedCompositeFieldContainer[Gap]
    def __init__(self, id: _Optional[str] = ..., short_id: _Optional[str] = ..., agent: _Optional[str] = ..., agent_version: _Optional[str] = ..., state: _Optional[_Union[SessionState, str]] = ..., started_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., ended_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., duration: _Optional[_Union[datetime.timedelta, _duration_pb2.Duration, _Mapping]] = ..., first_user_text: _Optional[str] = ..., content_capture: _Optional[_Union[ContentCapture, str]] = ..., totals: _Optional[_Union[SessionTotals, _Mapping]] = ..., models: _Optional[_Iterable[str]] = ..., resource: _Optional[_Union[_resource_pb2.Resource, _Mapping]] = ..., conversation_id: _Optional[str] = ..., trace_ids: _Optional[_Iterable[str]] = ..., eval_set_id: _Optional[str] = ..., span_count: _Optional[int] = ..., gaps: _Optional[_Iterable[_Union[Gap, _Mapping]]] = ...) -> None: ...

class Session(_message.Message):
    __slots__ = ("summary", "turns")
    SUMMARY_FIELD_NUMBER: _ClassVar[int]
    TURNS_FIELD_NUMBER: _ClassVar[int]
    summary: SessionSummary
    turns: _containers.RepeatedCompositeFieldContainer[TurnView]
    def __init__(self, summary: _Optional[_Union[SessionSummary, _Mapping]] = ..., turns: _Optional[_Iterable[_Union[TurnView, _Mapping]]] = ...) -> None: ...

class ConversationView(_message.Message):
    __slots__ = ("key", "conversation_id", "trace_ids", "turns", "gaps")
    KEY_FIELD_NUMBER: _ClassVar[int]
    CONVERSATION_ID_FIELD_NUMBER: _ClassVar[int]
    TRACE_IDS_FIELD_NUMBER: _ClassVar[int]
    TURNS_FIELD_NUMBER: _ClassVar[int]
    GAPS_FIELD_NUMBER: _ClassVar[int]
    key: str
    conversation_id: str
    trace_ids: _containers.RepeatedScalarFieldContainer[str]
    turns: _containers.RepeatedCompositeFieldContainer[TurnView]
    gaps: _containers.RepeatedCompositeFieldContainer[Gap]
    def __init__(self, key: _Optional[str] = ..., conversation_id: _Optional[str] = ..., trace_ids: _Optional[_Iterable[str]] = ..., turns: _Optional[_Iterable[_Union[TurnView, _Mapping]]] = ..., gaps: _Optional[_Iterable[_Union[Gap, _Mapping]]] = ...) -> None: ...
