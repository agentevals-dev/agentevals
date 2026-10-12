from agentevals.api.v1alpha1 import common_pb2 as _common_pb2
from agentevals.api.v1alpha1 import views_pb2 as _views_pb2
from google.protobuf.internal import containers as _containers
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class ListSessionsRequest(_message.Message):
    __slots__ = ("page",)
    PAGE_FIELD_NUMBER: _ClassVar[int]
    page: _common_pb2.PageRequest
    def __init__(self, page: _Optional[_Union[_common_pb2.PageRequest, _Mapping]] = ...) -> None: ...

class ListSessionsResponse(_message.Message):
    __slots__ = ("sessions", "page")
    SESSIONS_FIELD_NUMBER: _ClassVar[int]
    PAGE_FIELD_NUMBER: _ClassVar[int]
    sessions: _containers.RepeatedCompositeFieldContainer[_views_pb2.SessionSummary]
    page: _common_pb2.PageResponse
    def __init__(self, sessions: _Optional[_Iterable[_Union[_views_pb2.SessionSummary, _Mapping]]] = ..., page: _Optional[_Union[_common_pb2.PageResponse, _Mapping]] = ...) -> None: ...

class GetSessionRequest(_message.Message):
    __slots__ = ("id",)
    ID_FIELD_NUMBER: _ClassVar[int]
    id: str
    def __init__(self, id: _Optional[str] = ...) -> None: ...

class GetSessionResponse(_message.Message):
    __slots__ = ("session",)
    SESSION_FIELD_NUMBER: _ClassVar[int]
    session: _views_pb2.Session
    def __init__(self, session: _Optional[_Union[_views_pb2.Session, _Mapping]] = ...) -> None: ...

class WatchSessionsRequest(_message.Message):
    __slots__ = ("include_turns",)
    INCLUDE_TURNS_FIELD_NUMBER: _ClassVar[int]
    include_turns: bool
    def __init__(self, include_turns: _Optional[bool] = ...) -> None: ...

class SessionRemoved(_message.Message):
    __slots__ = ("id", "absorbed_by")
    ID_FIELD_NUMBER: _ClassVar[int]
    ABSORBED_BY_FIELD_NUMBER: _ClassVar[int]
    id: str
    absorbed_by: str
    def __init__(self, id: _Optional[str] = ..., absorbed_by: _Optional[str] = ...) -> None: ...

class Synced(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class WatchSessionsResponse(_message.Message):
    __slots__ = ("upsert", "removed", "synced")
    UPSERT_FIELD_NUMBER: _ClassVar[int]
    REMOVED_FIELD_NUMBER: _ClassVar[int]
    SYNCED_FIELD_NUMBER: _ClassVar[int]
    upsert: _views_pb2.Session
    removed: SessionRemoved
    synced: Synced
    def __init__(self, upsert: _Optional[_Union[_views_pb2.Session, _Mapping]] = ..., removed: _Optional[_Union[SessionRemoved, _Mapping]] = ..., synced: _Optional[_Union[Synced, _Mapping]] = ...) -> None: ...
