"""OTLP/HTTP protocol handling: content negotiation, compression, error bodies.

Kept separate from `otlp_routes.py` so the routing layer stays thin and this
logic can be unit-tested without an HTTP server (the convention used by
`otlp_processing.py`). Nothing here is gRPC-specific; `otlp_grpc.py` reuses the
response builders from `otlp_processing.py` instead.
"""

from __future__ import annotations

import asyncio
import gzip
import json
import logging
import zlib
from collections.abc import Callable

from fastapi import FastAPI, Request, Response
from google.protobuf.json_format import MessageToJson
from google.protobuf.message import DecodeError, Message
from google.rpc.status_pb2 import Status
from starlette.exceptions import HTTPException as StarletteHTTPException

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

_SUPPORTED_CONTENT_ENCODINGS = frozenset({"", "identity", "gzip"})


class OtlpError(Exception):
    """An OTLP/HTTP failure that must be returned as a `google.rpc.Status` body."""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message


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
        raise OtlpError(400, f"Unsupported Content-Encoding {encoding!r}; supported encodings: gzip")
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
    decode_protobuf: Callable[[bytes], dict],
    container_key: str,
) -> tuple[dict, str]:
    """Read, decompress and decode an OTLP/HTTP export request.

    Returns the OTLP body as a dict plus the media type the response must use.
    Every decoding failure is permanent, so it surfaces as a 400.

    ``container_key`` is the top-level request list (``resourceSpans`` or
    ``resourceLogs``); its type is checked so a structurally invalid JSON body
    fails as a 400 rather than crashing the pipeline as a 500.
    """
    media_type = resolve_media_type(request)
    body_bytes = await request.body()
    if len(body_bytes) > MAX_REQUEST_BYTES:
        raise OtlpError(413, f"Request body exceeds {MAX_REQUEST_BYTES} bytes")

    # Decompression is CPU-bound. Run it in a worker thread: this process also
    # serves the dashboard API and the gRPC receiver on one event loop, so blocking
    # here would stall all of them.
    raw = await asyncio.to_thread(_decode_content_encoding, request, body_bytes)

    if not raw:
        return {}, media_type

    if media_type == PROTOBUF_MEDIA_TYPE:
        try:
            return decode_protobuf(raw), media_type
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

    return body, media_type


def _accepts_gzip(request: Request) -> bool:
    """Whether the client advertised gzip in ``Accept-Encoding``."""
    for part in request.headers.get("accept-encoding", "").split(","):
        token, _, params = part.partition(";")
        if token.strip().lower() != "gzip":
            continue
        quality = 1.0
        for param in params.split(";"):
            key, _, value = param.partition("=")
            if key.strip().lower() == "q":
                try:
                    quality = float(value)
                except ValueError:
                    quality = 0.0
        return quality > 0
    return False


def _finalize_response(request: Request, payload: bytes, content_type: str, status_code: int) -> Response:
    """Wrap a payload, gzip-encoding it when the client accepts that."""
    headers: dict[str, str] = {}
    # An empty payload is left alone: a full-success protobuf response is zero
    # bytes, and gzip framing would turn it into a non-empty body for no gain.
    if payload and _accepts_gzip(request):
        payload = gzip.compress(payload)
        headers["content-encoding"] = "gzip"
    return Response(content=payload, status_code=status_code, media_type=content_type, headers=headers)


def build_export_response(request: Request, message: Message, media_type: str) -> Response:
    """Serialize an ``Export<signal>ServiceResponse``, mirroring the request's Content-Type."""
    if media_type == PROTOBUF_MEDIA_TYPE:
        return _finalize_response(request, message.SerializeToString(), PROTOBUF_MEDIA_TYPE, 200)
    return _finalize_response(request, MessageToJson(message).encode("utf-8"), JSON_MEDIA_TYPE, 200)


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

    status = Status(message=message)
    if media_type == PROTOBUF_MEDIA_TYPE:
        return _finalize_response(request, status.SerializeToString(), PROTOBUF_MEDIA_TYPE, status_code)
    return _finalize_response(request, MessageToJson(status).encode("utf-8"), JSON_MEDIA_TYPE, status_code)


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
        return build_status_response(request, exc.status_code, exc.message)

    @app.exception_handler(Exception)
    async def _handle_unexpected_error(request: Request, exc: Exception) -> Response:
        logger.exception("Unhandled error in OTLP receiver")
        return build_status_response(request, 500, "Internal server error")
