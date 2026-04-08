"""Run a LlamaIndex ReActAgent with OTLP export — no agentevals SDK.

Demonstrates zero-code integration: any OTel-instrumented agent streams
traces to agentevals by pointing the OTLP exporter at the receiver.

Uses openinference-instrumentation-llama-index to provide standard
OpenTelemetry spans that are forwarded to the OTLP receiver.

Prerequisites:
    1. pip install -r requirements.txt
    2. agentevals serve --dev
    3. export OPENAI_API_KEY="your-key-here"

Usage:
    python examples/zero-code-examples/llamaindex/run.py
"""

import os
import random

from dotenv import load_dotenv
from llama_index.agent.openai import OpenAIAgent
from llama_index.core.tools import FunctionTool
from llama_index.llms.openai import OpenAI
from openinference.instrumentation.llama_index import LlamaIndexInstrumentor
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

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
        "agentevals.eval_set_id=llamaindex_agent_eval,agentevals.session_name=llamaindex-zero-code",
    )

    resource = Resource.create()

    tracer_provider = TracerProvider(resource=resource)
    tracer_provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(), schedule_delay_millis=1000))
    trace.set_tracer_provider(tracer_provider)

    LlamaIndexInstrumentor().instrument()

    tools = [
        FunctionTool.from_defaults(fn=roll_die),
        FunctionTool.from_defaults(fn=check_prime),
    ]

    llm = OpenAI(model="gpt-4o-mini")
    agent = OpenAIAgent.from_tools(
        tools,
        llm=llm,
        verbose=False,
        system_prompt="You are a helpful assistant. You can roll dice and check if numbers are prime.",
    )

    test_queries = [
        "Hi! Can you help me?",
        "Roll a 20-sided die for me",
        "Is the number you rolled prime?",
    ]

    for i, query in enumerate(test_queries, 1):
        print(f"\n[{i}/{len(test_queries)}] User: {query}")
        response = agent.chat(query)
        print(f"     Agent: {response}")

    print()
    tracer_provider.force_flush()
    print("All traces flushed to OTLP receiver.")


if __name__ == "__main__":
    main()
