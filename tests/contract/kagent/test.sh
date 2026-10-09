#!/usr/bin/env bash

# Checks the agentevals contract policy: every registry under registries/
# passes, and every testdata/<finding id>/ fails with that finding.

set -o errexit -o nounset -o pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

check() {
  ./weaver.sh registry check -r "/contract/$1" --v2 --policy /contract/policy --diagnostic-format json 2>&1
}

failed=0
for registry in registries/*/; do
  if output="$(check "${registry}")"; then
    echo "ok: ${registry} passes"
  else
    echo "FAIL: ${registry} should pass"
    echo "${output}" | tail -n 40
    failed=1
  fi
done
for fixture in testdata/*/; do
  id="$(basename "${fixture}")"
  if output="$(check "${fixture}")"; then
    echo "FAIL: ${id}: the check passed"
    failed=1
  elif ! grep -q "\"id\": \"${id}\"" <<<"${output}"; then
    echo "FAIL: ${id}: the check failed without that finding"
    echo "${output}" | tail -n 40
    failed=1
  else
    echo "ok: ${id}"
  fi
done
exit "${failed}"
