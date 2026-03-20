# Contributing to agentevals

Thank you for your interest in contributing to agentevals! This document covers how to get started, contribute code, and get your changes merged.

> **Note:** This project is under active development. Expect breaking changes.

## Ways to Contribute

- **Report bugs or request features** — use [GitHub Issues](https://github.com/agentevals-dev/agentevals/issues). Search existing issues before opening a new one.
- **Fix a bug** — open a PR with a test that reproduces the issue.
- **Add a feature** — open an issue first to discuss the approach, then submit a PR.
- **Improve docs** — PRs for documentation fixes and improvements are always welcome.

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

### Workflow

1. Create a branch from `main`: `git checkout -b feature/my-change`
2. Make your changes
3. Add or update tests as needed
4. Run `uv run pytest` and ensure all tests pass
5. Commit with a clear message (see [Commit Messages](#commit-messages))
6. Push to your fork and open a PR against `main`

### Small Changes

For bug fixes or minor improvements (< 100 lines), open a PR directly with tests.

### Large Changes

For new features, refactors, or anything that touches multiple files:

1. **Open an issue** describing the change and your proposed approach
2. **Get alignment** before investing significant effort
3. **Open a draft PR** early to get feedback
4. Iterate based on review

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

agentevals converts OTel traces from agent frameworks into a common `Invocation` format for evaluation. If you're adding support for a new framework or changing how we extract data from spans, this section will help you find your way around.

### Key Modules

| Module | What it does |
|--------|--------------|
| `trace_attrs.py` | Single source of truth for OTel attribute key constants (`OTEL_GENAI_*` for standard semconv, `ADK_*` for Google ADK) |
| `extraction.py` | Shared extraction functions, span classifiers, and the `TraceFormatExtractor` protocol with `AdkExtractor` / `GenAIExtractor` |
| `converter.py` | Batch conversion orchestration, turns ADK traces into `Invocation` objects |
| `genai_converter.py` | Batch conversion for GenAI semconv traces (single-turn and multi-turn) |
| `streaming/incremental_processor.py` | Real-time span processing for the live UI, uses the same shared extraction functions |
| `utils/log_enrichment.py` | Reconstructs `gen_ai.input/output.messages` from OTel log records into span attributes |

### Adding a new attribute constant

Add it to `trace_attrs.py` and import from there. Don't use hardcoded attribute key strings elsewhere.

### Adding or modifying extraction logic

The extraction functions in `extraction.py` accept flat `dict[str, Any]` attribute maps. This means they work with both `Span`-based batch converters (via `span.tags`) and the raw OTLP dict incremental processor. When extracting data, check ADK-specific attributes first (they contain richer data), then fall back to GenAI semconv.

### Supporting a new trace format

1. Add a new `TraceFormatExtractor` implementation in `extraction.py` with `detect()`, `find_invocation_spans()`, `find_llm_spans_in()`, `find_tool_spans_in()`, and `classify_span()`
2. Register it in the `_EXTRACTORS` list. Order matters here: more specific formats should come first so they get detected before the generic GenAI fallback
3. If the format introduces new attribute keys, add them to `trace_attrs.py`
4. If you need conversion logic that the shared extraction functions don't cover, add a dedicated converter module (see `genai_converter.py` for an example)
5. Add tests to `tests/test_extraction.py` for detection and span classification

### Adding an SDK example

Each example directory under `examples/` is self-contained with its own `requirements.txt`. The example needs to actually produce OTel spans. For OpenAI-based agents this means including `opentelemetry-instrumentation-openai-v2` in the requirements. Make sure all framework-specific OTel dependencies are listed in the example's `requirements.txt`.

## Responsible AI Usage

We welcome contributors who use AI tools to assist their work, but we ask that you use them responsibly:

- **Do not generate issues, comments, or PR descriptions with AI.** These should be written by you in your own words. Maintainers need to communicate with the person behind the contribution, not a language model.
- **Do not "vibe code."** AI should assist and accelerate code that you (the human!) would write on your own. You are expected to understand every line of code you submit. If you cannot explain a change during review, it will not be merged.
- **Indicate non-trivial AI assistance.** If AI played a significant role in writing your code (beyond autocomplete or minor suggestions), mention it in your PR description. This helps reviewers calibrate their review.

## Getting Help

- Open an [issue](https://github.com/agentevals-dev/agentevals/issues) for bugs or questions
- Check [DEVELOPMENT.md](DEVELOPMENT.md) for detailed build and release instructions
