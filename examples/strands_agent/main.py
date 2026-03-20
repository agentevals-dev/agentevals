"""Strands agent with live streaming to agentevals.

This example demonstrates streaming traces from a Strands agent
to the agentevals dev server for real-time evaluation and visualization.

Key integration points:
1. StrandsTelemetry initializes the global TracerProvider with OTel tracing
2. AgentEvalsStreamingProcessor is added to that provider to stream spans via WebSocket
3. OTEL_SEMCONV_STABILITY_OPT_IN=gen_ai_latest_experimental enables structured message
   events with gen_ai.input.messages / gen_ai.output.messages attributes, which the
   streaming processor promotes from span events to span attributes

Prerequisites:
    1. Install dependencies:
       $ pip install -r requirements.txt

    2. Start agentevals dev server:
       $ agentevals serve --dev --port 8001

    3. (Optional) Start UI for visualization:
       $ cd ui && npm run dev

    4. Set OpenAI API key:
       $ export OPENAI_API_KEY="your-key-here"

Usage:
    $ python examples/strands_agent/main.py

View live results at http://localhost:5173
"""

import asyncio
import os
import threading

from agent import create_dice_agent
from dotenv import load_dotenv
from strands.telemetry import StrandsTelemetry

from agentevals.streaming.processor import AgentEvalsStreamingProcessor

load_dotenv(override=True)

os.environ.setdefault("OTEL_SEMCONV_STABILITY_OPT_IN", "gen_ai_latest_experimental")


def setup_otel_streaming(ws_url: str, session_id: str, eval_set_id: str | None = None):
    """Configure OTel for streaming Strands traces to agentevals.

    StrandsTelemetry() creates and sets the global TracerProvider. Strands agents
    pick up the global provider automatically. We attach AgentEvalsStreamingProcessor
    to that same provider so all agent spans are streamed to the dev server.

    Returns:
        tuple: (telemetry, processor, event_loop)
    """
    telemetry = StrandsTelemetry()

    processor = AgentEvalsStreamingProcessor(
        ws_url=ws_url,
        session_id=session_id,
        trace_id=os.urandom(16).hex(),
    )

    loop = asyncio.new_event_loop()

    def run_loop_in_background():
        asyncio.set_event_loop(loop)
        loop.run_forever()

    thread = threading.Thread(target=run_loop_in_background, daemon=True)
    thread.start()

    future = asyncio.run_coroutine_threadsafe(processor.connect(eval_set_id=eval_set_id), loop)
    future.result()

    telemetry.tracer_provider.add_span_processor(processor)

    return telemetry, processor, loop


def main():
    if not os.getenv("OPENAI_API_KEY"):
        print("⚠️  OPENAI_API_KEY not set. Set it with:")
        print("   export OPENAI_API_KEY='your-key-here'")
        return

    session_id = f"strands-session-{os.urandom(4).hex()}"

    print("Setting up OpenTelemetry streaming...")
    telemetry, processor, loop = setup_otel_streaming(
        ws_url="ws://localhost:8001/ws/traces",
        session_id=session_id,
        eval_set_id="strands_agent_eval",
    )

    print("✓ Connected to agentevals dev server")
    print(f"  Session: {session_id}")
    print("  View live: http://localhost:5173")
    print()

    print("🎲 Strands Dice Agent - Live Dev Mode")
    print("=" * 50)
    print()

    agent = create_dice_agent()

    test_queries = [
        "Hi! Can you help me?",
        "Roll a 20-sided die for me",
        "Is the number you rolled prime?",
    ]

    for i, query in enumerate(test_queries, 1):
        print(f"\n[{i}/{len(test_queries)}] User: {query}")
        result = agent(query)
        print(f"     Agent: {result}")

    print()
    print("✓ Agent execution complete")
    print()

    telemetry.tracer_provider.force_flush()
    print("✓ All traces flushed to server")

    future = asyncio.run_coroutine_threadsafe(processor.shutdown_async(), loop)
    future.result()
    print("✓ Session ended")


if __name__ == "__main__":
    main()
