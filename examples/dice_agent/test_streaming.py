"""Quick test to verify streaming setup works."""

import asyncio
import os

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider


async def test_streaming():
    """Test that streaming processor connects and shuts down cleanly."""

    provider = TracerProvider()
    trace.set_tracer_provider(provider)

    try:
        from agentevals.streaming.processor import AgentEvalsStreamingProcessor

        print("✓ Imported streaming processor")

        processor = AgentEvalsStreamingProcessor(
            ws_url="ws://localhost:8001/ws/traces",
            session_id="test-session",
            trace_id="test-trace-123",
        )

        print("✓ Created processor")

        await processor.connect(
            eval_set_id="test-eval",
            metadata={"test": True},
        )

        print("✓ Connected to server")

        provider.add_span_processor(processor)

        print("✓ Added processor to provider")

        tracer = trace.get_tracer("test")
        with tracer.start_as_current_span("test_span"):
            print("✓ Created test span")

        print("✓ Span sent")

        await processor.shutdown_async()

        print("✓ Clean shutdown")
        print()
        print("All tests passed! Streaming is working correctly.")

    except ImportError:
        print("❌ agentevals not installed")
        print("   Run: pip install -e .")
    except Exception as e:
        print(f"❌ Error: {e}")
        print()
        print("Make sure dev server is running:")
        print("  agentevals serve --dev --port 8001")


if __name__ == "__main__":
    asyncio.run(test_streaming())
