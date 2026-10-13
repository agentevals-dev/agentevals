import datetime

from google.protobuf import duration_pb2 as _duration_pb2
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class GetVersionRequest(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class GetVersionResponse(_message.Message):
    __slots__ = ("version",)
    VERSION_FIELD_NUMBER: _ClassVar[int]
    version: str
    def __init__(self, version: _Optional[str] = ...) -> None: ...

class GetConfigRequest(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class Ports(_message.Message):
    __slots__ = ("http", "otlp_http", "otlp_grpc", "api_grpc")
    HTTP_FIELD_NUMBER: _ClassVar[int]
    OTLP_HTTP_FIELD_NUMBER: _ClassVar[int]
    OTLP_GRPC_FIELD_NUMBER: _ClassVar[int]
    API_GRPC_FIELD_NUMBER: _ClassVar[int]
    http: int
    otlp_http: int
    otlp_grpc: int
    api_grpc: int
    def __init__(self, http: _Optional[int] = ..., otlp_http: _Optional[int] = ..., otlp_grpc: _Optional[int] = ..., api_grpc: _Optional[int] = ...) -> None: ...

class LiveLimits(_message.Message):
    __slots__ = ("max_sessions", "spans_per_trace", "spans_per_session", "traces_per_session", "max_bytes", "completion_grace", "idle_timeout", "session_ttl")
    MAX_SESSIONS_FIELD_NUMBER: _ClassVar[int]
    SPANS_PER_TRACE_FIELD_NUMBER: _ClassVar[int]
    SPANS_PER_SESSION_FIELD_NUMBER: _ClassVar[int]
    TRACES_PER_SESSION_FIELD_NUMBER: _ClassVar[int]
    MAX_BYTES_FIELD_NUMBER: _ClassVar[int]
    COMPLETION_GRACE_FIELD_NUMBER: _ClassVar[int]
    IDLE_TIMEOUT_FIELD_NUMBER: _ClassVar[int]
    SESSION_TTL_FIELD_NUMBER: _ClassVar[int]
    max_sessions: int
    spans_per_trace: int
    spans_per_session: int
    traces_per_session: int
    max_bytes: int
    completion_grace: _duration_pb2.Duration
    idle_timeout: _duration_pb2.Duration
    session_ttl: _duration_pb2.Duration
    def __init__(self, max_sessions: _Optional[int] = ..., spans_per_trace: _Optional[int] = ..., spans_per_session: _Optional[int] = ..., traces_per_session: _Optional[int] = ..., max_bytes: _Optional[int] = ..., completion_grace: _Optional[_Union[datetime.timedelta, _duration_pb2.Duration, _Mapping]] = ..., idle_timeout: _Optional[_Union[datetime.timedelta, _duration_pb2.Duration, _Mapping]] = ..., session_ttl: _Optional[_Union[datetime.timedelta, _duration_pb2.Duration, _Mapping]] = ...) -> None: ...

class JudgeKeys(_message.Message):
    __slots__ = ("google", "anthropic", "openai")
    GOOGLE_FIELD_NUMBER: _ClassVar[int]
    ANTHROPIC_FIELD_NUMBER: _ClassVar[int]
    OPENAI_FIELD_NUMBER: _ClassVar[int]
    google: bool
    anthropic: bool
    openai: bool
    def __init__(self, google: _Optional[bool] = ..., anthropic: _Optional[bool] = ..., openai: _Optional[bool] = ...) -> None: ...

class GetConfigResponse(_message.Message):
    __slots__ = ("ports", "live_limits", "runs_enabled", "evaluation_events_enabled", "judge_keys")
    PORTS_FIELD_NUMBER: _ClassVar[int]
    LIVE_LIMITS_FIELD_NUMBER: _ClassVar[int]
    RUNS_ENABLED_FIELD_NUMBER: _ClassVar[int]
    EVALUATION_EVENTS_ENABLED_FIELD_NUMBER: _ClassVar[int]
    JUDGE_KEYS_FIELD_NUMBER: _ClassVar[int]
    ports: Ports
    live_limits: LiveLimits
    runs_enabled: bool
    evaluation_events_enabled: bool
    judge_keys: JudgeKeys
    def __init__(self, ports: _Optional[_Union[Ports, _Mapping]] = ..., live_limits: _Optional[_Union[LiveLimits, _Mapping]] = ..., runs_enabled: _Optional[bool] = ..., evaluation_events_enabled: _Optional[bool] = ..., judge_keys: _Optional[_Union[JudgeKeys, _Mapping]] = ...) -> None: ...
