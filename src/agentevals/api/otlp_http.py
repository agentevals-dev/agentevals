"""OTLP/HTTP protocol handling: content negotiation, compression, error bodies.

Kept separate from `otlp_routes.py` so the routing layer stays thin and this
logic can be unit-tested without an HTTP server (the convention used by
`otlp_processing.py`). Nothing here is gRPC-specific; `otlp_grpc.py` reuses the
response builders from `otlp_processing.py` instead.
"""

from __future__ import annotations

import asyncio
import json
import logging
import zlib
from collections.abc import Callable, Mapping

from fastapi import FastAPI, Request, Response
from google.protobuf.json_format import MessageToJson
from google.protobuf.message import DecodeError, Message
from google.rpc import code_pb2
from google.rpc.status_pb2 import Status
from starlette.exceptions import HTTPException as StarletteHTTPException

from ..otel.decode import DecodeResult

logger = logging.getLogger(__name__)

PROTOBUF_MEDIA_TYPE = "application/x-protobuf"
JSON_MEDIA_TYPE = "application/json"

# OTLP recommends 64 MiB as the default limit for a decompressed request body.
MAX_DECOMPRESSED_BYTES = 64 * 1024 * 1024

# The raw body is capped too, for both the compressed and identity paths: without
# this a request can grow the process until the OOM killer takes down the whole
# CLI (the API, UI and gRPC servers share this process).
MAX_REQUEST_BYTES = 64 * 1024 * 1024

# Compressed input gets a tighter cap than the limit it may expand to.
MAX_COMPRESSED_BYTES = 8 * 1024 * 1024

# gzip decompression cost scales with the number of concatenated members, not with
# what they produce: every member costs a header parse and a CRC check even if it
# yields nothing, and an empty member is only 20 bytes. Counting members is what
# actually bounds the work -- without this, a body of empty members inside
# MAX_COMPRESSED_BYTES would burn seconds of CPU and still answer 200.
# Real exporters emit a single member; this is generous headroom.
MAX_GZIP_MEMBERS = 32

# zlib window bits for a gzip stream: 16 (gzip framing) + 15 (max window size).
_GZIP_WBITS = 31

# Parsing a decompressed body costs about 40ms per MiB (measured: ~1.2s for a
# 32 MiB protobuf export), all of it on the event loop unless it is threaded.
# Below this size the thread hop costs more than the work it moves.
PARSE_THREAD_THRESHOLD = 1024 * 1024

# Sent with 503 when the live store refused every item for capacity.
OVERLOAD_RETRY_AFTER_SECONDS = 5

_SUPPORTED_CONTENT_ENCODINGS = frozenset({"", "identity", "gzip"})

# OTLP asks servers to populate Status.code; these are the mappings from the
# reference Go receiver's statusutil helper. Status.code carries no meaning to
# OTLP itself ("this specification does not use Status.code"), but clients that
# branch on it see something sensible instead of OK.
_GRPC_STATUS_CODES = {
    400: code_pb2.INVALID_ARGUMENT,
    404: code_pb2.NOT_FOUND,
    405: code_pb2.UNIMPLEMENTED,
    413: code_pb2.RESOURCE_EXHAUSTED,
    415: code_pb2.INVALID_ARGUMENT,
    429: code_pb2.RESOURCE_EXHAUSTED,
    500: code_pb2.INTERNAL,
    503: code_pb2.UNAVAILABLE,
}
_GRPC_STATUS_CODE_DEFAULT = code_pb2.UNKNOWN


