#!/usr/bin/env bash

# Runs the pinned Weaver image, locked down, with this directory mounted read
# only at /contract. Weaver only ever runs from this pin, never from a version
# named in kagent's own files.

set -o errexit -o nounset -o pipefail

WEAVER_IMAGE="docker.io/otel/weaver:v0.27.0@sha256:3049b4079049d4abb1b5632f511ada2c33505a1c60f3f8535e93f87f0696f056"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

exec docker run --rm --read-only --tmpfs /tmp:rw,size=1g \
  --memory 1g --pids-limit 256 --cap-drop ALL --security-opt no-new-privileges \
  -u "$(id -u):$(id -g)" -e HOME=/tmp -v "${HERE}:/contract:ro" \
  "${WEAVER_IMAGE}" "$@"
