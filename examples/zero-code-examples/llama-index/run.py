"""Run a LlamaIndex dice agent with standard OTLP export.

Uses the official LlamaIndexOpenTelemetry integration to set up OTel tracing.
It handles the tracer provider setup and span export internally.
Traces stream to agentevals via OTLPSpanExporter with no agentevals SDK needed.

Note: LlamaIndexOpenTelemetry exports spans only. Log-based content delivery
is not part of this integration. Message content is captured via span attributes.

Prerequisites:
    1. pip install -r examples/zero-code-examples/llama-index/requirements.txt
    2. agentevals serve --dev
    3. export OPENAI_API_KEY="your-key-here"

Usage:
    python examples/zero-code-examples/llama-index/run.py
"""

import asyncio
import os
import random

from dotenv import load_dotenv
from llama_index.core.agent.workflow import FunctionAgent
from llama_index.core.tools import FunctionTool
from llama_index.llms.openai_like import OpenAILike
from llama_index.observability.otel import LlamaIndexOpenTelemetry
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource

load_dotenv(override=True)


def roll_die(sides: int) -> int:
    """Roll a die with the given number of sides and return the result."""
    return random.randint(1, sides)


def check_prime(number: int) -> bool:
    """Return True if the number is prime, False otherwise."""
    if number < 2:
        return False
    return all(number % i for i in range(2, int(number**0.5) + 1))


async def main():
    if not os.getenv("OPENAI_API_KEY"):
        print("OPENAI_API_KEY not set.")
        return

    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318")
    print(f"OTLP endpoint: {endpoint}")

    os.environ.setdefault(
        "OTEL_RESOURCE_ATTRIBUTES",
        "agentevals.eval_set_id=llama_index_eval,agentevals.session_name=llama-index-zero-code",
    )

    resource = Resource.create()

    LlamaIndexOpenTelemetry(
        span_exporter=OTLPSpanExporter(),
        span_processor="batch",
        service_name_or_resource=resource,
    ).start_registering()

    llm = OpenAILike(
        model=os.environ.get("OPENAI_MODEL", "gpt-4o-mini"),
        api_base=os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1"),
        is_chat_model=True,
        is_function_calling_model=True,
    )
    agent = FunctionAgent(
        tools=[FunctionTool.from_defaults(fn=roll_die), FunctionTool.from_defaults(fn=check_prime)],
        llm=llm,
        system_prompt="Use roll_die when asked to roll dice. Use check_prime when asked about prime numbers.",
    )

    test_queries = [
        "Hi! Can you help me?",
        "Roll a 20-sided die for me",
        "Is the number you rolled prime?",
    ]

    try:
        for i, query in enumerate(test_queries, 1):
            print(f"\n[{i}/{len(test_queries)}] User: {query}")
            result = await agent.run(query)
            print(f"     Agent: {result.response.content}")
    finally:
        print()
        trace.get_tracer_provider().force_flush()
        print("All traces flushed to OTLP receiver.")


if __name__ == "__main__":
    asyncio.run(main())
