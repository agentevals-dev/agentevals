"""Strands agent with live streaming to agentevals.

This example demonstrates streaming traces from a Strands agent
to the agentevals dev server for real-time evaluation and visualization.

Key integration points:
1. StrandsTelemetry initializes the global TracerProvider with OTel tracing
2. An AgentEvals session on that provider exports the spans produced inside it over OTLP
3. OTEL_SEMCONV_STABILITY_OPT_IN=gen_ai_latest_experimental enables structured message
   events with gen_ai.input.messages / gen_ai.output.messages attributes, which agentevals
   reads from the span events

Note: Strands currently delivers message content via span events. The OTel community
is deprecating span events in favor of log-based events (see
https://opentelemetry.io/blog/2026/deprecating-span-events/). agentevals reads both forms,
so this example keeps working when Strands moves to log based events.

Prerequisites:
    1. Install dependencies:
       $ pip install -r requirements.txt

    2. Start agentevals dev server:
       $ agentevals serve --dev

    3. (Optional) Start UI for visualization:
       $ cd ui && npm run dev

    4. Set OpenAI API key:
       $ export OPENAI_API_KEY="your-key-here"

Usage:
    $ python examples/strands_agent/main.py

View live results at http://localhost:5173
"""

import os

from agent import create_dice_agent
from dotenv import load_dotenv
from strands.telemetry import StrandsTelemetry

from agentevals import AgentEvals

load_dotenv(override=True)

os.environ.setdefault("OTEL_SEMCONV_STABILITY_OPT_IN", "gen_ai_latest_experimental")


def main():
    if not os.getenv("OPENAI_API_KEY"):
        print("⚠️  OPENAI_API_KEY not set. Set it with:")
        print("   export OPENAI_API_KEY='your-key-here'")
        return

    telemetry = StrandsTelemetry()
    session_name = f"strands-session-{os.urandom(4).hex()}"
    app = AgentEvals(eval_set_id="strands_agent_eval", auto_instrument=False)

    with app.session(session_name=session_name, tracer_provider=telemetry.tracer_provider):
        print("✓ Streaming to the agentevals dev server")
        print(f"  Session: {session_name}")
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
    print("✓ Agent execution complete, session flushed to the server")


if __name__ == "__main__":
    main()