class OtlpError(Exception):
    """An OTLP/HTTP failure that must be returned as a `google.rpc.Status` body."""

    def __init__(self, status_code: int, message: str, headers: Mapping[str, str] | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.headers = dict(headers or {})


def overload_error(signal: str) -> OtlpError:
    return OtlpError(
        503,
        f"Live store is at capacity; {signal} not accepted, retry later",
        {"Retry-After": str(OVERLOAD_RETRY_AFTER_SECONDS)},
    )


def media_type_of(content_type: str) -> str:
    """Strip parameters from a media type: ``'application/json; charset=utf-8'`` → ``'application/json'``."""
    return content_type.split(";", 1)[0].strip().lower()


def resolve_media_type(request: Request) -> str:
    """Return the media type to answer the request in.

    A missing ``Content-Type`` is treated as JSON for backward compatibility
    with clients that post JSON without setting the header. A present but
    unrecognized one is a 415: guessing would misroute the body.
    """
    raw = request.headers.get("content-type", "")
    media_type = media_type_of(raw)
    if media_type in (PROTOBUF_MEDIA_TYPE, JSON_MEDIA_TYPE):
        return media_type
    if not raw:
        logger.debug("OTLP request without Content-Type; assuming JSON")
        return JSON_MEDIA_TYPE
    raise OtlpError(415, f"Unsupported Content-Type {raw!r}; expected {JSON_MEDIA_TYPE} or {PROTOBUF_MEDIA_TYPE}")


def _decode_content_encoding(request: Request, raw: bytes) -> bytes:
    """Decompress the request body according to ``Content-Encoding``.

    Both the compressed input and the decompressed output are bounded, so a
    payload cannot expand without limit in either direction.
    """
    encoding = request.headers.get("content-encoding", "").strip().lower()
    if encoding in ("", "identity"):
        return raw
    if encoding not in _SUPPORTED_CONTENT_ENCODINGS:
        # 415 rather than 400: collectors offer deflate/zstd/snappy too, so anyone
        # who picks one gets a permanent drop either way -- 415 is what RFC 9110
        # §15.5.16 defines for content in an unsupported format, and it reads more
        # clearly in an exporter's logs. Name the fix in the message.
        raise OtlpError(415, f"Unsupported Content-Encoding {encoding!r}; set compression: gzip or none")
    if len(raw) > MAX_COMPRESSED_BYTES:
        raise OtlpError(413, f"Compressed request body exceeds {MAX_COMPRESSED_BYTES} bytes")

    return _gunzip_bounded(raw)


def _gunzip_bounded(raw: bytes) -> bytes:
    """Decompress a gzip body, capping both the member count and the output size.

    Drives the member loop by hand rather than using ``gzip.GzipFile``, which walks
    concatenated members internally with no way to count them. Every failure mode
    here is a permanent client fault, so all of them surface as 400 rather than a
    retryable 500.
    """
    out = bytearray()
    remaining = raw
    members = 0

    while remaining:
        members += 1
        if members > MAX_GZIP_MEMBERS:
            # Also reached by trailing data after MAX_GZIP_MEMBERS valid members, so
            # the message names both possibilities rather than blaming the count.
            raise OtlpError(
                400,
                f"gzip request body exceeds {MAX_GZIP_MEMBERS} concatenated members or has trailing data",
            )

        decompressor = zlib.decompressobj(_GZIP_WBITS)
        try:
            # Ask for one byte past the cap so an over-cap body is distinguishable
            # from a corrupt one; that distinction is what keeps 413 and 400 apart.
            out += decompressor.decompress(remaining, MAX_DECOMPRESSED_BYTES + 1 - len(out))
        except zlib.error as exc:
            raise OtlpError(400, "Unable to decompress gzip request body") from exc

        if len(out) > MAX_DECOMPRESSED_BYTES:
            raise OtlpError(413, f"Request body exceeds {MAX_DECOMPRESSED_BYTES} bytes after decompression")

        if not decompressor.eof:
            # The stream ended before its end-of-stream marker, i.e. a truncated
            # upload. Raw zlib raises no EOFError here (that came from GzipFile), so
            # this structural check is the only thing that catches it.
            #
            # Saturated output leaves unconsumed input behind, but that case is
            # already caught by the cap check above, so this is belt-and-braces.
            if decompressor.unconsumed_tail:
                raise OtlpError(413, f"Request body exceeds {MAX_DECOMPRESSED_BYTES} bytes after decompression")
            raise OtlpError(400, "Unable to decompress gzip request body")

        # Skip NUL padding between members, which the gzip FAQ permits and
        # `gzip.GzipFile` tolerates. A gzip member never begins with a NUL, so
        # stripping here cannot swallow a subsequent member.
        remaining = decompressor.unused_data.lstrip(b"\x00")

    return bytes(out)


async def read_export_request(
    request: Request,
    decode_json: Callable[[dict], DecodeResult],
    decode_protobuf: Callable[[bytes], DecodeResult],
    container_key: str,
) -> tuple[DecodeResult, str]:
    """Read, decompress and decode an OTLP/HTTP export request into envelopes.

    Returns the decoded items plus the media type the response must use. Every
    decoding failure is permanent, so it surfaces as a 400; invalid items inside
    a well formed body are counted on the ``DecodeResult`` instead.

    ``container_key`` is the top-level request list (``resourceSpans`` or
    ``resourceLogs``); its type is checked so a structurally invalid JSON body
    fails as a 400 rather than crashing the pipeline as a 500.
    """
    media_type = resolve_media_type(request)
    body_bytes = await _read_bounded_body(request)

    # Decompression runs in a worker thread unconditionally: its cost is not
    # predictable from the input size, because a small body can expand enormously.
    # This process serves the dashboard API, the UI streams and the gRPC receiver
    # on one event loop, so blocking here would stall all of them.
    raw = await asyncio.to_thread(_decode_content_encoding, request, body_bytes)

    if not raw:
        return DecodeResult(), media_type

    # Parsing is proportional to the decompressed size, which is now known, so the
    # thread hop is only worth paying above the threshold.
    if len(raw) < PARSE_THREAD_THRESHOLD:
        return _parse_body(raw, decode_json, decode_protobuf, media_type, container_key), media_type

    decoded = await asyncio.to_thread(_parse_body, raw, decode_json, decode_protobuf, media_type, container_key)
    return decoded, media_type


async def _read_bounded_body(request: Request) -> bytes:
    """Read the request body, refusing anything larger than ``MAX_REQUEST_BYTES``.

    ``Content-Length`` is checked first so an honest oversized upload is rejected
    before it is read, then bytes are counted as they arrive so a chunked or
    understated upload still stops at the cap rather than after it.
    """
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > MAX_REQUEST_BYTES:
        raise OtlpError(413, f"Request body exceeds {MAX_REQUEST_BYTES} bytes")

    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > MAX_REQUEST_BYTES:
            raise OtlpError(413, f"Request body exceeds {MAX_REQUEST_BYTES} bytes")
        chunks.append(chunk)
    return b"".join(chunks)


def _parse_body(
    raw: bytes,
    decode_json: Callable[[dict], DecodeResult],
    decode_protobuf: Callable[[bytes], DecodeResult],
    media_type: str,
    container_key: str,
) -> DecodeResult:
    """Decode a decompressed export body. Synchronous; callers thread it when large."""
    if media_type == PROTOBUF_MEDIA_TYPE:
        try:
            return decode_protobuf(raw)
        except DecodeError as exc:
            raise OtlpError(400, f"Unable to parse Protobuf request body: {exc}") from exc

    try:
        body = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError, RecursionError) as exc:
        # RecursionError: json.loads rejects nesting past ~1000 levels, and the
        # exception is not a ValueError, so it needs naming explicitly.
        raise OtlpError(400, f"Unable to parse JSON request body: {exc}") from exc
    if not isinstance(body, dict):
        raise OtlpError(400, "OTLP JSON request body must be a JSON object")

    containers = body.get(container_key, [])
    if not isinstance(containers, list) or any(not isinstance(item, dict) for item in containers):
        raise OtlpError(400, f"Malformed OTLP payload: {container_key!r} must be a list of objects")

    return decode_json(body)


