"""Quick test to verify streaming connectivity with agentevals dev server.

This minimal script tests:
1. WebSocket connection to dev server
2. Basic OTEL span transmission
3. Session creation and shutdown

Prerequisites:
    $ agentevals serve --dev --port 8001

Usage:
    $ python examples/langchain_agent/test_streaming.py
"""

import asyncio
import os
from datetime import datetime
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider


async def test_connection():
    print("🔍 Testing connection to agentevals dev server...")
    print()

    provider = TracerProvider()
    trace.set_tracer_provider(provider)

    try:
        from agentevals.streaming.processor import AgentEvalsStreamingProcessor

        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:21]
        session_id = f"test-langchain-{timestamp}"

        processor = AgentEvalsStreamingProcessor(
            ws_url="ws://localhost:8001/ws/traces",
            session_id=session_id,
            trace_id="test-" + os.urandom(8).hex(),
        )

        print("Connecting...")
        await processor.connect(
            eval_set_id="test_eval",
            metadata={"test": True},
        )

        provider.add_span_processor(processor)

        print(f"✓ Connected successfully!")
        print(f"  Session ID: {session_id}")
        print()

        tracer = trace.get_tracer(__name__)
        with tracer.start_as_current_span("test_span") as span:
            span.set_attribute("test.attribute", "hello")
            print("✓ Created test span")

        await processor.shutdown_async()
        print("✓ Shutdown complete")
        print()
        print("Connection test PASSED ✓")

    except ImportError:
        print("❌ agentevals not installed")
        print("   Install with: pip install -e ../..")
        print()

    except Exception as e:
        print(f"❌ Connection test FAILED")
        print(f"   Error: {e}")
        print()
        print("Make sure agentevals dev server is running:")
        print("  $ agentevals serve --dev --port 8001")
        print()


if __name__ == "__main__":
    asyncio.run(test_connection())
