"""Run a dice-rolling Pydantic AI agent with OTLP export — no agentevals SDK.

Demonstrates zero-code integration: any OTel-instrumented agent streams
traces to agentevals by pointing the OTLP exporter at the receiver.

Pydantic AI has built-in OTel support via Agent.instrument_all(). By default
it uses version 2 of the GenAI semconv format, storing message content in span
attributes — only a TracerProvider is needed.
No separate instrumentation library is needed.

Prerequisites:
    1. pip install -r requirements.txt
    2. agentevals serve --dev
    3. export OPENAI_API_KEY="your-key-here"

Usage:
    python examples/zero-code-examples/pydantic-ai/run.py
"""

import os
import random

from dotenv import load_dotenv
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from pydantic_ai import Agent

load_dotenv(override=True)


def roll_die(sides: int) -> int:
    """Roll a die with the given number of sides and return the result."""
    return random.randint(1, sides)


def check_prime(number: int) -> bool:
    """Return True if the number is prime, False otherwise."""
    if number < 2:
        return False
    for i in range(2, int(number**0.5) + 1):
        if number % i == 0:
            return False
    return True


def main():
    if not os.getenv("OPENAI_API_KEY"):
        print("OPENAI_API_KEY not set.")
        return

    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318")
    print(f"OTLP endpoint: {endpoint}")

    os.environ.setdefault(
        "OTEL_RESOURCE_ATTRIBUTES",
        "agentevals.eval_set_id=pydantic_ai_eval,agentevals.session_name=pydantic-ai-zero-code",
    )

    resource = Resource.create()

    tracer_provider = TracerProvider(resource=resource)
    tracer_provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(), schedule_delay_millis=1000))
    trace.set_tracer_provider(tracer_provider)

    # Enable Pydantic AI's built-in OTel instrumentation. This one call
    # wires up all agents globally — no framework-specific instrumentor
    # library (like opentelemetry-instrumentation-openai-v2) is needed.
    Agent.instrument_all()

    agent = Agent(
        "openai:gpt-4o-mini",
        instructions="You are a helpful assistant. You can roll dice and check if numbers are prime.",
    )
    agent.tool_plain(roll_die)
    agent.tool_plain(check_prime)

    test_queries = [
        "Hi! Can you help me?",
        "Roll a 20-sided die for me",
        "Is the number you rolled prime?",
    ]

    message_history = []

    try:
        for i, query in enumerate(test_queries, 1):
            print(f"\n[{i}/{len(test_queries)}] User: {query}")

            result = agent.run_sync(query, message_history=message_history)

            print(f"     Agent: {result.output}")

            # Pass the full message history forward for multi-turn conversation.
            message_history = result.all_messages()
    finally:
        print()
        tracer_provider.force_flush()
        print("All traces flushed to OTLP receiver.")


if __name__ == "__main__":
    main()
