"""Quick check that this machine can stream to the agentevals dev server.

Opens an AgentEvals session, records one span inside it and closes the session, which flushes
the span to the OTLP receiver. The session then shows up in the UI.

Prerequisites:
    $ agentevals serve --dev

Usage:
    $ python examples/dice_agent/test_streaming.py
"""

import asyncio

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider

from agentevals import AgentEvals


async def test_streaming():
    provider = TracerProvider()
    trace.set_tracer_provider(provider)
    app = AgentEvals(auto_instrument=False)

    try:
        async with app.session_async(
            eval_set_id="test-eval",
            session_name="streaming-check",
            metadata={"test": True},
            tracer_provider=provider,
        ):
            print("✓ Reached the OTLP receiver")
            with provider.get_tracer("streaming-check").start_as_current_span("test_span"):
                print("✓ Created test span")
        print("✓ Session flushed")
        print()
        print("Streaming works. Look for the 'streaming-check' session in the UI.")
    except ConnectionError as exc:
        print(f"❌ {exc}")


if __name__ == "__main__":
    asyncio.run(test_streaming())
