#!/usr/bin/env bash

# Checks kagent's registry at each given ref (main or a release tag) against
# the agentevals contract and writes a Markdown report. Exits 1 if any ref
# fails. Weaver's exit code is the result; the report lists its findings.
#
#   tests/contract/kagent/check.sh <report.md> main v1.0.0-alpha9

set -o errexit -o nounset -o pipefail

KAGENT_GIT="https://github.com/kagent-dev/kagent.git"
REF_PATTERN='^(main|v[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.]+)?)$'
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

report="$1"
shift

body="$(mktemp)"
failed=0
for ref in "$@"; do
  if [[ ! "${ref}" =~ ${REF_PATTERN} ]]; then
    echo "refusing kagent ref, expected main or a release tag" >&2
    exit 2
  fi
  if output="$("${HERE}/weaver.sh" registry check -r "${KAGENT_GIT}@${ref}[telemetry/registry]" \
    --v2 --policy /contract/policy --diagnostic-format json 2>&1)"; then
    printf "## \`kagent@%s\`: ok\n\n" "${ref}" >>"${body}"
    continue
  fi
  failed=1
  {
    echo "## \`kagent@${ref}\`: failed"
    echo
    echo '```text'
    # Weaver's JSON diagnostics without warnings: policy findings and errors.
    # Backticks are dropped so fetched names cannot end the code block.
    sed -n '/^\[$/,/^\]$/p' <<<"${output}" | jq -r '.[]
      | select(.diagnostic.severity != "Warning")
      | if .error.type == "policy_violation"
        then "\(.error.violation.id): \(.error.violation.message)"
        else .diagnostic.message // "unknown error" end' 2>/dev/null | tr -d '`' \
      || echo "Weaver produced no diagnostics."
    echo '```'
    echo
  } >>"${body}"
done

{
  echo "# kagent telemetry contract"
  echo
  echo "kagent's telemetry registry checked against what agentevals reads (\`tests/contract/kagent/policy/agentevals.rego\`)."
  echo
  cat "${body}"
  echo
  echo "<!-- kagent-contract-report: $(sha256sum "${body}" | cut -d' ' -f1) -->"
} >"${report}"
rm -f "${body}"
cat "${report}"
exit "${failed}"
