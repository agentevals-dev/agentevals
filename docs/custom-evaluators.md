# Custom Evaluators

Custom evaluators let you score agent traces with your own logic. An evaluator is any program that reads `EvalInput` JSON from stdin and writes `EvalResult` JSON to stdout. This simple protocol means you can write evaluators in Python, JavaScript/TypeScript, or any language that can read/write JSON.

## Quick Start

### 1. Scaffold an evaluator

```bash
agentevals evaluator init my_evaluator
```

This creates a directory with boilerplate code and an `evaluator.yaml` manifest:

```
my_evaluator/
├── my_evaluator.py     # scoring logic (implement your checks here)
└── evaluator.yaml      # metadata manifest
```

You can also specify a language:

```bash
agentevals evaluator init my_evaluator --runtime js    # JavaScript
agentevals evaluator init my_evaluator.ts              # TypeScript (inferred from extension)
```

### 2. Install the SDK (Python only)

```bash
pip install agentevals-evaluator-sdk
```

### 3. Write an evaluator

```python
# evaluators/response_quality.py
from agentevals_evaluator_sdk import evaluator, EvalInput, EvalResult


@evaluator
def response_quality(input: EvalInput) -> EvalResult:
    scores = []
    for inv in input.invocations:
        if not inv.final_response:
            scores.append(0.0)
        elif len(inv.final_response.strip()) < input.config.get("min_length", 10):
            scores.append(0.5)
        else:
            scores.append(1.0)

    return EvalResult(
        score=sum(scores) / len(scores) if scores else 0.0,
        per_invocation_scores=scores,
    )


if __name__ == "__main__":
    response_quality.run()
```

The `@evaluator` decorator marks your function as an evaluator. Call `.run()` to execute it as a stdin/stdout script. Your function receives an `EvalInput` and returns an `EvalResult`. The decorated function can still be called directly in tests.

### 3. Add it to your eval config

```yaml
# eval_config.yaml
evaluators:
  - name: tool_trajectory_avg_score   # built-in metric
    type: builtin

  - name: response_quality            # your custom evaluator
    type: code
    path: ./evaluators/response_quality.py
    threshold: 0.7
    config:
      min_length: 20
```

### 4. Run

```bash
agentevals run traces/my_trace.json \
  --config eval_config.yaml \
  --eval-set eval_set.json
```

## Eval Config Reference

Each evaluator entry in the `evaluators` list uses the following fields. The `type` field determines which other fields are valid.

### `type: code` (local scripts)

| Field | Required | Default | Description |
|---|---|---|---|
| `name` | yes | | Unique name for the evaluator (used in output) |
| `type` | yes | | `code` for local code files |
| `path` | yes | | Path to the evaluator file (`.py`, `.js`, or `.ts`) |
| `threshold` | no | `0.5` | Score at or above this value means PASSED |
| `timeout` | no | `30` | Subprocess timeout in seconds |
| `config` | no | `{}` | Arbitrary key-value pairs passed to the evaluator |

## Protocol

Every evaluator, in any language, uses the same JSON protocol over stdin and stdout.

### Input (`EvalInput`)

```json
{
  "protocol_version": "1.1",
  "metric_name": "response_quality",
  "threshold": 0.7,
  "config": { "min_length": 20 },
  "invocations": [
    {
      "invocation_id": "a1b2c3d4e5f60718",
      "trace_id": "4bf92f3577b34da6a3ce929d0e0e4736",
      "span_id": "a1b2c3d4e5f60718",
      "user_content": "What is 2+2?",
      "final_response": "The answer is 4.",
      "intermediate_steps": {
        "tool_calls": [
          { "id": "call_1", "name": "calculator", "args": { "expression": "2+2" } }
        ],
        "tool_responses": [
          { "id": "call_1", "name": "calculator", "output": "{\"result\": 4}", "response": { "result": 4 } }
        ]
      }
    }
  ],
  "expected_invocations": null
}
```

| Field | Type | Description |
|---|---|---|
| `protocol_version` | string | Wire format version (`"MAJOR.MINOR"`). Current: `"1.1"` |
| `metric_name` | string | Name of this evaluator |
| `threshold` | float | Pass/fail threshold |
| `config` | object | User-provided config from the YAML |
| `invocations` | array | Agent turns to evaluate |
| `expected_invocations` | array or null | Golden reference turns (from eval set) |

Each invocation contains:

| Field | Type | Description |
|---|---|---|
| `invocation_id` | string | Unique turn id: the span id of the turn's agent span |
| `trace_id`, `span_id` | string or null | Where the turn is in the telemetry (1.1) |
| `user_content` | string | What the user said |
| `final_response` | string or null | The agent's final response |
| `intermediate_steps` | object | Steps between user input and final response |

