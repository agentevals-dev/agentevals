# syntax=docker/dockerfile:1

# Base images are pinned by digest so the layer cache survives upstream tag moves
# and builds stay reproducible. Keep the references literal: Dependabot does not
# resolve ARGs in FROM lines and silently skips images that use them.
FROM ghcr.io/astral-sh/uv:0.12.11@sha256:79c6f4776b851471cc73b7d21d0cc834bb94383c292e83640d27eff512864df7 AS uv-bin

FROM node:26-bookworm-slim@sha256:b822af76043e2656ffc172072036fe0968a3c09a692e4ad05eb9348ad5dbdf4c AS ui
WORKDIR /build/ui
COPY ui/package.json ui/package-lock.json ./
# Skip lifecycle scripts during ci, then rebuild esbuild in its own layer — avoids ETXTBSY when
# install.js execs the binary while overlayfs still has the file busy (common with BuildKit).
RUN npm ci --ignore-scripts
RUN npm rebuild esbuild
COPY ui/ ./
RUN npm run build

FROM cgr.dev/chainguard/wolfi-base:latest@sha256:6ab86c5e059da41ff1454b6f69ecd7422b94af41969f4e231aa23244e47d4a81

ARG PYTHON_VERSION=3.14
# Shared libraries the compiled wheels in the venv link against (scikit-learn and
# tokenizers need libstdc++); tzdata backs zoneinfo lookups. The upgrade pulls
# base package fixes (glibc, openssl) newer than the pinned digest whenever this
# layer rebuilds, so uncached release builds do not wait on a digest bump.
RUN apk upgrade --no-cache \
    && apk add --no-cache python-${PYTHON_VERSION} ca-certificates libstdc++ \
        zlib libffi sqlite-libs bzip2 xz tzdata

WORKDIR /app

# Install uv binary only (no pip); same approach as astral-sh/uv's Dockerfile.
# https://github.com/astral-sh/uv/blob/6d889fd53d5c108d304c5a4085eb3140ec6a9cdb/Dockerfile#L21
COPY --from=uv-bin /uv /usr/local/bin/uv

COPY pyproject.toml uv.lock README.md ./
COPY packages ./packages
COPY src ./src

COPY --from=ui /build/ui/dist ./src/agentevals/_static

# hatch-vcs reads the version from .git, which the docker build context omits.
# Pass it in as a build arg. We use the generic SETUPTOOLS_SCM_PRETEND_VERSION
# because hatch-vcs does not forward dist_name to setuptools-scm, so the
# per-package SETUPTOOLS_SCM_PRETEND_VERSION_FOR_<DIST> form is never consulted.
ARG VERSION
ENV SETUPTOOLS_SCM_PRETEND_VERSION=${VERSION}

RUN UV_PYTHON_PREFERENCE=only-system \
    uv sync --frozen --no-dev --extra live --extra postgres --extra kubernetes \
    && addgroup -g 1000 app \
    && adduser -u 1000 -G app -h /app -D -H app \
    && chown -R app:app /app

USER app
ENV PATH="/app/.venv/bin:$PATH"
ENV AGENTEVALS_SERVER_URL=http://127.0.0.1:8001

EXPOSE 8001 4318 4317 8002 8080

CMD ["agentevals", "serve", "--host", "0.0.0.0", "--port", "8001", "--otlp-http-port", "4318", "--otlp-grpc-port", "4317", "--mcp-port", "8080"]
