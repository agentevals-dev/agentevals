"""Kubernetes secret resolver tests.

The kubernetes client is mocked, so these run whether or not the optional
``kubernetes`` extra is installed.
"""

from __future__ import annotations

import base64
import sys
from unittest.mock import MagicMock

import pytest

from agentevals.resolvers.kubernetes import KubernetesSecretResolver, create_kubernetes_resolver


def _client_returning(data: dict[str, str]) -> MagicMock:
    client = MagicMock()
    client.read_namespaced_secret.return_value = MagicMock(data=data)
    return client


def _b64(value: str) -> str:
    return base64.b64encode(value.encode()).decode()


class TestResolve:
    async def test_reads_and_base64_decodes_value(self):
        client = _client_returning({"api-key": _b64("sk-secret-value")})
        resolver = KubernetesSecretResolver(client)

        value = await resolver.resolve({"namespace": "ns", "name": "creds", "key": "api-key"})

        assert value == "sk-secret-value"
        client.read_namespaced_secret.assert_called_once_with("creds", "ns")

    async def test_missing_fields_raise(self):
        resolver = KubernetesSecretResolver(MagicMock())
        with pytest.raises(ValueError, match="namespace, name, key"):
            await resolver.resolve({"kind": "kubernetes"})

    async def test_key_not_found_names_neither_keys_nor_values(self):
        secret_value = _b64("sk-do-not-leak")
        client = _client_returning({"api-key": secret_value, "tls.crt": _b64("cert")})
        resolver = KubernetesSecretResolver(client)

        with pytest.raises(ValueError) as exc:
            await resolver.resolve({"namespace": "ns", "name": "creds", "key": "wrong"})

        message = str(exc.value)
        assert "'wrong' not found in Secret ns/creds" in message
        # The error reaches API callers, so it must not reveal what else the Secret holds.
        assert "api-key" not in message and "tls.crt" not in message
        assert secret_value not in message


class TestFactory:
    def test_requires_kubernetes_extra(self, monkeypatch):
        # Shadow the kubernetes package so the lazy import fails regardless of
        # whether the extra is installed in the test venv.
        monkeypatch.setitem(sys.modules, "kubernetes", None)
        with pytest.raises(RuntimeError, match="kubernetes"):
            create_kubernetes_resolver({"kind": "kubernetes"})
