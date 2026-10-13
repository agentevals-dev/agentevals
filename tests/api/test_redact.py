"""Secrets out of run specs on every read path."""

from __future__ import annotations

import pytest

from agentevals.api.redact import REDACTED, redact_spec
from agentevals.storage.models import RunSpec, TraceTarget


def _spec(**fields) -> RunSpec:
    return RunSpec(target=fields.pop("target", TraceTarget(kind="inline", inline={})), **fields)


class TestHeaders:
    def test_literal_values_are_redacted_and_env_references_kept(self):
        spec = _spec(
            context={"headers": {"Authorization": "Bearer a", "X-Ref": {"from_env": "TOKEN_VAR"}}},
            sinks=[{"kind": "http_webhook", "url": "https://h.example.com", "headers": {"X-Key": "b"}}],
        )
        out = redact_spec(spec)
        assert out.context["headers"] == {"Authorization": REDACTED, "X-Ref": {"from_env": "TOKEN_VAR"}}
        assert out.sinks[0]["headers"] == {"X-Key": REDACTED}

    def test_a_reference_with_extra_keys_is_treated_as_a_value(self):
        out = redact_spec(_spec(context={"headers": {"X": {"from_env": "V", "value": "literal"}}}))
        assert out.context["headers"]["X"] == REDACTED


class TestSecretKeys:
    @pytest.mark.parametrize(
        "key", ["token", "api_key", "apiKey", "client_secret", "password", "dsn", "private_key", "Authorization"]
    )
    def test_secret_like_keys_are_redacted_at_any_depth(self, key):
        out = redact_spec(_spec(sinks=[{"kind": "custom", "auth": {key: "value", "region": "eu"}}]))
        assert out.sinks[0]["auth"] == {key: REDACTED, "region": "eu"}

    def test_other_keys_and_nulls_are_kept(self):
        out = redact_spec(_spec(sinks=[{"kind": "file", "path": "/var/out.ndjson", "token": None}]))
        assert out.sinks[0] == {"kind": "file", "path": "/var/out.ndjson", "token": None}


class TestUrls:
    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            ("https://hooks.example.com/services/T0/B0/abc", "https://hooks.example.com/REDACTED"),
            ("https://hooks.example.com?sig=abc", "https://hooks.example.com/REDACTED"),
            ("https://hooks.example.com:8443/", "https://hooks.example.com:8443"),
            ("https://user:pw@hooks.example.com/x", "https://hooks.example.com/REDACTED"),
            ("not a url", REDACTED),
        ],
    )
    def test_sink_urls_keep_only_their_origin(self, url, expected):
        assert redact_spec(_spec(sinks=[{"kind": "http_webhook", "url": url}])).sinks[0]["url"] == expected

    @pytest.mark.parametrize(
        ("base_url", "expected"),
        [
            ("https://reader:pw@traces.example.com:9443/api?key=k", "https://traces.example.com:9443/api"),
            ("https://traces.example.com/api?key=k", "https://traces.example.com/api"),
            ("https://traces.example.com/api", "https://traces.example.com/api"),
        ],
    )
    def test_trace_base_url_loses_userinfo_and_query(self, base_url, expected):
        target = TraceTarget(kind="http", base_url=base_url, trace_id="1dbd779ff94bf24c")
        assert redact_spec(_spec(target=target)).target.base_url == expected


def test_the_spec_itself_is_not_changed():
    spec = _spec(
        target=TraceTarget(kind="http", base_url="https://u:p@t.example.com/api", trace_id="1dbd779ff94bf24c"),
        context={"headers": {"Authorization": "Bearer a"}},
        sinks=[{"kind": "http_webhook", "url": "https://h.example.com/x", "headers": {"X-Key": "b"}}],
    )
    before = spec.model_dump()
    redact_spec(spec)
    assert spec.model_dump() == before
