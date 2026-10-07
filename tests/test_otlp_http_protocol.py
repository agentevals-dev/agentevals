"""OTLP/HTTP protocol conformance tests for /v1/traces and /v1/logs.

Deliberately NOT under `tests/integration/`: that directory is marked
`pytest.mark.integration` per file, and CI's test job runs
`pytest -m "not integration and not e2e"`, so anything placed there never
executes in CI. These tests need no API keys and no real server, so they run
in the default unit job.

Fixtures are defined locally rather than hoisted to a top-level
`tests/conftest.py` so that ~50 unrelated test modules do not silently inherit
a trace manager.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import re

import httpx
import pytest
from google.rpc import code_pb2
from google.rpc.status_pb2 import Status
from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import ExportLogsServiceResponse
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceResponse
from opentelemetry.proto.common.v1.common_pb2 import AnyValue, KeyValue
from opentelemetry.proto.trace.v1.trace_pb2 import Span

from agentevals.api.otlp_app import create_otlp_app
from agentevals.api.otlp_http import MAX_GZIP_MEMBERS
from agentevals.otel.store import Limits
from agentevals.streaming.manager import LiveManager

JSON = "application/json"
PROTOBUF = "application/x-protobuf"


@pytest.fixture
async def trace_manager():
    """Fresh LiveManager with fast timers."""
    mgr = LiveManager(completion_grace_seconds=0.1, idle_timeout_seconds=0.5)
    mgr.start()
    yield mgr
    await mgr.shutdown()


def hex_id(label: str, nbytes: int = 16) -> str:
    """Receivers accept only hex ids; readable test labels map to stable ones."""
    if re.fullmatch(r"[0-9a-f]+", label) and len(label) == 2 * nbytes:
        return label
    return hashlib.sha256(label.encode()).hexdigest()[: 2 * nbytes]


@pytest.fixture
async def otlp_client(trace_manager):
    """httpx client → the real OTLP app (so exception handlers are wired in)."""
    app = create_otlp_app(trace_manager=trace_manager)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://otlp") as client:
        yield client


# ---------------------------------------------------------------------------
# Payload builders
# ---------------------------------------------------------------------------


def trace_request(trace_id: str, session_name: str = "http-test", span: bool = True, span_id: str = "bb" * 8) -> dict:
    """Build an ExportTraceServiceRequest JSON body."""
    spans = []
    if span:
        spans.append(
            {
                "traceId": hex_id(trace_id),
                "spanId": span_id,
                "name": "chat gpt-4o-mini",
                "kind": 3,
                "attributes": [
                    {"key": "gen_ai.operation.name", "value": {"stringValue": "chat"}},
                    {"key": "gen_ai.conversation.id", "value": {"stringValue": "conv-1"}},
                ],
            }
        )
    return {
        "resourceSpans": [
            {
                "resource": {
                    "attributes": [{"key": "agentevals.session_name", "value": {"stringValue": session_name}}]
                },
                "scopeSpans": [{"scope": {"name": "test"}, "spans": spans}],
            }
        ]
    }


def log_request(trace_id: str, event_name: str = "gen_ai.user.message", session_name: str = "http-test") -> dict:
    """Build an ExportLogsServiceRequest JSON body."""
    return {
        "resourceLogs": [
            {
                "resource": {
                    "attributes": [{"key": "agentevals.session_name", "value": {"stringValue": session_name}}]
                },
                "scopeLogs": [
                    {
                        "logRecords": [
                            {
                                "eventName": event_name,
                                "traceId": hex_id(trace_id),
                                "observedTimeUnixNano": "1500000000",
                                "body": {
                                    "kvlistValue": {"values": [{"key": "content", "value": {"stringValue": "hello"}}]}
                                },
                                "attributes": [],
                            }
                        ]
                    }
                ],
            }
        ]
    }


def proto_trace_request(trace_id_hex: str, session_name: str = "http-test", span_id_hex: str = "bb" * 8) -> bytes:
    """Build a serialized ExportTraceServiceRequest, as a Collector would send it."""
    from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest

    request = ExportTraceServiceRequest()
    resource_spans = request.resource_spans.add()
    resource_spans.resource.attributes.append(
        KeyValue(key="agentevals.session_name", value=AnyValue(string_value=session_name))
    )
    scope_spans = resource_spans.scope_spans.add()
    scope_spans.scope.name = "test"
    span = scope_spans.spans.add()
    span.trace_id = bytes.fromhex(trace_id_hex)
    span.span_id = bytes.fromhex(span_id_hex)
    span.name = "chat gpt-4o-mini"
    span.kind = Span.SPAN_KIND_CLIENT
    span.attributes.append(KeyValue(key="gen_ai.conversation.id", value=AnyValue(string_value="conv-1")))
    return request.SerializeToString()


def status_message(body: bytes, content_type: str) -> str:
    """Decode the google.rpc.Status message from an error response."""
    if PROTOBUF in content_type:
        return Status.FromString(body).message
    return json.loads(body)["message"]


# ---------------------------------------------------------------------------
# Compression
# ---------------------------------------------------------------------------


class TestContentEncoding:
    async def test_gzip_json_body_is_accepted(self, trace_manager, otlp_client):
        """The receiver MUST support gzip; a compressed JSON export succeeds."""
        payload = gzip.compress(json.dumps(trace_request("gzip-json")).encode())
        resp = await otlp_client.post(
            "/v1/traces",
            content=payload,
            headers={"Content-Type": JSON, "Content-Encoding": "gzip"},
        )
        assert resp.status_code == 200
        assert trace_manager.sessions["http-test"].span_count

    async def test_gzip_protobuf_body_is_accepted(self, trace_manager, otlp_client):
        """The stock Collector case: protobuf compressed with gzip (its default)."""
        payload = gzip.compress(proto_trace_request("aa" * 16))
        resp = await otlp_client.post(
            "/v1/traces",
            content=payload,
            headers={"Content-Type": PROTOBUF, "Content-Encoding": "gzip"},
        )
        assert resp.status_code == 200
        assert trace_manager.sessions["http-test"].span_count

    async def test_content_encoding_is_case_insensitive(self, otlp_client):
        payload = gzip.compress(json.dumps(trace_request("gzip-upper")).encode())
        resp = await otlp_client.post(
            "/v1/traces",
            content=payload,
            headers={"Content-Type": JSON, "Content-Encoding": "GZIP"},
        )
        assert resp.status_code == 200

    @pytest.mark.parametrize("encoding", ["br", "deflate", "zstd"])
    async def test_unsupported_content_encoding_is_rejected(self, otlp_client, encoding):
        """415, not 400: collectors offer these too, and the message names the fix."""
        resp = await otlp_client.post(
            "/v1/traces",
            content=b"whatever",
            headers={"Content-Type": JSON, "Content-Encoding": encoding},
        )
        assert resp.status_code == 415
        message = status_message(resp.content, resp.headers["content-type"])
        assert encoding in message
        assert "gzip" in message

    @pytest.mark.parametrize(
        "payload",
        [
            pytest.param(b"not actually gzip", id="not-gzip"),
            pytest.param(b"\x1f\x8b", id="two-bytes"),
            pytest.param("truncated-mid-stream", id="truncated-mid-stream"),
            pytest.param("missing-trailer", id="missing-trailer"),
        ],
    )
    async def test_malformed_gzip_is_rejected(self, otlp_client, payload):
        """Every corrupt-gzip shape is permanent bad data, so none may be a retryable 500.

        Truncation (what a proxy cut or half-flushed exporter produces) makes gzip
        raise EOFError, which is not an OSError subclass and so is easy to miss.
        """
        if isinstance(payload, str):
            good = gzip.compress(json.dumps(trace_request("truncated")).encode())
            payload = good[: len(good) // 2] if payload == "truncated-mid-stream" else good[:-8]

        resp = await otlp_client.post(
            "/v1/traces",
            content=payload,
            headers={"Content-Type": JSON, "Content-Encoding": "gzip"},
        )
        assert resp.status_code == 400
        assert status_message(resp.content, resp.headers["content-type"])

    async def test_oversized_decompressed_body_is_rejected(self, otlp_client, monkeypatch):
        """The limit bounds decompressed size, so a tiny compressed body can trip it."""
        monkeypatch.setattr("agentevals.api.otlp_http.MAX_DECOMPRESSED_BYTES", 64)
        bomb = gzip.compress(b"a" * 100_000)
        assert len(bomb) < 1000  # negligible on the wire, 100 KB when inflated

        resp = await otlp_client.post(
            "/v1/traces",
            content=bomb,
            headers={"Content-Type": JSON, "Content-Encoding": "gzip"},
        )
        assert resp.status_code == 413
        assert status_message(resp.content, resp.headers["content-type"])

    async def test_oversized_compressed_body_is_rejected(self, otlp_client, monkeypatch):
        """Compressed input is capped too: its cost is proportional to input bytes."""
        monkeypatch.setattr("agentevals.api.otlp_http.MAX_COMPRESSED_BYTES", 128)
        resp = await otlp_client.post(
            "/v1/traces",
            content=gzip.compress(json.dumps(trace_request("big")).encode()),
            headers={"Content-Type": JSON, "Content-Encoding": "gzip"},
        )
        assert resp.status_code == 413

    async def test_decompression_runs_off_the_event_loop(self, otlp_client, monkeypatch):
        """Decompression is CPU-bound and must not run on the shared event loop.

        This process also serves the dashboard API, the UI WebSockets and the gRPC
        receiver on one loop, so inline decompression would stall all of them. A
        body of many empty gzip members is the worst case: ~1.5us per *input*
        byte, since each member costs a header parse and CRC check and yields
        nothing.
        """
        import threading

        import agentevals.api.otlp_http as otlp_http

        loop_thread = threading.get_ident()
        worker_threads: list[int] = []
        original = otlp_http._decode_content_encoding

        def spy(request, raw):
            worker_threads.append(threading.get_ident())
            return original(request, raw)

        monkeypatch.setattr(otlp_http, "_decode_content_encoding", spy)
        resp = await otlp_client.post(
            "/v1/traces",
            content=gzip.compress(json.dumps(trace_request("off-loop")).encode()),
            headers={"Content-Type": JSON, "Content-Encoding": "gzip"},
        )

        assert resp.status_code == 200
        assert worker_threads, "decompression was never called"
        assert worker_threads[0] != loop_thread, "decompression ran on the event loop thread"

    async def test_large_body_parsing_runs_off_the_event_loop(self, otlp_client, monkeypatch):
        """Decode is threaded too, not just decompression.

        Parsing dominates: decoding protobuf measures ~40ms per MiB (~1.2s for
        a 32 MiB export), and it used to run entirely on the loop shared with the
        dashboard API, the UI streams and the gRPC receiver.
        """
        import threading

        import agentevals.api.otlp_http as otlp_http

        loop_thread = threading.get_ident()
        parse_threads: list[int] = []
        original = otlp_http._parse_body

        def spy(*args):
            parse_threads.append(threading.get_ident())
            return original(*args)

        monkeypatch.setattr(otlp_http, "_parse_body", spy)
        # Force the threaded branch; the real threshold is exercised by size.
        monkeypatch.setattr(otlp_http, "PARSE_THREAD_THRESHOLD", 1)

        resp = await otlp_client.post(
            "/v1/traces",
            content=json.dumps(trace_request("off-loop-parse")).encode(),
            headers={"Content-Type": JSON},
        )

        assert resp.status_code == 200
        assert parse_threads, "parse was never called"
        assert parse_threads[0] != loop_thread, "body parsing ran on the event loop thread"

    async def test_small_body_parsing_stays_inline(self, otlp_client, monkeypatch):
        """Below the threshold the thread hop is skipped; the work is negligible."""
        import threading

        import agentevals.api.otlp_http as otlp_http

        loop_thread = threading.get_ident()
        parse_threads: list[int] = []
        original = otlp_http._parse_body

        def spy(*args):
            parse_threads.append(threading.get_ident())
            return original(*args)

        monkeypatch.setattr(otlp_http, "_parse_body", spy)

        resp = await otlp_client.post(
            "/v1/traces",
            content=json.dumps(trace_request("inline-parse")).encode(),
            headers={"Content-Type": JSON},
        )

        assert resp.status_code == 200
        assert parse_threads == [loop_thread]

    async def test_empty_members_within_the_cap_are_accepted(self, otlp_client):
        """Concatenated members are legal gzip, so a body within the cap still works."""
        body = gzip.compress(b"") * MAX_GZIP_MEMBERS
        resp = await otlp_client.post(
            "/v1/traces",
            content=body,
            headers={"Content-Type": JSON, "Content-Encoding": "gzip"},
        )
        assert resp.status_code == 200

    async def test_multiple_members_are_joined(self, trace_manager, otlp_client):
        """Members are concatenated into one stream, not just tolerated.

        The payload is split mid-JSON, so this only passes if the halves are joined
        before parsing -- the behaviour gzip.GzipFile provided and the loop must keep.
        """
        whole = json.dumps(trace_request("joined-members")).encode()
        cut = len(whole) // 2
        body = gzip.compress(whole[:cut]) + gzip.compress(whole[cut:])

        resp = await otlp_client.post(
            "/v1/traces",
            content=body,
            headers={"Content-Type": JSON, "Content-Encoding": "gzip"},
        )

        assert resp.status_code == 200
        assert trace_manager.sessions["http-test"].span_count

    async def test_trailing_nul_padding_is_tolerated(self, otlp_client):
        """The gzip FAQ permits NUL padding; gzip.GzipFile skipped it, so we must too."""
        body = gzip.compress(json.dumps(trace_request("nul-padded")).encode()) + b"\x00" * 8
        resp = await otlp_client.post(
            "/v1/traces",
            content=body,
            headers={"Content-Type": JSON, "Content-Encoding": "gzip"},
        )
        assert resp.status_code == 200

    @pytest.mark.parametrize("corrupt_index", [-8, -1], ids=["crc32", "isize"])
    async def test_corrupted_trailer_is_rejected(self, otlp_client, corrupt_index):
        """The trailer is validated, so a corrupted checksum is not silently accepted.

        -8 is the CRC32, -1 is the last byte of ISIZE. Both leave the payload itself
        decodable, which is what makes this a real check rather than a header check.
        """
        body = bytearray(gzip.compress(json.dumps(trace_request("bad-trailer")).encode()))
        body[corrupt_index] ^= 0xFF

        resp = await otlp_client.post(
            "/v1/traces",
            content=bytes(body),
            headers={"Content-Type": JSON, "Content-Encoding": "gzip"},
        )
        assert resp.status_code == 400

    async def test_output_exactly_at_the_cap_is_accepted(self, trace_manager, otlp_client, monkeypatch):
        """The +1 probe must not turn output of exactly the cap size into a 413.

        Setting the cap to the payload's exact length: reading `MAX - len(out)`
        instead of `MAX + 1 - len(out)` would leave the stream unfinished at the
        boundary and reject a body that is precisely at the limit.
        """
        payload = json.dumps(trace_request("at-cap")).encode()
        monkeypatch.setattr("agentevals.api.otlp_http.MAX_DECOMPRESSED_BYTES", len(payload))

        resp = await otlp_client.post(
            "/v1/traces",
            content=gzip.compress(payload),
            headers={"Content-Type": JSON, "Content-Encoding": "gzip"},
        )
        assert resp.status_code == 200
        assert trace_manager.sessions["http-test"].span_count

    async def test_member_count_is_capped(self, otlp_client):
        """A body past the member cap is rejected rather than decompressed.

        Decompression cost scales with member count, not output, and an empty member
        is 20 bytes -- so without this cap an 8 MiB body of them would burn seconds
        of CPU (measured ~1.5us per input byte) and still answer 200.
        """
        body = gzip.compress(b"") * (MAX_GZIP_MEMBERS + 1)
        resp = await otlp_client.post(
            "/v1/traces",
            content=body,
            headers={"Content-Type": JSON, "Content-Encoding": "gzip"},
        )
        assert resp.status_code == 400
        assert str(MAX_GZIP_MEMBERS) in status_message(resp.content, resp.headers["content-type"])

    async def test_pathological_member_body_bounds_the_work(self, otlp_client, monkeypatch):
        """Regression guard: the cap must bound how many members are actually decoded.

        Counted rather than timed. The uncapped cost of a body this size is only
        milliseconds, so a wall-clock threshold would pass even with the cap removed
        -- the invariant is the number of decompressors constructed, not the clock.
        """
        import zlib as zlib_module

        constructed = 0
        real_decompressobj = zlib_module.decompressobj

        def counting_decompressobj(*args, **kwargs):
            nonlocal constructed
            constructed += 1
            return real_decompressobj(*args, **kwargs)

        monkeypatch.setattr(zlib_module, "decompressobj", counting_decompressobj)
        body = gzip.compress(b"") * (MAX_GZIP_MEMBERS * 10)

        resp = await otlp_client.post(
            "/v1/traces",
            content=body,
            headers={"Content-Type": JSON, "Content-Encoding": "gzip"},
        )

        assert resp.status_code == 400
        assert constructed <= MAX_GZIP_MEMBERS + 1, f"decoded {constructed} members, cap is {MAX_GZIP_MEMBERS}"

    async def test_oversized_identity_body_is_rejected(self, otlp_client, monkeypatch):
        """The uncompressed path is bounded as well; it previously was not."""
        monkeypatch.setattr("agentevals.api.otlp_http.MAX_REQUEST_BYTES", 128)
        resp = await otlp_client.post(
            "/v1/traces",
            content=json.dumps(trace_request("big")).encode(),
            headers={"Content-Type": JSON},
        )
        assert resp.status_code == 413


class TestResponsesAreNotCompressed:
    """Response compression is deliberately absent (see `build_export_response`).

    OTLP does not ask for it, Export responses are a few bytes, and Go clients
    advertise gzip automatically -- so compressing would inflate the common case.
    These pin that decision against someone re-adding the feature.
    """

    @pytest.mark.parametrize("accept_encoding", ["gzip", "identity", "gzip;q=0.5", "GZIP"])
    async def test_responses_are_never_gzipped(self, otlp_client, accept_encoding):
        resp = await otlp_client.post(
            "/v1/traces",
            content=json.dumps(trace_request("resp-compression")).encode(),
            headers={"Content-Type": JSON, "Accept-Encoding": accept_encoding},
        )
        assert resp.status_code == 200
        assert "content-encoding" not in resp.headers
        assert json.loads(resp.content) == {}

    async def test_error_responses_are_never_gzipped(self, otlp_client):
        resp = await otlp_client.post(
            "/v1/traces",
            content=b"{not json",
            headers={"Content-Type": JSON, "Accept-Encoding": "gzip"},
        )
        assert resp.status_code == 400
        assert "content-encoding" not in resp.headers


# ---------------------------------------------------------------------------
# Error responses
# ---------------------------------------------------------------------------


class TestErrorResponses:
    async def test_malformed_json_returns_400_with_status_body(self, otlp_client):
        """Undecodable data is permanent, so the client must not be told to retry."""
        resp = await otlp_client.post("/v1/traces", content=b"{not json", headers={"Content-Type": JSON})
        assert resp.status_code == 400
        assert status_message(resp.content, resp.headers["content-type"])

    async def test_malformed_protobuf_returns_400_with_status_body(self, otlp_client):
        resp = await otlp_client.post("/v1/traces", content=b"\xff\xff\xff\xff", headers={"Content-Type": PROTOBUF})
        assert resp.status_code == 400
        assert status_message(resp.content, resp.headers["content-type"])

    async def test_json_array_body_is_rejected(self, otlp_client):
        resp = await otlp_client.post("/v1/traces", content=b"[]", headers={"Content-Type": JSON})
        assert resp.status_code == 400

    async def test_deeply_nested_json_returns_400(self, otlp_client):
        """json.loads raises RecursionError past ~1000 levels, which is not a ValueError."""
        resp = await otlp_client.post("/v1/traces", content=b"[" * 3000 + b"]" * 3000, headers={"Content-Type": JSON})
        assert resp.status_code == 400
        assert status_message(resp.content, resp.headers["content-type"])

    @pytest.mark.parametrize(
        ("path", "body"),
        [
            pytest.param("/v1/traces", '{"resourceSpans": 5}', id="container-not-a-list"),
            pytest.param("/v1/traces", '{"resourceSpans": [null]}', id="container-element-null"),
            pytest.param("/v1/traces", '{"resourceSpans": {"a": 1}}', id="container-is-an-object"),
            pytest.param("/v1/logs", '{"resourceLogs": 5}', id="logs-container-not-a-list"),
        ],
    )
    async def test_structurally_invalid_json_returns_400(self, otlp_client, path, body):
        """Valid JSON with the wrong shape is still permanent bad data, not a 500."""
        resp = await otlp_client.post(path, content=body.encode(), headers={"Content-Type": JSON})
        assert resp.status_code == 400
        assert status_message(resp.content, resp.headers["content-type"])

    async def test_unknown_content_type_returns_415(self, otlp_client):
        resp = await otlp_client.post("/v1/traces", content=b"x", headers={"Content-Type": "text/plain"})
        assert resp.status_code == 415
        assert status_message(resp.content, resp.headers["content-type"])

    @pytest.mark.parametrize(
        ("body", "content_type", "expected_status", "expected_code"),
        [
            pytest.param(b"{not json", JSON, 400, code_pb2.INVALID_ARGUMENT, id="400"),
            pytest.param(b"x", "text/plain", 415, code_pb2.INVALID_ARGUMENT, id="415"),
        ],
    )
    async def test_error_status_carries_a_grpc_code(
        self, otlp_client, body, content_type, expected_status, expected_code
    ):
        """`Status.code` is populated so clients that branch on it never see OK.

        `google.rpc.Status.code` is declared `int32` (the `Code` enum is only a set
        of constants), so it renders as a number rather than a name.
        """
        resp = await otlp_client.post("/v1/traces", content=body, headers={"Content-Type": content_type})

        assert resp.status_code == expected_status
        assert json.loads(resp.content)["code"] == expected_code

    async def test_resource_exhausted_code_on_the_size_cap(self, otlp_client, monkeypatch):
        monkeypatch.setattr("agentevals.api.otlp_http.MAX_REQUEST_BYTES", 64)
        resp = await otlp_client.post("/v1/traces", content=b"x" * 4096, headers={"Content-Type": JSON})

        assert resp.status_code == 413
        assert json.loads(resp.content)["code"] == code_pb2.RESOURCE_EXHAUSTED

    async def test_error_status_carries_a_grpc_code_over_protobuf(self, otlp_client):
        resp = await otlp_client.post("/v1/traces", content=b"\xff\xff\xff", headers={"Content-Type": PROTOBUF})

        assert resp.status_code == 400
        decoded = Status.FromString(resp.content)
        assert decoded.code == code_pb2.INVALID_ARGUMENT
        assert decoded.message

    async def test_missing_trace_manager_returns_503_with_status_body(self):
        """`require_trace_manager` fails during dependency resolution, before the handler."""
        app = create_otlp_app()  # no trace_manager -> live mode off
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://otlp") as client:
            resp = await client.post("/v1/traces", content=b"{}", headers={"Content-Type": JSON})
        assert resp.status_code == 503
        assert status_message(resp.content, resp.headers["content-type"])

    async def test_method_not_allowed_keeps_its_allow_header(self, otlp_client):
        """A 405 must still carry `Allow` (RFC 9110) alongside the Status body."""
        resp = await otlp_client.get("/v1/traces")
        assert resp.status_code == 405
        assert "POST" in resp.headers.get("allow", "")
        assert status_message(resp.content, resp.headers["content-type"])

    async def test_unexpected_error_returns_500_with_status_body(self, otlp_client, monkeypatch):
        import agentevals.api.otlp_routes as routes

        async def boom(*args, **kwargs):
            raise RuntimeError("kaboom")

        monkeypatch.setattr(routes, "ingest_traces", boom)
        resp = await otlp_client.post(
            "/v1/traces", content=json.dumps(trace_request("boom")).encode(), headers={"Content-Type": JSON}
        )
        assert resp.status_code == 500
        assert status_message(resp.content, resp.headers["content-type"]) == "Internal server error"


# ---------------------------------------------------------------------------
# Content-Type mirroring
# ---------------------------------------------------------------------------


class TestContentTypeMirroring:
    async def test_json_request_gets_json_response(self, otlp_client):
        resp = await otlp_client.post(
            "/v1/traces", content=json.dumps(trace_request("ct-json")).encode(), headers={"Content-Type": JSON}
        )
        assert resp.status_code == 200
        assert JSON in resp.headers["content-type"]
        assert json.loads(resp.content) == {}

    async def test_json_request_with_charset_is_accepted(self, otlp_client):
        resp = await otlp_client.post(
            "/v1/traces",
            content=json.dumps(trace_request("ct-charset")).encode(),
            headers={"Content-Type": "application/json; charset=utf-8"},
        )
        assert resp.status_code == 200

    async def test_missing_content_type_defaults_to_json(self, otlp_client):
        resp = await otlp_client.post("/v1/traces", content=json.dumps(trace_request("ct-none")).encode())
        assert resp.status_code == 200

    async def test_empty_body_succeeds(self, otlp_client):
        resp = await otlp_client.post("/v1/traces", content=b"", headers={"Content-Type": JSON})
        assert resp.status_code == 200

    async def test_protobuf_request_gets_protobuf_response(self, otlp_client):
        resp = await otlp_client.post(
            "/v1/traces", content=proto_trace_request("cc" * 16), headers={"Content-Type": PROTOBUF}
        )
        assert resp.status_code == 200
        assert PROTOBUF in resp.headers["content-type"]
        parsed = ExportTraceServiceResponse.FromString(resp.content)
        assert not parsed.HasField("partial_success")

    async def test_protobuf_error_body_mirrors_content_type(self, otlp_client):
        resp = await otlp_client.post("/v1/traces", content=b"\xff\xff\xff", headers={"Content-Type": PROTOBUF})
        assert resp.status_code == 400
        assert PROTOBUF in resp.headers["content-type"]
        assert Status.FromString(resp.content).message


# ---------------------------------------------------------------------------
# Partial success
# ---------------------------------------------------------------------------


class TestPartialSuccess:
    async def test_full_success_leaves_partial_success_unset(self, otlp_client):
        resp = await otlp_client.post(
            "/v1/traces", content=json.dumps(trace_request("full-ok")).encode(), headers={"Content-Type": JSON}
        )
        assert resp.status_code == 200
        assert json.loads(resp.content) == {}

    async def test_span_limit_reports_rejected_spans(self, trace_manager, otlp_client):
        trace_id = "limit-trace"
        await otlp_client.post(
            "/v1/traces",
            content=json.dumps(trace_request(trace_id)).encode(),
            headers={"Content-Type": JSON},
        )
        trace_manager.store.limits = Limits(spans_per_trace=1)

        resp = await otlp_client.post(
            "/v1/traces",
            content=json.dumps(trace_request(trace_id, span_id="cc" * 8)).encode(),
            headers={"Content-Type": JSON},
        )
        assert resp.status_code == 200
        partial = json.loads(resp.content)["partialSuccess"]
        # proto3 JSON maps int64 to a string, and rejected_spans is int64.
        assert int(partial["rejectedSpans"]) == 1
        assert "trace span limit" in partial["errorMessage"]

    async def test_span_limit_reports_rejected_spans_over_protobuf(self, trace_manager, otlp_client):
        trace_id = "dd" * 16
        await otlp_client.post("/v1/traces", content=proto_trace_request(trace_id), headers={"Content-Type": PROTOBUF})
        trace_manager.store.limits = Limits(spans_per_trace=1)

        resp = await otlp_client.post(
            "/v1/traces",
            content=proto_trace_request(trace_id, span_id_hex="cc" * 8),
            headers={"Content-Type": PROTOBUF},
        )
        assert resp.status_code == 200
        parsed = ExportTraceServiceResponse.FromString(resp.content)
        assert parsed.partial_success.rejected_spans == 1
        assert parsed.partial_success.error_message

    async def test_log_limit_reports_rejected_log_records(self, trace_manager, otlp_client):
        trace_id = "log-limit-trace"
        await otlp_client.post(
            "/v1/traces",
            content=json.dumps(trace_request(trace_id)).encode(),
            headers={"Content-Type": JSON},
        )
        trace_manager.store.limits = Limits(logs_per_trace=0)

        resp = await otlp_client.post(
            "/v1/logs", content=json.dumps(log_request(trace_id)).encode(), headers={"Content-Type": JSON}
        )
        assert resp.status_code == 200
        partial = json.loads(resp.content)["partialSuccess"]
        assert int(partial["rejectedLogRecords"]) == 1
        assert "trace log limit" in partial["errorMessage"]

    async def test_span_without_trace_id_is_reported(self, trace_manager, otlp_client):
        body = trace_request("missing-trace-id")
        body["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["traceId"] = ""
        resp = await otlp_client.post("/v1/traces", content=json.dumps(body).encode(), headers={"Content-Type": JSON})
        assert resp.status_code == 200
        partial = json.loads(resp.content)["partialSuccess"]
        assert int(partial["rejectedSpans"]) == 1
        assert "invalid trace or span id" in partial["errorMessage"]

    async def test_filtered_non_genai_logs_are_not_reported_as_rejected(self, otlp_client):
        """Non-gen_ai.* records are filtered by design, not rejected: no partial success."""
        resp = await otlp_client.post(
            "/v1/logs",
            content=json.dumps(log_request("ignored-log", event_name="http.server.request")).encode(),
            headers={"Content-Type": JSON},
        )
        assert resp.status_code == 200
        assert json.loads(resp.content) == {}

    async def test_logs_protobuf_full_success(self, otlp_client):
        resp = await otlp_client.post("/v1/logs", content=b"", headers={"Content-Type": PROTOBUF})
        assert resp.status_code == 200
        assert not ExportLogsServiceResponse.FromString(resp.content).HasField("partial_success")


# ---------------------------------------------------------------------------
# Body size bounding
# ---------------------------------------------------------------------------


class TestBodySizeBound:
    async def test_declared_oversized_body_is_rejected(self, otlp_client, monkeypatch):
        """A Content-Length over the cap is refused before the body is read."""
        monkeypatch.setattr("agentevals.api.otlp_http.MAX_REQUEST_BYTES", 1024)
        resp = await otlp_client.post("/v1/traces", content=b"x" * 4096, headers={"Content-Type": JSON})
        assert resp.status_code == 413

    async def test_chunked_oversized_body_is_rejected_midstream(self, otlp_client, monkeypatch):
        """A body with no Content-Length is stopped while streaming, not after buffering.

        Without the running total this would be read in full and only then judged.
        """
        monkeypatch.setattr("agentevals.api.otlp_http.MAX_REQUEST_BYTES", 1024)

        async def unbounded():
            for _ in range(64):
                yield b"x" * 1024

        resp = await otlp_client.post("/v1/traces", content=unbounded(), headers={"Content-Type": JSON})
        assert resp.status_code == 413

    async def test_body_within_the_cap_is_accepted(self, otlp_client, monkeypatch):
        monkeypatch.setattr("agentevals.api.otlp_http.MAX_REQUEST_BYTES", 1024 * 1024)
        resp = await otlp_client.post(
            "/v1/traces",
            content=json.dumps(trace_request("within-cap")).encode(),
            headers={"Content-Type": JSON},
        )
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Scoping
# ---------------------------------------------------------------------------


class TestExceptionHandlersAreScopedToTheOtlpApp:
    """The OTLP Status bodies must not leak onto the dashboard API.

    `require_trace_manager` is shared with the main app and `debug_routes`, so the
    handlers are registered per-app. A future refactor that hoists them to a shared
    spot would otherwise silently change the main API's error shape.
    """

    async def test_main_app_keeps_its_own_error_shape(self):
        from agentevals.api.app import create_app

        transport = httpx.ASGITransport(app=create_app(), raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://main") as client:
            resp = await client.get("/api/definitely-not-a-route")

        assert resp.status_code == 404
        assert json.loads(resp.content) == {"detail": "Not Found"}
