"""agentevals' own API server spans never become live sessions, even when exported back to it."""

from __future__ import annotations

from agentevals.otel.identity import GRPC_INSTRUMENTATION_SCOPE
from agentevals.otel.store import DROPPED_OWN_SPANS

from .builders import ingest, make_store, named, request, span

SERVER = 2
CLIENT = 3


def _rpc_span(name: str, kind: int) -> dict:
    data = span(name=name, attrs={"rpc.system.name": "grpc"})
    data["kind"] = kind
    return data


def _ingest(name: str, kind: int = SERVER, scope: str = GRPC_INSTRUMENTATION_SCOPE):
    store, _ = make_store()
    result = ingest(store, request([_rpc_span(name, kind)], named("own"), scope=scope))
    return store, result


def test_api_server_spans_are_dropped():
    store, result = _ingest("/agentevals.api.v1alpha1.SessionService/WatchSessions")
    assert (result.accepted, result.rejected) == (1, 0)
    assert result.dropped[DROPPED_OWN_SPANS] == 1
    assert store.counters[DROPPED_OWN_SPANS] == 1
    assert store.sessions == {}


def test_other_grpc_server_spans_are_kept():
    store, result = _ingest("/acme.orders.v1.OrderService/Create")
    assert result.dropped[DROPPED_OWN_SPANS] == 0
    assert list(store.sessions) == ["own"]


def test_client_spans_of_the_api_are_kept():
    store, _ = _ingest("/agentevals.api.v1alpha1.SessionService/ListSessions", kind=CLIENT)
    assert list(store.sessions) == ["own"]


def test_the_scope_must_be_the_grpc_instrumentation():
    store, _ = _ingest("/agentevals.api.v1alpha1.SessionService/ListSessions", scope="acme.grpc")
    assert list(store.sessions) == ["own"]
