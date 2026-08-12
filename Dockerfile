# syntax=docker/dockerfile:1

FROM node:25-bookworm-slim AS ui
WORKDIR /build/ui
COPY ui/package.json ui/package-lock.json ./
# Skip lifecycle scripts during ci, then rebuild esbuild in its own layer — avoids ETXTBSY when
# install.js execs the binary while overlayfs still has the file busy (common with BuildKit).
RUN npm ci --ignore-scripts
RUN npm rebuild esbuild
COPY ui/ ./
RUN npm run build

FROM python:3.14-slim-bookworm

WORKDIR /app

# Install uv binary only (no pip); same approach as astral-sh/uv's Dockerfile.
# https://github.com/astral-sh/uv/blob/6d889fd53d5c108d304c5a4085eb3140ec6a9cdb/Dockerfile#L21
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

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

RUN uv sync --frozen --no-dev --extra live --extra postgres --extra kubernetes \
    # The runtime only uses the uv-managed venv; drop the base image's bundled
    # pip so its vendored packages (msgpack, pkg_resources) don't ship unused.
    && rm -rf /usr/local/lib/python*/site-packages/pip \
              /usr/local/lib/python*/site-packages/pip-*.dist-info \
              /usr/local/bin/pip* \
    && ! /usr/local/bin/python -c "import pip" 2>/dev/null \
    && groupadd --gid 1000 app \
    && useradd --uid 1000 --gid app --home-dir /app --no-log-init app \
    && chown -R app:app /app

USER app
ENV PATH="/app/.venv/bin:$PATH"
ENV AGENTEVALS_SERVER_URL=http://127.0.0.1:8001

EXPOSE 8001 4318 4317 8080

CMD ["agentevals", "serve", "--host", "0.0.0.0", "--port", "8001", "--otlp-http-port", "4318", "--otlp-grpc-port", "4317", "--mcp-port", "8080"]