def build_export_response(message: Message, media_type: str) -> Response:
    """Serialize an ``Export<signal>ServiceResponse``, mirroring the request's Content-Type.

    Responses are deliberately never compressed. OTLP does not ask for it, an
    Export response is a few bytes (a full success serializes to zero bytes), and
    Go clients advertise ``Accept-Encoding: gzip`` automatically -- so compressing
    would inflate the common case rather than shrink it.
    """
    if media_type == PROTOBUF_MEDIA_TYPE:
        return Response(content=message.SerializeToString(), status_code=200, media_type=PROTOBUF_MEDIA_TYPE)
    return Response(content=MessageToJson(message).encode("utf-8"), status_code=200, media_type=JSON_MEDIA_TYPE)


def build_status_response(
    request: Request,
    status_code: int,
    message: str,
    media_type: str | None = None,
) -> Response:
    """Build the ``google.rpc.Status`` body OTLP requires for every 4xx and 5xx.

    The body encoding mirrors the request's Content-Type, which is what the
    reference Go receiver does and the only reading that keeps the response's
    own Content-Type honest. See ``docs/otel-compatibility.md`` for the
    rationale and the literal alternative.
    """
    if media_type is None:
        try:
            media_type = resolve_media_type(request)
        except OtlpError:
            media_type = JSON_MEDIA_TYPE

    status = Status(
        code=_GRPC_STATUS_CODES.get(status_code, _GRPC_STATUS_CODE_DEFAULT),
        message=message,
    )
    if media_type == PROTOBUF_MEDIA_TYPE:
        return Response(content=status.SerializeToString(), status_code=status_code, media_type=PROTOBUF_MEDIA_TYPE)
    return Response(content=MessageToJson(status).encode("utf-8"), status_code=status_code, media_type=JSON_MEDIA_TYPE)


def register_otlp_exception_handlers(app: FastAPI) -> None:
    """Ensure every 4xx/5xx out of the OTLP app carries a ``google.rpc.Status`` body.

    Registered on the OTLP app alone. ``require_trace_manager`` is shared with
    the main API, whose endpoints must keep their own ``{"detail": ...}`` error
    shape, so these handlers must never be installed globally.
    """

    @app.exception_handler(StarletteHTTPException)
    async def _handle_http_exception(request: Request, exc: StarletteHTTPException) -> Response:
        response = build_status_response(request, exc.status_code, str(exc.detail))
        # Preserve headers Starlette attaches to specific errors, such as the
        # `Allow` header a 405 is required to carry (RFC 9110).
        if exc.headers:
            response.headers.update(exc.headers)
        return response

    @app.exception_handler(OtlpError)
    async def _handle_otlp_error(request: Request, exc: OtlpError) -> Response:
        response = build_status_response(request, exc.status_code, exc.message)
        response.headers.update(exc.headers)
        return response

    @app.exception_handler(Exception)
    async def _handle_unexpected_error(request: Request, exc: Exception) -> Response:
        logger.exception("Unhandled error in OTLP receiver")
        return build_status_response(request, 500, "Internal server error")
