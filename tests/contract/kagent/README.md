# kagent telemetry contract

What agentevals reads from kagent spans, checked with [Weaver](https://github.com/open-telemetry/weaver) against kagent's telemetry registry every day by `.github/workflows/kagent-contract.yml`. A failure opens or updates one issue labelled `kagent-contract`; it never fails a pull request.

| Path | What |
|---|---|
| `policy/agentevals.rego` | The contract: the attributes agentevals needs per upstream span group type, and the requirement levels that must not drop. A Weaver `after_resolution` policy that only reports violations. |
| `registries/<name>/` | Copies of kagent's `telemetry/registry` at fixed commits. They must pass. `main-2cc3ffae` is also the baseline for the daily `weaver registry diff`. |
| `testdata/<finding id>/` | Registries that must fail with that finding. |
| `weaver.sh` | Runs the pinned Weaver image, locked down. |
| `test.sh` | The self test: registries pass, testdata fails with its finding. |
| `check.sh` | Checks kagent refs (`main` or a release tag) and writes the report. |

Groups are matched by the upstream type they refine (`gen_ai.invoke_agent.internal`, `gen_ai.client.inference`, `gen_ai.execute_tool.internal`), never by kagent ids. Weaver resolves kagent's pinned upstream semantic conventions, so a group kagent only imports is checked too. When kagent adds its own refinement of a type, that refinement is checked instead.

Run it locally (needs Docker, `jq` and network):

```bash
tests/contract/kagent/test.sh
tests/contract/kagent/check.sh report.md main v1.0.0-alpha9
```

When kagent changes:

* A new requirement need goes into `contract` in the policy, with a `testdata/<finding id>/` case if it adds a finding.
* To track a new release, copy its `telemetry/registry` into `registries/<tag>/`.
* If kagent moves to a Weaver release the pinned image cannot read, the report shows Weaver's error. Bump `WEAVER_IMAGE` in `weaver.sh` by digest.
