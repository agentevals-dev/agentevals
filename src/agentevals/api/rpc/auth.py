"""Optional Kubernetes ServiceAccount authentication for the gRPC API.

A caller sends ``authorization: Bearer <token>`` with a ServiceAccount token minted for the configured
audience. The server verifies the token with a TokenReview and accepts only the ServiceAccounts on an
explicit allowlist: any pod can mint a token for any audience, so the audience alone authorizes
nothing. Health checks stay open; reflection, when turned on, needs a token like every other
service. A failed check never falls back to unauthenticated access.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import re
import time
from collections import OrderedDict
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

import grpc

logger = logging.getLogger(__name__)

DEFAULT_AUDIENCE = "agentevals"
SERVICE_ACCOUNT_PREFIX = "system:serviceaccount:"
CACHE_TTL_SECONDS = 30.0
CACHE_MAX_ENTRIES = 1024
REVIEW_TIMEOUT_SECONDS = 10
OPEN_SERVICES = frozenset({"grpc.health.v1.Health"})

UNAUTHENTICATED_MESSAGE = "missing or invalid bearer token"
PERMISSION_DENIED_MESSAGE = "this service account may not call the API"
UNAVAILABLE_MESSAGE = "credential verification is unavailable"

_NAMESPACE = re.compile(r"[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?")
_NAME = re.compile(r"[a-z0-9]([-a-z0-9.]{0,251}[a-z0-9])?")


class Unauthenticated(Exception):
    """The token is missing, malformed, expired or rejected by the TokenReview."""


class ReviewUnavailable(Exception):
    """The TokenReview could not be completed."""


def parse_service_account(value: str) -> str:
    """Normalize ``namespace/name``, rejecting anything Kubernetes would not accept as one."""
    namespace, sep, name = value.strip().partition("/")
    if not sep or not _NAMESPACE.fullmatch(namespace) or not _NAME.fullmatch(name):
        raise ValueError(f"expected a ServiceAccount as namespace/name, got {value!r}")
    return f"{namespace}/{name}"


@dataclass(frozen=True, slots=True)
class KubernetesAuth:
    audience: str
    allowed_service_accounts: frozenset[str]
    # An ``AuthenticationV1Api`` or anything with the same ``create_token_review``.
    token_reviews: Any

    def __post_init__(self) -> None:
        if not self.audience:
            raise ValueError("Kubernetes authentication needs an audience")
        if not self.allowed_service_accounts:
            raise ValueError("Kubernetes authentication needs at least one allowed ServiceAccount")


def kubernetes_token_reviews() -> Any:
    """An ``AuthenticationV1Api`` from in-cluster config, or the local kubeconfig for development."""
    try:
        from kubernetes import client, config
    except ImportError as exc:
        raise RuntimeError(
            "Kubernetes authentication requires the 'kubernetes' extra; install agentevals-cli[kubernetes]"
        ) from exc

    try:
        config.load_incluster_config()
    except config.ConfigException:
        config.load_kube_config()
    return client.AuthenticationV1Api()


@dataclass(frozen=True, slots=True)
class _Verified:
    username: str
    valid_until: float


class TokenReviewVerifier:
    """Verifies ServiceAccount tokens for one audience.

    Successful reviews are cached by a SHA-256 of the audience and token for at most ``ttl`` seconds
    and never past the token's ``exp``. Failures are never cached. Concurrent lookups of one token
    share a single review.
    """

    def __init__(
        self,
        token_reviews: Any,
        audience: str,
        *,
        clock: Callable[[], float] = time.time,
        ttl: float = CACHE_TTL_SECONDS,
        max_entries: int = CACHE_MAX_ENTRIES,
    ) -> None:
        self._token_reviews = token_reviews
        self._audience = audience
        self._clock = clock
        self._ttl = ttl
        self._max_entries = max_entries
        self._verified: OrderedDict[str, _Verified] = OrderedDict()
        self._lookups: dict[str, asyncio.Future[_Verified]] = {}

    async def verify(self, token: str) -> str:
        """The ServiceAccount username of a valid token; raises :class:`Unauthenticated` or
        :class:`ReviewUnavailable`."""
        key = hashlib.sha256(f"{self._audience}\0{token}".encode()).hexdigest()
        entry = self._cached(key)
        if entry is None:
            lookup = self._lookups.get(key)
            if lookup is None or lookup.done():
                lookup = asyncio.ensure_future(self._review(key, token))
                self._lookups[key] = lookup
                lookup.add_done_callback(lambda done: self._lookup_done(key, done))
            # A caller that goes away cancels its own wait, not the review others share.
            entry = await asyncio.shield(lookup)
        return entry.username

    def _cached(self, key: str) -> _Verified | None:
        entry = self._verified.get(key)
        if entry is None:
            return None
        if self._clock() < entry.valid_until:
            self._verified.move_to_end(key)
            return entry
        del self._verified[key]
        return None

    def _lookup_done(self, key: str, lookup: asyncio.Future[_Verified]) -> None:
        if self._lookups.get(key) is lookup:
            del self._lookups[key]
        if not lookup.cancelled():
            # Marks the failure as retrieved when every waiter has gone away.
            lookup.exception()

    async def _review(self, key: str, token: str) -> _Verified:
        expires_at = _expiration(_unverified_claims(token), self._clock())
        # The TTL starts before the request so API server latency cannot extend it.
        valid_until = self._clock() + self._ttl
        if expires_at is not None:
            valid_until = min(valid_until, expires_at)

        body = {
            "apiVersion": "authentication.k8s.io/v1",
            "kind": "TokenReview",
            "spec": {"token": token, "audiences": [self._audience]},
        }
        try:
            review = await asyncio.to_thread(
                self._token_reviews.create_token_review, body, _request_timeout=REVIEW_TIMEOUT_SECONDS
            )
        except Exception as exc:
            # A TokenReview response echoes the token, so neither the error nor its body is logged.
            logger.warning("TokenReview failed: %s %s", type(exc).__name__, getattr(exc, "status", "") or "")
            raise ReviewUnavailable from None

        status = getattr(review, "status", None)
        user = getattr(status, "user", None)
        username = getattr(user, "username", None) or ""
        if (
            status is None
            or status.authenticated is not True
            or status.error
            or not getattr(user, "uid", None)
            or not username.startswith(SERVICE_ACCOUNT_PREFIX)
            or self._audience not in (status.audiences or [])
            or not self._clock() < valid_until
        ):
            raise Unauthenticated

        entry = _Verified(username=username, valid_until=valid_until)
        self._verified[key] = entry
        self._verified.move_to_end(key)
        while len(self._verified) > self._max_entries:
            self._verified.popitem(last=False)
        return entry


class ServiceAccountInterceptor(grpc.aio.ServerInterceptor):
    """Rejects every call outside :data:`OPEN_SERVICES` unless it carries the token of an allowed
    ServiceAccount: ``UNAUTHENTICATED`` for a missing or invalid token, ``PERMISSION_DENIED`` for a
    ServiceAccount outside the allowlist, ``UNAVAILABLE`` when the TokenReview fails."""

    def __init__(self, verifier: TokenReviewVerifier, allowed_service_accounts: Iterable[str]) -> None:
        self._verifier = verifier
        self._allowed = frozenset(allowed_service_accounts)

    async def intercept_service(self, continuation, handler_call_details):
        handler = await continuation(handler_call_details)
        method = handler_call_details.method
        if handler is None or method.lstrip("/").partition("/")[0] in OPEN_SERVICES:
            return handler
        denial = await self._check(method, handler_call_details.invocation_metadata or ())
        return handler if denial is None else _denying(handler, *denial)

    async def _check(self, method: str, metadata: Iterable[tuple[str, Any]]) -> tuple[grpc.StatusCode, str] | None:
        token = _bearer_token([value for key, value in metadata if key == "authorization"])
        try:
            if token is None:
                raise Unauthenticated
            username = await self._verifier.verify(token)
        except Unauthenticated:
            logger.info("RPC %s rejected: missing or invalid bearer token", method)
            return grpc.StatusCode.UNAUTHENTICATED, UNAUTHENTICATED_MESSAGE
        except ReviewUnavailable:
            return grpc.StatusCode.UNAVAILABLE, UNAVAILABLE_MESSAGE
        except Exception as exc:
            logger.error("RPC %s rejected: credential check failed with %s", method, type(exc).__name__)
            return grpc.StatusCode.INTERNAL, "internal server error"

        account = username.removeprefix(SERVICE_ACCOUNT_PREFIX).replace(":", "/", 1)
        if account not in self._allowed:
            logger.info("RPC %s rejected: ServiceAccount %s is not allowed", method, account)
            return grpc.StatusCode.PERMISSION_DENIED, PERMISSION_DENIED_MESSAGE
        return None


def build_interceptor(auth: KubernetesAuth) -> ServiceAccountInterceptor:
    return ServiceAccountInterceptor(
        TokenReviewVerifier(auth.token_reviews, auth.audience), auth.allowed_service_accounts
    )


def _bearer_token(values: list[Any]) -> str | None:
    if len(values) != 1 or not isinstance(values[0], str):
        return None
    scheme, _, token = values[0].partition(" ")
    if scheme.lower() != "bearer" or not token:
        return None
    return token


def _unverified_claims(token: str) -> dict[str, Any]:
    """The JWT payload, read without checking the signature; the TokenReview verifies the token."""
    parts = token.split(".")
    if len(parts) != 3:
        raise Unauthenticated
    payload = parts[1]
    try:
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (ValueError, RecursionError):
        raise Unauthenticated from None
    if not isinstance(claims, dict):
        raise Unauthenticated
    return claims


def _expiration(claims: dict[str, Any], now: float) -> float | None:
    """``exp`` as a timestamp, ``None`` when the token has none; raises when it is invalid or past."""
    if "exp" not in claims:
        return None
    exp = claims["exp"]
    if isinstance(exp, bool) or not isinstance(exp, int | float) or not exp > now:
        raise Unauthenticated
    return float(exp)


def _denying(handler: grpc.RpcMethodHandler, code: grpc.StatusCode, details: str) -> grpc.RpcMethodHandler:
    """A handler of the same kind as ``handler`` that only aborts."""

    async def deny(request, context):
        await context.abort(code, details)

    if handler.unary_unary:
        factory = grpc.unary_unary_rpc_method_handler
    elif handler.unary_stream:
        factory = grpc.unary_stream_rpc_method_handler
    elif handler.stream_unary:
        factory = grpc.stream_unary_rpc_method_handler
    else:
        factory = grpc.stream_stream_rpc_method_handler
    return factory(
        deny,
        request_deserializer=handler.request_deserializer,
        response_serializer=handler.response_serializer,
    )
