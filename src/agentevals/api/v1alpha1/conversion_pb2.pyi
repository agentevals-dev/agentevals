from agentevals.api.v1alpha1 import common_pb2 as _common_pb2
from agentevals.api.v1alpha1 import views_pb2 as _views_pb2
from google.protobuf import struct_pb2 as _struct_pb2
from google.protobuf.internal import containers as _containers
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class ConvertTracesRequest(_message.Message):
    __slots__ = ("traces", "group_by")
    TRACES_FIELD_NUMBER: _ClassVar[int]
    GROUP_BY_FIELD_NUMBER: _ClassVar[int]
    traces: _common_pb2.TraceInput
    group_by: _common_pb2.GroupBy
    def __init__(self, traces: _Optional[_Union[_common_pb2.TraceInput, _Mapping]] = ..., group_by: _Optional[_Union[_common_pb2.GroupBy, str]] = ...) -> None: ...

class ConvertedConversation(_message.Message):
    __slots__ = ("conversation", "golden_adk_eval_set")
    CONVERSATION_FIELD_NUMBER: _ClassVar[int]
    GOLDEN_ADK_EVAL_SET_FIELD_NUMBER: _ClassVar[int]
    conversation: _views_pb2.ConversationView
    golden_adk_eval_set: _struct_pb2.Struct
    def __init__(self, conversation: _Optional[_Union[_views_pb2.ConversationView, _Mapping]] = ..., golden_adk_eval_set: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ...) -> None: ...

class ConvertTracesResponse(_message.Message):
    __slots__ = ("conversations", "warnings")
    CONVERSATIONS_FIELD_NUMBER: _ClassVar[int]
    WARNINGS_FIELD_NUMBER: _ClassVar[int]
    conversations: _containers.RepeatedCompositeFieldContainer[ConvertedConversation]
    warnings: _containers.RepeatedScalarFieldContainer[str]
    def __init__(self, conversations: _Optional[_Iterable[_Union[ConvertedConversation, _Mapping]]] = ..., warnings: _Optional[_Iterable[str]] = ...) -> None: ...
