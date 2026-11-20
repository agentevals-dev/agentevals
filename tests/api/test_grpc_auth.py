"""Kubernetes ServiceAccount authentication on the gRPC API, against a fake TokenReview client."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import threading
from types import SimpleNamespace

import grpc
import pytest
from grpc_health.v1 import health_pb2, health_pb2_grpc
from grpc_reflection.v1alpha import reflection_pb2, reflection_pb2_grpc

from agentevals.api.rpc.auth import (
    KubernetesAuth,
    ReviewUnavailable,
    TokenReviewVerifier,
    Unauthenticated,
    parse_service_account,
)
from agentevals.api.rpc.server import start_api_server, stop_api_server
from agentevals.api.rpc.services import ApiContext, Ports
from agentevals.api.v1alpha1 import sessions_pb2, sessions_pb2_grpc, system_pb2, system_pb2_grpc
from agentevals.streaming.manager import LiveManager

AUDIENCE = "agentevals"
CALLER = "system:serviceaccount:team:caller"
NOW = 1_800_000_000.0


def _segment(data: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()


def _token(**claims) -> str:
    claims.setdefault("exp", NOW + 3600)
    claims.setdefault("sub", CALLER)
    return f"{_segment({'alg': 'RS256'})}.{_segment(claims)}.signature"


def _status(
    *,
    authenticated=True,
    error="",
    username=CALLER,
    uid="0b9c1f1e",
    audiences=(AUDIENCE,),
) -> SimpleNamespace:
    return SimpleNamespace(
        authenticated=authenticated,
        error=error,
        user=SimpleNamespace(username=username, uid=uid),
        audiences=list(audiences),
    )


class FakeTokenReviews:
    """Stands in for ``kubernetes.client.AuthenticationV1Api``."""

    def __init__(self, status: SimpleNamespace | None = None) -> None:
        self.status = status or _status()
        self.error: Exception | None = None
        self.calls: list[dict] = []
        self.gate: threading.Event | None = None

    def create_token_review(self, body, _request_timeout=None):
        self.calls.append(body)
        if self.gate is not None:
            self.gate.wait(5)
        if self.error is not None:
            raise self.error
        return SimpleNamespace(spec=body["spec"], status=self.status)


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def reviews():
    return FakeTokenReviews()


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def verifier(reviews, clock):
    return TokenReviewVerifier(reviews, AUDIENCE, clock=clock)


@pytest.fixture
async def channel(reviews, monkeypatch):
    monkeypatch.delenv("AGENTEVALS_API_TRACES", raising=False)
    monkeypatch.setenv("AGENTEVALS_GRPC_REFLECTION", "true")
    context = ApiContext(
        manager=LiveManager(), run_service=None, ports=Ports(http=8001, otlp_http=4318, otlp_grpc=4317, api_grpc=8002)
    )
    auth = KubernetesAuth(audience=AUDIENCE, allowed_service_accounts=frozenset({"team/caller"}), token_reviews=reviews)
    api = await start_api_server(context, "127.0.0.1", 0, auth)
    try:
        async with grpc.aio.insecure_channel(f"127.0.0.1:{api.port}") as channel:
            yield channel
    finally:
        await stop_api_server(api)


async def _get_version(channel, metadata=None) -> grpc.StatusCode:
    try:
        await system_pb2_grpc.SystemServiceStub(channel).GetVersion(system_pb2.GetVersionRequest(), metadata=metadata)
    except grpc.aio.AioRpcError as exc:
        return exc.code()
    return grpc.StatusCode.OK


def _bearer(token: str) -> tuple[tuple[str, str], ...]:
    return (("authorization", f"Bearer {token}"),)


class TestServer:
    async def test_an_allowed_service_account_is_accepted(self, channel, reviews):
        assert await _get_version(channel, _bearer(_token(exp=4_000_000_000))) == grpc.StatusCode.OK
        [body] = reviews.calls
        assert body["spec"]["audiences"] == [AUDIENCE]

    async def test_health_needs_no_token(self, channel, reviews):
        response = await health_pb2_grpc.HealthStub(channel).Check(health_pb2.HealthCheckRequest())
        assert response.status == health_pb2.HealthCheckResponse.SERVING
        assert reviews.calls == []

    async def test_reflection_needs_a_token(self, channel):
        stub = reflection_pb2_grpc.ServerReflectionStub(channel)
        request = reflection_pb2.ServerReflectionRequest(list_services="")
        with pytest.raises(grpc.aio.AioRpcError) as raised:
            [message async for message in stub.ServerReflectionInfo(iter([request]))]
        assert raised.value.code() == grpc.StatusCode.UNAUTHENTICATED

        call = stub.ServerReflectionInfo(iter([request]), metadata=_bearer(_token(exp=4_000_000_000)))
        [listing] = [message async for message in call]
        assert listing.list_services_response.service

    @pytest.mark.parametrize(
        "metadata",
        [
            None,
            (("authorization", "Bearer "),),
            (("authorization", "Basic dXNlcjpwYXNz"),),
            (("authorization", "not-a-jwt"),),
            (("authorization", "Bearer not-a-jwt"),),
            (("authorization", "Bearer a.b.c"),),
            (("authorization", f"Bearer {_token(exp=4_000_000_000)}"),) * 2,
        ],
        ids=["missing", "empty", "basic", "no-scheme", "not-a-jwt", "bad-payload", "two-headers"],
    )
    async def test_a_missing_or_malformed_token_is_unauthenticated(self, channel, reviews, metadata):
        assert await _get_version(channel, metadata) == grpc.StatusCode.UNAUTHENTICATED
        assert reviews.calls == []

    @pytest.mark.parametrize(
        "status",
        [
            _status(authenticated=False),
            _status(error="token has been invalidated"),
            _status(username="alice"),
            _status(username="system:node:worker-1"),
            _status(uid=""),
            _status(audiences=()),
            _status(audiences=("https://kubernetes.default.svc",)),
        ],
        ids=["not-authenticated", "error", "user", "node", "no-uid", "no-audience", "other-audience"],
    )
    async def test_a_rejected_review_is_unauthenticated(self, channel, reviews, status):
        reviews.status = status
        assert await _get_version(channel, _bearer(_token(exp=4_000_000_000))) == grpc.StatusCode.UNAUTHENTICATED

    async def test_a_service_account_outside_the_allowlist_is_denied(self, channel, reviews):
        reviews.status = _status(username="system:serviceaccount:team:other")
        assert await _get_version(channel, _bearer(_token(exp=4_000_000_000))) == grpc.StatusCode.PERMISSION_DENIED

    async def test_an_unreachable_api_server_is_unavailable_and_never_falls_back(self, channel, reviews):
        reviews.error = ConnectionRefusedError()
        token = _token(exp=4_000_000_000)
        assert await _get_version(channel, _bearer(token)) == grpc.StatusCode.UNAVAILABLE
        assert await _get_version(channel, _bearer(token)) == grpc.StatusCode.UNAVAILABLE
        assert await _get_version(channel) == grpc.StatusCode.UNAUTHENTICATED
        assert len(reviews.calls) == 2

        reviews.error = None
        assert await _get_version(channel, _bearer(token)) == grpc.StatusCode.OK

    async def test_streaming_calls_are_checked(self, channel):
        stub = sessions_pb2_grpc.SessionServiceStub(channel)
        with pytest.raises(grpc.aio.AioRpcError) as raised:
            async for _ in stub.WatchSessions(sessions_pb2.WatchSessionsRequest()):
                pass
        assert raised.value.code() == grpc.StatusCode.UNAUTHENTICATED

        call = stub.WatchSessions(sessions_pb2.WatchSessionsRequest(), metadata=_bearer(_token(exp=4_000_000_000)))
        first = await call.read()
        assert first.HasField("synced")
        call.cancel()

    async def test_the_token_never_reaches_the_log(self, channel, reviews, caplog):
        token = _token(exp=4_000_000_000)
        reviews.error = RuntimeError(f"response body echoing {token}")
        with caplog.at_level(logging.DEBUG, logger="agentevals"):
            assert await _get_version(channel, _bearer(token)) == grpc.StatusCode.UNAVAILABLE
            reviews.error = None
            reviews.status = _status(username="system:serviceaccount:team:other")
            assert await _get_version(channel, _bearer(token)) == grpc.StatusCode.PERMISSION_DENIED
        assert "TokenReview failed" in caplog.text
        assert token.split(".")[1] not in caplog.text


class TestVerifier:
    async def test_a_success_is_cached_for_the_ttl(self, verifier, reviews, clock):
        token = _token()
        assert await verifier.verify(token) == CALLER
        clock.now += 29
        assert await verifier.verify(token) == CALLER
        assert len(reviews.calls) == 1

        clock.now += 1
        assert await verifier.verify(token) == CALLER
        assert len(reviews.calls) == 2

    async def test_the_cache_never_outlives_the_token(self, verifier, reviews, clock):
        token = _token(exp=NOW + 10)
        await verifier.verify(token)
        clock.now += 9
        await verifier.verify(token)
        assert len(reviews.calls) == 1

        clock.now += 1
        with pytest.raises(Unauthenticated):
            await verifier.verify(token)
        assert len(reviews.calls) == 1

    @pytest.mark.parametrize("exp", [NOW, NOW - 1, "4000000000", True, None, float("nan")])
    async def test_an_expired_or_invalid_exp_is_rejected_without_a_review(self, verifier, reviews, exp):
        with pytest.raises(Unauthenticated):
            await verifier.verify(_token(exp=exp))
        assert reviews.calls == []

    async def test_a_token_without_exp_is_bounded_by_the_ttl(self, verifier, reviews, clock):
        claims = {"sub": CALLER}
        token = f"{_segment({'alg': 'RS256'})}.{_segment(claims)}.signature"
        await verifier.verify(token)
        clock.now += 30
        await verifier.verify(token)
        assert len(reviews.calls) == 2

    async def test_a_deeply_nested_payload_is_rejected(self, verifier, reviews):
        payload = base64.urlsafe_b64encode(b"[" * 200_000).rstrip(b"=").decode()
        with pytest.raises(Unauthenticated):
            await verifier.verify(f"e30.{payload}.signature")
        assert reviews.calls == []

    async def test_failures_are_never_cached(self, verifier, reviews):
        token = _token()
        reviews.status = _status(authenticated=False)
        with pytest.raises(Unauthenticated):
            await verifier.verify(token)
        reviews.error = TimeoutError()
        with pytest.raises(ReviewUnavailable):
            await verifier.verify(token)

        reviews.error = None
        reviews.status = _status()
        assert await verifier.verify(token) == CALLER
        assert len(reviews.calls) == 3

    async def test_concurrent_lookups_share_one_review(self, verifier, reviews):
        reviews.gate = threading.Event()
        token = _token()
        waiters = [asyncio.ensure_future(verifier.verify(token)) for _ in range(8)]
        await asyncio.sleep(0.05)
        reviews.gate.set()
        assert await asyncio.gather(*waiters) == [CALLER] * 8
        assert len(reviews.calls) == 1

    async def test_a_shared_failure_reaches_every_waiter_and_is_not_kept(self, verifier, reviews):
        reviews.gate = threading.Event()
        reviews.error = ConnectionResetError()
        token = _token()
        waiters = [asyncio.ensure_future(verifier.verify(token)) for _ in range(3)]
        await asyncio.sleep(0.05)
        reviews.gate.set()
        results = await asyncio.gather(*waiters, return_exceptions=True)
        assert all(isinstance(result, ReviewUnavailable) for result in results)

        reviews.gate = None
        reviews.error = None
        assert await verifier.verify(token) == CALLER
        assert len(reviews.calls) == 2

    async def test_a_cancelled_waiter_does_not_cancel_the_shared_review(self, verifier, reviews):
        reviews.gate = threading.Event()
        token = _token()
        leaving = asyncio.ensure_future(verifier.verify(token))
        staying = asyncio.ensure_future(verifier.verify(token))
        await asyncio.sleep(0.05)
        leaving.cancel()
        reviews.gate.set()
        assert await staying == CALLER
        assert len(reviews.calls) == 1

    async def test_the_cache_is_bounded(self, reviews, clock):
        verifier = TokenReviewVerifier(reviews, AUDIENCE, clock=clock, max_entries=2)
        first, second, third = (_token(jti=str(n)) for n in range(3))
        for token in (first, second, third):
            await verifier.verify(token)
        await verifier.verify(third)
        await verifier.verify(second)
        assert len(reviews.calls) == 3

        await verifier.verify(first)
        assert len(reviews.calls) == 4


class TestSettings:
    def test_an_empty_allowlist_refuses_to_start(self, reviews):
        with pytest.raises(ValueError, match="at least one allowed ServiceAccount"):
            KubernetesAuth(audience=AUDIENCE, allowed_service_accounts=frozenset(), token_reviews=reviews)

    @pytest.mark.parametrize("value", ["caller", "team/", "/caller", "Team/caller", "team/caller/x", "team:caller"])
    def test_a_malformed_service_account_is_rejected(self, value):
        with pytest.raises(ValueError):
            parse_service_account(value)

    def test_a_service_account_is_normalized(self):
        assert parse_service_account(" team/caller.v2 ") == "team/caller.v2"