The `intermediate_steps` object contains:

| Field | Type | Description |
|---|---|---|
| `tool_calls` | array | Tools the agent called: `name`, `args`, and `id` when the producer recorded one (1.1) |
| `tool_responses` | array | Tool results: `name`, `output` (the result as JSON text), `response` (the same result as a JSON value, 1.1) and `id` of the call it answers (1.1) |

Tool results used to arrive in `output` as Python text in 1.0. Read `response` for structured access.

### Output (`EvalResult`)

```json
{
  "score": 0.85,
  "status": null,
  "per_invocation_scores": [1.0, 0.7],
  "details": { "issues": ["inv-002: response too short"] }
}
```

| Field | Required | Description |
|---|---|---|
| `score` | yes | Overall score between 0.0 and 1.0 |
| `status` | no | `"PASSED"`, `"FAILED"`, or `"NOT_EVALUATED"`. If omitted, derived from score vs threshold. |
| `per_invocation_scores` | no | Per-turn scores (same order as input invocations) |
| `details` | no | Arbitrary metadata for debugging |

### Protocol Versioning

The `protocol_version` field uses `"MAJOR.MINOR"` format (currently `"1.1"`). This lets the CLI and SDK evolve independently:

* **Additive only.** New fields may be added; existing fields are never removed or renamed within a major version.
* **Defaults required.** Every new field has a default, and older SDKs ignore unknown fields, so an evaluator built against 1.0 keeps working with a 1.1 CLI.
* **Minor bumps** add optional fields. Nothing to do for evaluator authors.
* **Major bumps** are breaking. The SDK's `@evaluator` decorator warns when it sees a major version it does not know.

The CLI and SDK are **independent packages**. Install them at whatever versions you need:

```bash
pip install agentevals                 # CLI, speaks protocol 1.1
pip install agentevals-evaluator-sdk   # SDK, works with 1.0 and 1.1
```

As long as the major version matches, they are compatible.

## Writing Evaluators in Other Languages

You don't need the Python SDK. Any program that reads JSON from stdin and writes JSON to stdout works.

### JavaScript / TypeScript

```javascript
// evaluators/tool_check.js
const input = JSON.parse(require("fs").readFileSync("/dev/stdin", "utf8"));

let score = 1.0;
for (const inv of input.invocations) {
  if (inv.intermediate_steps.tool_calls.length === 0) {
    score -= 0.5;
  }
}

console.log(JSON.stringify({
  score: Math.max(0, score),
  per_invocation_scores: [],
}));
```

```yaml
evaluators:
  - name: tool_check
    type: code
    path: ./evaluators/tool_check.js
```

### Any language

Write a program that:

1. Reads all of stdin as a UTF-8 string
2. Parses it as JSON (matching the `EvalInput` schema)
3. Writes a JSON object to stdout (matching the `EvalResult` schema)
4. Exits with code 0 on success, non-zero on failure

The file extension determines which interpreter is used:

| Extension | Command |
|---|---|
| `.py` | `python <file>` |
| `.js`, `.ts` | `node <file>` |

## Discovering Evaluators

### List available evaluators

```bash
agentevals evaluator list                    # all sources
agentevals evaluator list --source builtin   # only ADK built-in metrics
agentevals evaluator list --source github    # only community evaluators
```

This shows evaluators from all registered sources: ADK built-in metrics and the community GitHub repository.

## Remote Evaluators

You can reference evaluators from the community repository directly in your eval config. They are downloaded and cached automatically on first use.

```yaml
evaluators:
  - name: tool_trajectory_avg_score
    type: builtin

  - name: response_quality
    type: remote
    source: github
    ref: evaluators/response_quality/response_quality.py
    threshold: 0.7
```

| Field | Required | Default | Description |
|---|---|---|---|
| `name` | yes | | Unique name for the evaluator (used in output) |
| `type` | yes | | `remote` for evaluators fetched from a registry |
| `source` | no | `github` | Evaluator source (`github`, or custom) |
| `ref` | yes | | Path within the source (e.g. path in the GitHub repo) |
| `threshold` | no | `0.5` | Score at or above this value means PASSED |
| `timeout` | no | `30` | Subprocess timeout in seconds |
| `config` | no | `{}` | Arbitrary key-value pairs passed to the evaluator |
| `executor` | no | `local` | Execution environment (`local` or `docker` in the future) |

Remote evaluators are cached in `~/.cache/agentevals/evaluators/`. To force a re-download, delete the cached file.

### Configuring the GitHub source

By default, evaluators are fetched from the official community repository. Override with environment variables:

