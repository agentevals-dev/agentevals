"""Run a Pydantic AI agent with OTLP export — no agentevals SDK.

Demonstrates zero-code integration: any OTel-instrumented agent streams
traces to agentevals by pointing the OTLP exporter at the receiver.

Pydantic AI uses logfire for instrumentation. Configuring logfire with
send_to_logfire=False while OTEL_EXPORTER_OTLP_ENDPOINT is set lets
logfire forward all spans to any standard OTLP receiver, including
agentevals, without any manual TracerProvider setup.

Prerequisites:
    1. pip install -r requirements.txt
    2. agentevals serve --dev
    3. export OPENAI_API_KEY="your-key-here"

Usage:
    python examples/zero-code-examples/pydantic-ai/run.py
"""

import asyncio
import os
import random

import logfire
from dotenv import load_dotenv
from opentelemetry import trace
from pydantic_ai import Agent

load_dotenv(override=True)

agent = Agent(
    "openai:gpt-4o-mini",
    system_prompt="You are a helpful assistant. You can roll dice and check if numbers are prime.",
)


@agent.tool_plain
def roll_die(sides: int) -> int:
    """Roll a die with the given number of sides and return the result."""
    return random.randint(1, sides)


@agent.tool_plain
def check_prime(number: int) -> bool:
    """Return True if the number is prime, False otherwise."""
    if number < 2:
        return False
    for i in range(2, int(number**0.5) + 1):
        if number % i == 0:
            return False
    return True


async def main():
    if not os.getenv("OPENAI_API_KEY"):
        print("OPENAI_API_KEY not set.")
        return

    os.environ.setdefault("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318")
    print(f"OTLP endpoint: {os.environ['OTEL_EXPORTER_OTLP_ENDPOINT']}")

    os.environ.setdefault(
        "OTEL_RESOURCE_ATTRIBUTES",
        "agentevals.eval_set_id=pydantic_ai_eval,agentevals.session_name=pydantic-ai-zero-code",
    )

    logfire.configure(send_to_logfire=False)
    logfire.instrument_pydantic_ai()

    test_queries = [
        "Hi! Can you help me?",
        "Roll a 20-sided die for me",
        "Is the number you rolled prime?",
    ]

    history = []
    try:
        for i, query in enumerate(test_queries, 1):
            print(f"\n[{i}/{len(test_queries)}] User: {query}")
            result = await agent.run(query, message_history=history)
            history = result.all_messages()
            print(f"     Agent: {result.output}")
    finally:
        print()
        tracer_provider = trace.get_tracer_provider()
        if hasattr(tracer_provider, "force_flush"):
            tracer_provider.force_flush()
        print("All traces flushed to OTLP receiver.")


if __name__ == "__main__":
    asyncio.run(main())
