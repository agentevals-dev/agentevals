# Development Guide

## Distribution tiers

agentevals ships as two distribution variants from a single codebase:

| Tier | Install | What you get |
|------|---------|--------------|
| **Core** | `pip install agentevals` | CLI + REST API + live mode (WebSocket streaming, sessions, SSE) |
| **Bundle** | `pip install agentevals` (bundled wheel) | Everything in Core + embedded React UI |

Live mode (WebSocket streaming, session management, SSE) is always enabled when running `agentevals serve`. The `--dev` flag adds hot reload and dev-friendly console output but does not change what features are active.

The optional `[live]` extra (`pip install "agentevals[live]"`) adds `mcp` and `httpx`, which are only needed for the MCP server (`agentevals mcp`). The bundled wheel is built with `make build-bundle` and includes compiled UI assets baked into the package.

## Makefile

### Development

```bash
make dev-backend       # start FastAPI in live mode (port 8001), reload on source changes
make dev-frontend      # start Vite dev server (port 5173) with HMR
make dev-bundle        # build UI, serve full bundled experience at port 8001 via uv run
```

Standard development uses `dev-backend` + `dev-frontend` in separate terminals. The Vite dev server proxies nothing — the frontend calls the backend at `http://localhost:8001` directly via CORS.

`dev-bundle` is useful for testing the bundled UI experience without building a wheel. It copies `ui/dist` into the source tree temporarily and cleans up when the server exits.

### Building

```bash
make build             # build core wheel → dist/agentevals-*.whl
make build-bundle      # build UI, embed into wheel, clean up → dist/agentevals-*.whl
make build-ui          # build React app only → ui/dist/
```

Both `build` and `build-bundle` produce `dist/agentevals-*.whl` with the same package name and version. The difference is that `build-bundle` embeds `ui/dist/` as `agentevals/_static/` inside the wheel. The hatchling `artifacts` config ensures the gitignored `_static/` directory is included.

### Testing

```bash
make test              # run all tests (unit + integration, excludes e2e)
make test-unit         # unit tests only (fast, no server startup)
make test-integration  # integration tests — OTLP pipeline, session grouping, timing (no API keys)
make test-e2e          # E2E tests — real agents as subprocesses (requires OPENAI_API_KEY and/or GOOGLE_API_KEY)
```

### Cleanup

```bash
make clean             # remove dist/, build/, ui/dist/, src/agentevals/_static/
```

## Testing

### Test tiers

Tests are organized into three tiers with different trade-offs:

| Tier | Location | Transport | API keys | What it verifies |
|------|----------|-----------|----------|------------------|
| **Unit** | `tests/` (excl. integration) | `TestClient` / mocks | None | Business logic, route handlers, converters |
| **Integration** | `tests/integration/` | ASGI in-process | None | OTLP session grouping, timing, concurrent batches, eval pipeline |
| **E2E** | `tests/integration/test_live_agents.py` | Real uvicorn servers | `OPENAI_API_KEY`, `GOOGLE_API_KEY` | Full pipeline — real agent → OTLP export → session creation → invocation extraction → API visibility |

Integration tests use `httpx.ASGITransport` to hit the OTLP and streaming API routes in-process (no ports, no real HTTP). Timers are configured fast (0.1s grace, 0.5s idle) for quick deterministic tests.

E2E tests start real uvicorn servers on ephemeral ports in a background thread, then run example agent scripts as subprocesses that emit real OTLP traces with `BatchSpanProcessor`/`BatchLogRecordProcessor` flush timing.

### Running E2E tests

E2E tests require `OPENAI_API_KEY` (LangChain and Strands agents) and/or `GOOGLE_API_KEY` (ADK agents). Each test class is skipped automatically when its required key is not set.

```bash
# Source your .env and run
set -a && source .env && set +a && make test-e2e
```

### Adding tests for new examples

When adding a new example agent to `examples/`, add corresponding E2E tests to ensure the full OTLP pipeline works:

1. Add a test class in `tests/integration/test_live_agents.py` following the existing pattern (`TestLangchainZeroCode`, `TestStrandsZeroCode`, `TestAdkZeroCode`)
2. Each agent should have at minimum three tests:
   - **Session creation** — agent runs successfully, session is created with spans (and logs if applicable)
   - **Invocation extraction** — invocations are extracted with user/agent content
   - **API visibility** — session appears in `GET /api/streaming/sessions`
3. Use `_run_agent()` to run the example as a subprocess with the test OTLP endpoint
4. Use `wait_for_session_complete_sync()` to poll until the session finalizes
5. Mark the test class with the appropriate skip condition (e.g., `_skip_no_openai`)
6. Use unique `session_name` values per test to avoid collisions within the session-scoped server fixture

## Runtime behavior

The serve command always enables live mode (WebSocket, streaming, sessions). The flags control UI serving and reload behavior:

- `agentevals serve` — live mode + REST API; UI served if bundled `_static/` is present
- `agentevals serve --dev` — same as above + hot reload on source changes + dev console output
- `agentevals serve --headless` — live mode + REST API, UI suppressed even if bundled

Controlled by environment variables `AGENTEVALS_LIVE=1` (always set by the CLI) and `AGENTEVALS_HEADLESS=1` (set when `--headless` is passed).

## NixOS / Nix devshell

The project provides a `flake.nix` devshell. Inside the Nix environment, `agentevals` in PATH points to the Nix store derivation (immutable). Use `uv run` to run from the live source tree:

```bash
uv run agentevals serve --dev    # live source, dev mode
make dev-bundle                   # live source, bundled UI test
```

To release a new Nix derivation, update `flake.nix` with the new version and rebuild.

## Releasing

1. Bump `version` in `pyproject.toml`
2. Commit and push the change
3. Tag and push — this triggers the release workflow automatically:
   ```bash
   git tag v0.1.0
   git push origin v0.1.0
   ```
4. Alternatively, trigger manually from **GitHub → Actions → Release → Run workflow** and enter the tag

The workflow (`.github/workflows/release.yml`) runs `make release`, which builds the wheel twice (once without UI, once with embedded UI) into separate subdirectories:

```
dist/core/agentevals-<version>-py3-none-any.whl    # CLI + REST API
dist/bundle/agentevals-<version>-py3-none-any.whl  # CLI + REST API + streaming + embedded UI
```

Both wheels use the same standard filename (valid per PEP 427). They are attached as separate release assets to the GitHub Release. Users download the appropriate wheel:

```bash
pip install agentevals-<version>-py3-none-any.whl
```

To also use the MCP server (`agentevals mcp`), install with the `[live]` extra:

```bash
pip install "agentevals-<version>-py3-none-any.whl[live]"
```