```bash
export AGENTEVALS_EVALUATOR_REPO="your-org/your-evaluators-repo"
export AGENTEVALS_EVALUATOR_BRANCH="main"
```

## Contributing Evaluators to the Community

1. Scaffold a new evaluator:

```bash
agentevals evaluator init my_evaluator
```

2. Implement your scoring logic and update the `evaluator.yaml` manifest with a description, tags, and your name.

3. Copy the `my_evaluator/` directory into the `evaluators/` folder of the community repository and open a PR.

The community repo uses per-evaluator manifests. A CI workflow compiles all `evaluators/*/evaluator.yaml` files into a single `index.yaml` on merge, which is what `agentevals evaluator list` fetches.

## Architecture

Custom evaluators use a layered architecture designed for extensibility.

```
┌──────────────────────────┐
│  Eval Config (YAML)      │
│  type: code | remote     │
└──────────┬───────────────┘
           │
           ▼
┌──────────────────────────┐
│  EvaluatorResolver       │
│  remote → local          │
│  (passthrough: code)     │
└──────────┬───────────────┘
           │
           ▼
┌──────────────────────────┐
│  evaluate_custom_evaluator│
│  turns ↔ EvalInput       │
│  EvalResult → metric     │
└──────────┬───────────────┘
           │
           ▼
┌──────────────────────────┐
│  EvaluatorBackend (ABC)  │
│  "local"  → Subprocess   │
│  "docker" → (future)     │
└──────────┬───────────────┘
           │
           ▼
┌──────────────────────────┐
│  Runtime registry        │
│  PythonRuntime (.py)     │
│  NodeRuntime (.js, .ts)  │
└──────────────────────────┘
```

- **`EvaluatorSource`** is the registry abstraction. Implementations (`BuiltinEvaluatorSource`, `GitHubEvaluatorSource`) list and fetch evaluators from different registries.
- **`EvaluatorResolver`** downloads remote evaluators and converts `RemoteEvaluatorDef` to `CodeEvaluatorDef` with a local cached path.
- **`EvaluatorBackend`** is the execution abstraction. The `executor` field in config selects which factory to use (`"local"` → `SubprocessBackend`). New executors (e.g. `DockerBackend`) register via `register_executor()`.
- **`SubprocessBackend`** runs a local file as a child process, piping JSON over stdin/stdout.
- **`Runtime`** is an internal detail of `SubprocessBackend` that maps file extensions to interpreter commands.
- **`evaluate_custom_evaluator`** turns the extracted conversation into `EvalInput`, runs the backend and maps `EvalResult` back to a metric result. It does not use ADK.

### Adding a new language runtime

To support a new language (e.g., Go), add a `Runtime` subclass in `custom_evaluators.py`:

```python
class GoRuntime(Runtime):
    @property
    def extensions(self) -> tuple[str, ...]:
        return (".go",)

    def build_command(self, path: Path) -> list[str]:
        go = shutil.which("go")
        if not go:
            raise RuntimeError("Go not found on PATH")
        return [go, "run", str(path)]
```

Then register it:

```python
_RUNTIMES: list[Runtime] = [
    PythonRuntime(),
    NodeRuntime(),
    GoRuntime(),  # new
]
```

No other files need to change: the extension validator and evaluator pick it up automatically.

### Adding a new executor

To support a different execution environment (e.g., Docker), you need two things:

1. Implement the backend in `custom_evaluators.py`:

```python
class DockerBackend(EvaluatorBackend):
    def __init__(self, path: Path, timeout: int = 30):
        self._path = path
        self._timeout = timeout

    async def run(self, eval_input: EvalInput, metric_name: str) -> EvalResult:
        # Build/run container, pipe JSON, return result
        ...
```

2. Register it:

```python
from agentevals.custom_evaluators import register_executor

register_executor("docker", lambda path, timeout: DockerBackend(path, timeout))
```

Users then set `executor: docker` in their config:

```yaml
evaluators:
  - name: untrusted_evaluator
    type: code
    path: ./evaluators/untrusted.py
    executor: docker
```

### Adding a new evaluator source

To support a different evaluator registry (e.g., a custom API), implement `EvaluatorSource`:

```python
from agentevals.evaluator.sources import EvaluatorSource, EvaluatorInfo, register_source


class MyRegistrySource(EvaluatorSource):
    @property
    def source_name(self) -> str:
        return "my-registry"

    async def list_evaluators(self) -> list[EvaluatorInfo]: ...
    async def fetch_evaluator(self, ref: str, dest: Path) -> Path: ...


register_source(MyRegistrySource())
```

Users can then reference evaluators from the new source:

```yaml
evaluators:
  - name: my_evaluator
    type: remote
    source: my-registry
    ref: some/ref/path.py
```
