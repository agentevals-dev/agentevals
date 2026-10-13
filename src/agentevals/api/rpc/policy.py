"""Method policy and error mapping for the API's gRPC services.

Every servicer method carries :func:`rpc`. The server refuses to start while a method of an
agentevals service has no policy, so a new RPC cannot ship without one. The wrapper checks the
method's requirement, then maps errors: an :class:`ApiError` to its status with a
``google.rpc.ErrorInfo``, a validation error to INVALID_ARGUMENT without the input values, and
anything else to INTERNAL with a fixed message, the details only in the server log.
"""

from __future__ import annotations

import functools
import inspect
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import grpc
from google.protobuf import any_pb2
from google.rpc import code_pb2, error_details_pb2, status_pb2
from grpc_status import rpc_status
from pydantic import ValidationError

logger = logging.getLogger(__name__)

ERROR_DOMAIN = "agentevals.api.v1alpha1"
REASON_STORAGE_NOT_CONFIGURED = "STORAGE_NOT_CONFIGURED"
REASON_RUN_SPEC_CONFLICT = "RUN_SPEC_CONFLICT"
REASON_INVALID_REQUEST = "INVALID_REQUEST"

_POLICY_ATTR = "__agentevals_rpc_policy__"


class ApiError(Exception):
    """An error the caller may see: a status code, a message and a stable reason."""

    def __init__(
        self,
        code: grpc.StatusCode,
        message: str,
        reason: str | None = None,
        metadata: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.reason = reason
        self.metadata = metadata or {}

    @classmethod
    def invalid(cls, message: str) -> ApiError:
        return cls(grpc.StatusCode.INVALID_ARGUMENT, message, REASON_INVALID_REQUEST)

    @classmethod
    def not_found(cls, message: str) -> ApiError:
        return cls(grpc.StatusCode.NOT_FOUND, message)


@dataclass(frozen=True, slots=True)
class Policy:
    # The method needs run storage; servicers expose it as ``run_service``.
    storage: bool = False


def rpc(*, storage: bool = False) -> Callable:
    """Declare a servicer method's policy and map its errors to gRPC statuses."""
    policy = Policy(storage=storage)

    def decorate(method: Callable) -> Callable:
        name = method.__name__
        if inspect.isasyncgenfunction(method):

            @functools.wraps(method)
            async def stream(self, request, context):
                try:
                    _require(self, policy)
                    async for item in method(self, request, context):
                        yield item
                except Exception as exc:
                    await _abort(context, name, exc)

            wrapper = stream
        else:

            @functools.wraps(method)
            async def unary(self, request, context):
                try:
                    _require(self, policy)
                    return await method(self, request, context)
                except Exception as exc:
                    await _abort(context, name, exc)

            wrapper = unary
        setattr(wrapper, _POLICY_ATTR, policy)
        return wrapper

    return decorate


def check_policies(servicer: Any, service: Any) -> None:
    """Fail unless every method of ``service`` (a ServiceDescriptor) is decorated with :func:`rpc`."""
    missing = [m.name for m in service.methods if not hasattr(getattr(type(servicer), m.name, None), _POLICY_ATTR)]
    if missing:
        raise RuntimeError(f"{service.full_name} methods without an rpc policy: {', '.join(missing)}")


def _require(servicer: Any, policy: Policy) -> None:
    if policy.storage and getattr(servicer, "run_service", None) is None:
        raise ApiError(
            grpc.StatusCode.FAILED_PRECONDITION,
            "run storage is not configured (set AGENTEVALS_STORAGE_BACKEND=postgres)",
            REASON_STORAGE_NOT_CONFIGURED,
        )


async def _abort(context: Any, method: str, exc: Exception) -> None:
    if isinstance(exc, grpc.aio.AbortError):
        raise exc
    if isinstance(exc, ApiError):
        status = _status(exc.code, exc.message, exc.reason, exc.metadata)
    elif isinstance(exc, ValidationError):
        status = _status(grpc.StatusCode.INVALID_ARGUMENT, validation_message(exc), REASON_INVALID_REQUEST)
    else:
        logger.exception("RPC %s failed", method)
        status = _status(grpc.StatusCode.INTERNAL, "internal server error")
    if status.code != grpc.StatusCode.INTERNAL:
        logger.info("RPC %s returned %s", method, status.code.name)
    await context.abort(status.code, status.details, status.trailing_metadata)


def validation_message(exc: ValidationError) -> str:
    """Field paths and messages, never the input values, which can carry secrets."""
    problems = [
        f"{'.'.join(str(part) for part in error['loc']) or 'request'}: {error['msg']}"
        for error in exc.errors(include_input=False, include_url=False, include_context=False)
    ]
    return "invalid request: " + "; ".join(problems)


def _status(
    code: grpc.StatusCode,
    message: str,
    reason: str | None = None,
    metadata: dict[str, str] | None = None,
) -> grpc.Status:
    proto = status_pb2.Status(code=getattr(code_pb2, code.name), message=message)
    if reason:
        detail = any_pb2.Any()
        detail.Pack(error_details_pb2.ErrorInfo(reason=reason, domain=ERROR_DOMAIN, metadata=metadata or {}))
        proto.details.append(detail)
    return rpc_status.to_status(proto)
