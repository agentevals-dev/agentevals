"""Kubernetes Secret resolver — an optional :class:`SecretResolver` plugin.

Resolves a reference of the form ``{"kind": "kubernetes", "namespace": ..., "name": ...,
"key": ...}`` by reading the named Secret and base64-decoding the requested key. Ships
behind the ``kubernetes`` extra and is wired through the ``agentevals.secret_resolvers``
entry-point group; the ``kubernetes`` package is imported lazily inside the factory so
installing agentevals without the extra never breaks import or plugin discovery.
"""

from __future__ import annotations

import asyncio
import base64
import logging
from typing import Any

logger = logging.getLogger(__name__)


class KubernetesSecretResolver:
    """Reads a key out of a Kubernetes Secret via a ``CoreV1Api`` client."""

    def __init__(self, core_v1_api: Any) -> None:
        self._core_v1 = core_v1_api

    async def resolve(self, ref: dict[str, Any]) -> str:
        namespace = ref.get("namespace")
        name = ref.get("name")
        key = ref.get("key")
        missing = [f for f, v in (("namespace", namespace), ("name", name), ("key", key)) if not v]
        if missing:
            raise ValueError(f"kubernetes secret reference is missing required field(s): {', '.join(missing)}")

        secret = await asyncio.to_thread(self._core_v1.read_namespaced_secret, name, namespace)
        data = secret.data or {}
        if key not in data:
            raise ValueError(f"key '{key}' not found in Secret {namespace}/{name}")
        return base64.b64decode(data[key]).decode("utf-8")


def create_kubernetes_resolver(spec: dict[str, Any]) -> KubernetesSecretResolver:
    """Build a :class:`KubernetesSecretResolver`, loading cluster config lazily.

    Tries in-cluster config first (for pods with a mounted service account), then falls
    back to the local kubeconfig for development. The ``kubernetes`` package is imported
    here rather than at module load so the plugin can be discovered even when the extra
    is not installed.
    """
    try:
        from kubernetes import client, config
    except ImportError as exc:
        raise RuntimeError(
            "the kubernetes secret resolver requires the 'kubernetes' extra; install agentevals-cli[kubernetes]"
        ) from exc

    try:
        config.load_incluster_config()
    except config.ConfigException:
        config.load_kube_config()

    return KubernetesSecretResolver(client.CoreV1Api())
