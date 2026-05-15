# Eval Set Format

An eval set is a JSON file containing golden reference data that evaluators compare agent traces against. It follows the [Google ADK `EvalSet`](https://github.com/google/adk-python/blob/main/src/google/adk/evaluation/eval_set.py) schema, which means eval sets are portable between agentevals and ADK tooling.

Most users will not need to author eval sets by hand. The web UI can generate them from live sessions (mark a session as golden, and the server builds the eval set automatically). This document is for users who want to create or edit eval sets directly, whether for CLI usage, CI pipelines, or version-controlled test suites.

## Structure Overview

```
EvalSet
├── eval_set_id        (required, string)
├── name               (optional, string)
├── description         (optional, string)
├── eval_cases          (required, list of EvalCase)
│   └── EvalCase
│       ├── eval_id             (required, string)
│       ├── conversation        (list of Invocation)
│       │   └── Invocation
│       │       ├── invocation_id       (string)
│       │       ├── user_content        (Content: role + parts)
│       │       ├── final_response      (Content, optional)
│       │       └── intermediate_data   (optional)
│       │           ├── tool_uses       (list of FunctionCall)
│       │           └── tool_responses  (list of FunctionResponse)
│       ├── rubrics             (optional, list of Rubric)
│       └── session_input       (optional)
└── creation_timestamp  (optional, float)
```

## Minimal Example

A single eval case with one user turn and an expected response:

```json
{
  "eval_set_id": "my-agent-eval",
  "eval_cases": [
    {
      "eval_id": "greeting",
      "conversation": [
        {
          "invocation_id": "inv-1",
          "user_content": {
            "role": "user",
            "parts": [{"text": "Hi! Can you help me?"}]
          },
          "final_response": {
            "role": "model",
            "parts": [{"text": "Hello! I can help you roll dice and check prime numbers."}]
          }
        }
      ]
    }
  ]
}
```

## Example with Tool Calls

When your agent uses tools, capture the expected tool trajectory in `intermediate_data`:

```json
{
  "eval_set_id": "helm_eval_set",
  "name": "Helm Agent Eval Set",
  "description": "Golden eval cases for the Helm agent.",
  "eval_cases": [
    {
      "eval_id": "helm_list_releases",
      "conversation": [
        {
          "invocation_id": "inv-1",
          "user_content": {
            "role": "user",
            "parts": [{"text": "list all Helm releases"}]
          },
          "final_response": {
            "role": "model",
            "parts": [{"text": "There are two Helm releases installed in the cluster..."}]
          },
          "intermediate_data": {
            "tool_uses": [
              {
                "name": "helm_list_releases",
                "args": {},
                "id": "call_1"
              }
            ],
            "tool_responses": [
              {
                "name": "helm_list_releases",
                "response": {
                  "content": [{"type": "text", "text": "NAME  NAMESPACE  STATUS ..."}],
                  "isError": false
                },
                "id": "call_1"
              }
            ]
          }
        }
      ]
    }
  ]
}
```

## Multi-turn Conversations

An eval case can have multiple invocations to represent a conversation. Each invocation is one user turn plus the agent's expected response:

```json
{
  "eval_set_id": "multi_turn_eval",
  "eval_cases": [
    {
      "eval_id": "roll_and_check",
      "conversation": [
        {
          "invocation_id": "inv-1",
          "user_content": {"role": "user", "parts": [{"text": "Roll a 20-sided die"}]},
          "final_response": {"role": "model", "parts": [{"text": "I rolled a 17!"}]},
          "intermediate_data": {
            "tool_uses": [{"name": "roll_die", "args": {"sides": 20}, "id": "c1"}],
            "tool_responses": [{"name": "roll_die", "response": {"result": 17}, "id": "c1"}]
          }
        },
        {
          "invocation_id": "inv-2",
          "user_content": {"role": "user", "parts": [{"text": "Is that number prime?"}]},
          "final_response": {"role": "model", "parts": [{"text": "Yes, 17 is a prime number."}]},
          "intermediate_data": {
            "tool_uses": [{"name": "check_prime", "args": {"nums": [17]}, "id": "c2"}],
            "tool_responses": [{"name": "check_prime", "response": {"17": true}, "id": "c2"}]
          }
        }
      ]
    }
  ]
}
```

## Field Reference

### EvalSet (top level)

| Field | Type | Required | Description |
|---|---|---|---|
| `eval_set_id` | string | yes | Unique identifier for this eval set |
| `name` | string | no | Human readable name |
| `description` | string | no | What this eval set covers |
| `eval_cases` | list[EvalCase] | yes | The evaluation cases |
| `creation_timestamp` | float | no | Unix timestamp, defaults to 0.0 |

### EvalCase

| Field | Type | Required | Description |
|---|---|---|---|
| `eval_id` | string | yes | Unique identifier for this case |
| `conversation` | list[Invocation] | yes* | Static conversation turns |
| `conversation_scenario` | ConversationScenario | no* | For simulated agent evaluation (ADK feature, not used by agentevals) |
| `session_input` | SessionInput | no | Initial session state for the agent |
| `rubrics` | list[Rubric] | no | Scoring rubrics for all invocations in this case |
| `final_session_state` | dict | no | Expected session state after the conversation |
| `creation_timestamp` | float | no | Unix timestamp |

*Exactly one of `conversation` or `conversation_scenario` must be provided. For agentevals, use `conversation`.

### Invocation

| Field | Type | Required | Description |
|---|---|---|---|
| `invocation_id` | string | no | Unique turn identifier (defaults to empty string) |
| `user_content` | Content | yes | What the user said |
| `final_response` | Content | no | Expected agent response |
| `intermediate_data` | IntermediateData | no | Expected tool calls and responses |
| `rubrics` | list[Rubric] | no | Scoring rubrics for this specific invocation |
| `creation_timestamp` | float | no | Unix timestamp |

### Content

Uses the Google GenAI `Content` format:

```json
{
  "role": "user" or "model",
  "parts": [
    {"text": "plain text content"},
    {"function_call": {"name": "tool_name", "args": {...}}},
    {"function_response": {"name": "tool_name", "response": {...}}}
  ]
}
```

The `parts` array can contain text, function calls, or function responses. Most commonly you will use text parts in `user_content` and `final_response`.

### IntermediateData

| Field | Type | Default | Description |
|---|---|---|---|
| `tool_uses` | list[FunctionCall] | `[]` | Tool calls the agent made, in chronological order |
| `tool_responses` | list[FunctionResponse] | `[]` | Tool responses received, in chronological order |
| `intermediate_responses` | list[tuple] | `[]` | Sub-agent responses (multi-agent systems) |

Each `FunctionCall` has `name`, `args`, and `id`. Each `FunctionResponse` has `name`, `response`, and `id`. Match `id` values between calls and responses to pair them.

## Which Evaluators Use Eval Sets

Not all evaluators require an eval set. Use `agentevals evaluator list --source builtin` to see which built-in evaluators do:

| Metric | Needs Eval Set | What It Reads |
|---|---|---|
| `tool_trajectory_avg_score` | yes | `intermediate_data.tool_uses` |
| `response_match_score` | yes | `final_response` (ROUGE-1 text similarity) |
| `final_response_match_v2` | yes | `final_response` (LLM judge comparison) |
| `response_evaluation_score` | yes | `final_response` (Vertex AI semantic eval) |
| `hallucinations_v1` | no | N/A |
| `safety_v1` | no | N/A |

## Usage

### CLI

```bash
agentevals run trace.json --eval-set eval_set.json -m tool_trajectory_avg_score
```

### Web UI

Upload an eval set file in the evaluation panel, or let the UI generate one from a golden session.

### API

```bash
curl -X POST http://localhost:8001/api/validate/eval-set \
  -F "eval_set_file=@eval_set.json"
```

The validation endpoint checks JSON syntax, required fields, and structural correctness before you run an evaluation.

## ADK Compatibility

The eval set format is defined by [Google ADK's evaluation module](https://github.com/google/adk-python/tree/main/src/google/adk/evaluation). agentevals loads eval sets using `EvalSet.model_validate()` from ADK directly, so any valid ADK eval set works with agentevals and vice versa.

Fields specific to ADK's live evaluation flow (`conversation_scenario`, `session_input`, `final_session_state`) are accepted but not used by agentevals, which evaluates pre-recorded traces rather than running agents live.
