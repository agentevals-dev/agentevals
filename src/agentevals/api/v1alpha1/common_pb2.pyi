from opentelemetry.proto.logs.v1 import logs_pb2 as _logs_pb2
from opentelemetry.proto.trace.v1 import trace_pb2 as _trace_pb2
from google.protobuf.internal import containers as _containers
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class TraceFormat(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    TRACE_FORMAT_UNSPECIFIED: _ClassVar[TraceFormat]
    TRACE_FORMAT_OTLP_JSON: _ClassVar[TraceFormat]
    TRACE_FORMAT_OTLP_JSONL: _ClassVar[TraceFormat]
    TRACE_FORMAT_JAEGER_JSON: _ClassVar[TraceFormat]

class GroupBy(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    GROUP_BY_UNSPECIFIED: _ClassVar[GroupBy]
    GROUP_BY_AUTO: _ClassVar[GroupBy]
    GROUP_BY_TRACE: _ClassVar[GroupBy]
    GROUP_BY_CONVERSATION: _ClassVar[GroupBy]
TRACE_FORMAT_UNSPECIFIED: TraceFormat
TRACE_FORMAT_OTLP_JSON: TraceFormat
TRACE_FORMAT_OTLP_JSONL: TraceFormat
TRACE_FORMAT_JAEGER_JSON: TraceFormat
GROUP_BY_UNSPECIFIED: GroupBy
GROUP_BY_AUTO: GroupBy
GROUP_BY_TRACE: GroupBy
GROUP_BY_CONVERSATION: GroupBy

class PageRequest(_message.Message):
    __slots__ = ("limit", "page_token")
    LIMIT_FIELD_NUMBER: _ClassVar[int]
    PAGE_TOKEN_FIELD_NUMBER: _ClassVar[int]
    limit: int
    page_token: str
    def __init__(self, limit: _Optional[int] = ..., page_token: _Optional[str] = ...) -> None: ...

class PageResponse(_message.Message):
    __slots__ = ("next_page_token",)
    NEXT_PAGE_TOKEN_FIELD_NUMBER: _ClassVar[int]
    next_page_token: str
    def __init__(self, next_page_token: _Optional[str] = ...) -> None: ...

class TraceDocument(_message.Message):
    __slots__ = ("content", "format", "name")
    CONTENT_FIELD_NUMBER: _ClassVar[int]
    FORMAT_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    content: bytes
    format: TraceFormat
    name: str
    def __init__(self, content: _Optional[bytes] = ..., format: _Optional[_Union[TraceFormat, str]] = ..., name: _Optional[str] = ...) -> None: ...

class OtlpTelemetry(_message.Message):
    __slots__ = ("traces", "logs")
    TRACES_FIELD_NUMBER: _ClassVar[int]
    LOGS_FIELD_NUMBER: _ClassVar[int]
    traces: _trace_pb2.TracesData
    logs: _logs_pb2.LogsData
    def __init__(self, traces: _Optional[_Union[_trace_pb2.TracesData, _Mapping]] = ..., logs: _Optional[_Union[_logs_pb2.LogsData, _Mapping]] = ...) -> None: ...

class TraceInput(_message.Message):
    __slots__ = ("otlp", "documents")
    OTLP_FIELD_NUMBER: _ClassVar[int]
    DOCUMENTS_FIELD_NUMBER: _ClassVar[int]
    otlp: OtlpTelemetry
    documents: _containers.RepeatedCompositeFieldContainer[TraceDocument]
    def __init__(self, otlp: _Optional[_Union[OtlpTelemetry, _Mapping]] = ..., documents: _Optional[_Iterable[_Union[TraceDocument, _Mapping]]] = ...) -> None: ...
