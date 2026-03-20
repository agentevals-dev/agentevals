"""Run the Strands dice agent with standard OTLP export — no agentevals SDK.

Demonstrates zero-code integration: any OTel-instrumented agent streams
traces to agentevals by pointing the OTLP exporter at the receiver.

The only change vs. the original strands_agent example is replacing
AgentEvalsStreamingProcessor with a standard OTLPSpanExporter.

Prerequisites:
    1. pip install -r requirements.txt
    2. agentevals serve --dev
    3. export OPENAI_API_KEY="your-key-here"

Usage:
    python examples/zero-code-examples/strands/run.py
"""

import os
import sys

from dotenv import load_dotenv
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from strands.telemetry import StrandsTelemetry

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "strands_agent"))
from agent import create_dice_agent

load_dotenv(override=True)

os.environ.setdefault("OTEL_SEMCONV_STABILITY_OPT_IN", "gen_ai_latest_experimental")


def main():
    if not os.getenv("OPENAI_API_KEY"):
        print("OPENAI_API_KEY not set.")
        return

    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318")
    print(f"OTLP endpoint: {endpoint}")

    os.environ.setdefault(
        "OTEL_RESOURCE_ATTRIBUTES",
        "agentevals.eval_set_id=strands_agent_eval,agentevals.session_name=strands-zero-code",
    )

    telemetry = StrandsTelemetry()

    exporter = OTLPSpanExporter()
    telemetry.tracer_provider.add_span_processor(BatchSpanProcessor(exporter, schedule_delay_millis=1000))

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
    telemetry.tracer_provider.force_flush()
    print("All traces flushed to OTLP receiver.")


if __name__ == "__main__":
    main()
