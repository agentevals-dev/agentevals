# Custom Graders

Custom graders let you score agent traces with your own logic. A grader is any program that reads `EvalInput` JSON from stdin and writes `EvalResult` JSON to stdout. This simple protocol means you can write graders in Python, JavaScript/TypeScript, or any language that can read/write JSON.

## Quick Start (Python)

### 1. Install the SDK

```bash
pip install agentevals-grader-sdk
```

### 2. Write a grader

```python
# graders/response_quality.py
from agentevals_grader_sdk import grader, EvalInput, EvalResult

@grader
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
```

The `@grader` decorator handles all the stdin/stdout plumbing. Your function receives an `EvalInput` and returns an `EvalResult`.

### 3. Add it to your eval config

```yaml
# eval_config.yaml
metrics:
  - tool_trajectory_avg_score   # built-in metric

  - name: response_quality      # your custom grader
    type: code
    path: ./graders/response_quality.py
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

Each custom grader entry in the `metrics` list uses the following fields:

| Field | Required | Default | Description |
|---|---|---|---|
| `name` | yes | | Unique name for the grader (used in output) |
| `type` | yes | | `code` for local code files |
| `path` | yes | | Path to the grader file (`.py`, `.js`, or `.ts`) |
| `threshold` | no | `0.5` | Score at or above this value means PASSED |
| `timeout` | no | `30` | Subprocess timeout in seconds |
| `config` | no | `{}` | Arbitrary key-value pairs passed to the grader |

## Protocol

Every grader — regardless of language — communicates via the same JSON protocol over stdin/stdout.

### Input (`EvalInput`)

```json
{
  "metric_name": "response_quality",
  "threshold": 0.7,
  "config": { "min_length": 20 },
  "invocations": [
    {
      "invocation_id": "inv-001",
      "user_content": "What is 2+2?",
      "final_response": "The answer is 4.",
      "tool_calls": [
        { "name": "calculator", "args": { "expression": "2+2" } }
      ],
      "tool_responses": [
        { "name": "calculator", "output": "4" }
      ]
    }
  ],
  "expected_invocations": null
}
```

| Field | Type | Description |
|---|---|---|
| `metric_name` | string | Name of this grader |
| `threshold` | float | Pass/fail threshold |
| `config` | object | User-provided config from the YAML |
| `invocations` | array | Agent turns to evaluate |
| `expected_invocations` | array or null | Golden reference turns (from eval set) |

Each invocation contains:

| Field | Type | Description |
|---|---|---|
| `invocation_id` | string | Unique turn identifier |
| `user_content` | string | What the user said |
| `final_response` | string or null | The agent's final response |
| `tool_calls` | array | Tools the agent called |
| `tool_responses` | array | Responses the agent received from tools |

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

## Writing Graders in Other Languages

You don't need the Python SDK. Any program that reads JSON from stdin and writes JSON to stdout works.

### JavaScript / TypeScript

```javascript
// graders/tool_check.js
const input = JSON.parse(require("fs").readFileSync("/dev/stdin", "utf8"));

let score = 1.0;
for (const inv of input.invocations) {
  if (inv.tool_calls.length === 0) {
    score -= 0.5;
  }
}

console.log(JSON.stringify({
  score: Math.max(0, score),
  per_invocation_scores: [],
}));
```

```yaml
- name: tool_check
  type: code
  path: ./graders/tool_check.js
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

## Architecture

Custom graders use a layered architecture designed for extensibility.

```
┌─────────────────────────────────────────┐
│  Eval Config (YAML)                     │
│  type: code, path: ./grader.py          │
└──────────────┬──────────────────────────┘
               │
               ▼
┌─────────────────────────────────────────┐
│  CustomGraderEvaluator                  │
│  ADK Evaluator adapter                  │
│  Invocation ↔ EvalInput/EvalResult      │
└──────────────┬──────────────────────────┘
               │
               ▼
┌─────────────────────────────────────────┐
│  GraderBackend (ABC)                    │
│  async run(EvalInput) → EvalResult      │
├─────────────────────────────────────────┤
│  SubprocessBackend                      │
│  Runs a local file via Runtime registry │
└──────────────┬──────────────────────────┘
               │
               ▼
┌─────────────────────────────────────────┐
│  Runtime registry                       │
│  PythonRuntime (.py)                    │
│  NodeRuntime (.js, .ts)                 │
└─────────────────────────────────────────┘
```

- **`GraderBackend`** is the primary abstraction. Any execution strategy that can accept `EvalInput` and return `EvalResult` fits here.
- **`SubprocessBackend`** is the current implementation. It runs a local file as a child process, piping JSON over stdin/stdout.
- **`Runtime`** is an internal detail of `SubprocessBackend` that maps file extensions to interpreter commands.
- **`CustomGraderEvaluator`** adapts any `GraderBackend` into ADK's `Evaluator` interface, handling the conversion between ADK's `Invocation` objects and the simpler `EvalInput`/`EvalResult` protocol.

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
    GoRuntime(),       # new
]
```

No other files need to change — the extension validator and evaluator pick it up automatically.

### Adding a new backend type

To support a different transport (e.g., HTTP, Docker), you would:

1. Add a config model in `config.py`:

```python
class HttpGraderDef(BaseModel):
    name: str
    type: Literal["http"] = "http"
    url: str
    threshold: float = 0.5
    timeout: int = 30
    headers: dict[str, str] = Field(default_factory=dict)
    config: dict[str, Any] = Field(default_factory=dict)
```

2. Add it to the union:

```python
CustomGraderDef = Annotated[
    Union[BuiltinMetricDef, CodeGraderDef, HttpGraderDef],
    Field(discriminator="type"),
]
```

3. Implement the backend in `custom_evaluators.py`:

```python
class HttpBackend(GraderBackend):
    def __init__(self, url: str, timeout: int, headers: dict[str, str] | None = None):
        self._url = url
        self._timeout = timeout
        self._headers = headers or {}

    async def run(self, eval_input: EvalInput, metric_name: str) -> EvalResult:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                self._url,
                json=eval_input.model_dump(),
                headers=self._headers,
                timeout=self._timeout,
            )
            resp.raise_for_status()
            return EvalResult.model_validate(resp.json())
```

4. Wire it up in the factory in `runner.py`:

```python
elif isinstance(grader_def, HttpGraderDef):
    backend = HttpBackend(grader_def.url, grader_def.timeout, grader_def.headers)
```

5. Add the type to `_TYPE_TO_MODEL` in `eval_config_loader.py` and `routes.py`.

Users would then write:

```yaml
metrics:
  - name: latency_check
    type: http
    url: https://grader.example.com/evaluate
    threshold: 0.8
```
