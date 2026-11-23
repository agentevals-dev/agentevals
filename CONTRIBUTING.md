# Contributing to agentevals

Thank you for your interest in contributing to agentevals! This document covers how to get started, contribute code, and get your changes merged.

> **Note:** This project is under active development. Expect breaking changes.

## Ways to Contribute

- **Report bugs or request features** — use [GitHub Issues](https://github.com/agentevals-dev/agentevals/issues). Search existing issues before opening a new one.
- **Fix a bug or add a feature**: open an issue first and wait for a maintainer to agree on the approach. See [Issue first](#issue-first).
- **Fix a typo or broken link**: open a PR directly.

## Development Setup

### Prerequisites

- Python 3.11+
- [uv](https://docs.astral.sh/uv/) (Python package manager)
- Node.js 20+ and npm (for the UI)
- Optionally, [Nix](https://nixos.org/) — the project includes a `flake.nix` devshell

### Getting Started

```bash
# Fork and clone
git clone https://github.com/YOUR_USERNAME/agentevals.git
cd agentevals
git remote add upstream https://github.com/agentevals-dev/agentevals.git

# Install Python dependencies
uv sync

# Install UI dependencies
cd ui && npm ci && cd ..
```

### Running Locally

Start the backend and frontend in separate terminals:

```bash
# Terminal 1 — backend (FastAPI, port 8001)
make dev-backend

# Terminal 2 — frontend (Vite, port 5173)
make dev-frontend
```

Open http://localhost:5173 to access the UI.

To test the full bundled experience (UI embedded in the backend):

```bash
make dev-bundle
```

See [DEVELOPMENT.md](DEVELOPMENT.md) for build tiers, Makefile targets, and release instructions.

### Running Tests

```bash
make test
# or directly:
uv run pytest
```

## Contributing Code

### Issue first

Every PR needs a linked issue where a maintainer has agreed to the change. This applies to bug fixes too, no matter how small. The only exception is typo and broken link fixes.

1. Open an issue describing the problem and the change you want to make
2. Wait for a maintainer to confirm the approach in the issue
3. Open a PR that links the issue (`Fixes #123`)

PRs without an agreed issue are closed without review. This is not a judgement on the code: it keeps maintainer time for changes the project has agreed to take.

### Workflow

1. Create a branch from `main`: `git checkout -b feature/my-change`
2. Make your changes
3. Add or update tests as needed
4. Run `uv run pytest` and ensure all tests pass
5. Commit with a clear message (see [Commit Messages](#commit-messages))
6. Push to your fork and open a PR against `main`

For larger changes, open a draft PR early once the issue is agreed, so the approach can be reviewed before it is finished.

## Code Style

### Python

- Use type hints for function signatures
- Keep functions focused and small

### TypeScript / React

- Follow the project's existing patterns: inline styles, CSS variables, Ant Design components
- Use TypeScript for type safety
- Functional components with hooks

## Commit Messages

Follow [Conventional Commits](https://www.conventionalcommits.org/):

```
type(scope): subject

body (optional)
```

Types: `feat`, `fix`, `docs`, `refactor`, `test`, `chore`

Examples:

```
feat(cli): add --threshold flag to run command
fix(ui): correct metric score display in eval table
docs: update development setup instructions
test: add coverage for OTLP trace parsing
```

## Pull Request Process

1. Ensure tests pass
2. Update documentation if your change affects user-facing behavior
3. Keep PRs focused — one logical change per PR
4. Request a review from a maintainer

### PR Checklist

The [PR template](.github/pull_request_template.md) includes this checklist.

- [ ] Links an issue a maintainer agreed to
- [ ] Tests added or updated
- [ ] All tests pass (`uv run pytest`)
- [ ] Documentation updated (if applicable)
- [ ] Commits are clean with meaningful messages

## Project Structure

```
src/agentevals/       # Python backend (FastAPI, CLI, evaluation engine)
ui/src/               # React frontend (Vite, Ant Design, TypeScript)
tests/                # Python tests (pytest)
examples/             # Agent examples (zero-code, SDK, custom evaluators)
samples/              # Example traces and eval sets
docs/                 # Documentation
```

## Trace Processing Architecture

Telemetry flows through four layers. Each is plain Python with no ADK dependency except the bridge.

| Module | What it does |
|--------|--------------|
| `otel/model.py`, `otel/decode.py`, `otel/encode.py` | Lossless OTLP envelopes (spans, logs, resources, scopes) decoded from protobuf, JSON or Jaeger files, and encoded back |
| `genai/overlay.py`, `genai/semconv.py` | Read only view of a span in current GenAI conventions, with fallbacks for deprecated keys, events, ADK and OpenLLMetry attributes |
| `genai/extract.py`, `genai/model.py` | Turns, logical model calls and tool calls extracted from traces (`extract_conversation`) |
| `genai/matching.py`, `genai/grouping.py` | Grouping traces into conversations and matching them to eval cases |
| `adk_bridge.py` | The only place that converts to ADK types (eval sets, built in metrics) |
| `otel/store.py`, `streaming/manager.py` | Live sessions: routing mechanics, limits, completion, recompute and UI updates |
| `genai/routing.py` | What the live store routes on: session keys, which traces and logs to keep, agentevals' own records |
| `evaluation_events.py` | Evaluation results as `gen_ai.evaluation.result` events |

Imports point down only: `otel/` imports nothing from `genai/`, and neither imports the runner, the evaluators or `evaluation_events.py`.

### Supporting a new producer

1. Record a few real traces and add them as fixtures with the turns you expect.
2. If the producer uses standard GenAI attributes, it should already work. Check with `agentevals run`.
3. If it uses its own attribute names, add a fallback in `genai/overlay.py`. Never change span classification for one producer; it keys on `gen_ai.operation.name`.

### Adding an SDK example

Each example directory under `examples/` is self-contained with its own `requirements.txt`. The example needs to actually produce OTel spans. For OpenAI-based agents this means including `opentelemetry-instrumentation-openai-v2` in the requirements. Make sure all framework-specific OTel dependencies are listed in the example's `requirements.txt`.

## Responsible AI Usage

We welcome contributors who use AI tools to assist their work, but we ask that you use them responsibly:

- **Do not generate issues, comments, or PR descriptions with AI.** These should be written by you in your own words. Maintainers need to communicate with the person behind the contribution, not a language model.
- **Do not "vibe code."** AI should assist and accelerate code that you (the human!) would write on your own. You are expected to understand every line of code you submit. If you cannot explain a change during review, it will not be merged.
- **Indicate non-trivial AI assistance.** If AI played a significant role in writing your code (beyond autocomplete or minor suggestions), mention it in your PR description. This helps reviewers calibrate their review.
- **Do not let agents open issues or PRs for you.** Autonomous agents and bots opening issues or PRs will be closed and may be blocked. Instructions for coding agents are in [AGENTS.md](AGENTS.md).

## Getting Help

- Open an [issue](https://github.com/agentevals-dev/agentevals/issues) for bugs or questions
- Check [DEVELOPMENT.md](DEVELOPMENT.md) for detailed build and release instructions
