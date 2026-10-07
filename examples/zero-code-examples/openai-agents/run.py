"""Run a dice-rolling OpenAI Agents SDK agent with OTLP export — no agentevals SDK.

Demonstrates zero-code integration: any OTel-instrumented agent streams
traces to agentevals by pointing the OTLP exporter at the receiver.

Unlike the LangChain and Strands examples, this one is fully self-contained:
the agent code lives inline with no cross-folder imports.

Prerequisites:
    1. pip install -r requirements.txt
    2. agentevals serve --dev
    3. export OPENAI_API_KEY="your-key-here"

Usage:
    python examples/zero-code-examples/openai-agents/run.py
"""

import os
import random
import uuid

from agents import Agent, Runner, function_tool
from dotenv import load_dotenv
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.openai_agents import OpenAIAgentsInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

load_dotenv(override=True)


@function_tool
def roll_die(sides: int) -> int:
    """Roll a die with the given number of sides and return the result."""
    return random.randint(1, sides)


@function_tool
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

    os.environ.setdefault("OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT", "span_and_event")
    os.environ.setdefault("OTEL_SEMCONV_STABILITY_OPT_IN", "gen_ai_latest_experimental")

    os.environ.setdefault("OTEL_SERVICE_NAME", "openai-agents-agent")
    os.environ.setdefault(
        "OTEL_RESOURCE_ATTRIBUTES",
        "agentevals.eval_set_id=openai_agents_eval,agentevals.session_name=openai-agents-zero-code",
    )
    # A process level identity, so agentevals can tell a rerun of this script from the next turn.
    if "service.instance.id=" not in os.environ["OTEL_RESOURCE_ATTRIBUTES"]:
        os.environ["OTEL_RESOURCE_ATTRIBUTES"] += f",service.instance.id={uuid.uuid4()}"

    resource = Resource.create()

    tracer_provider = TracerProvider(resource=resource)
    tracer_provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(), schedule_delay_millis=1000))
    trace.set_tracer_provider(tracer_provider)

    OpenAIAgentsInstrumentor().instrument()

    agent = Agent(
        name="Dice Agent",
        instructions="You are a helpful assistant. You can roll dice and check if numbers are prime.",
        tools=[roll_die, check_prime],
    )

    test_queries = [
        "Hi! Can you help me?",
        "Roll a 20-sided die for me",
        "Is the number you rolled prime?",
    ]

    conversation_input: list = []

    try:
        for i, query in enumerate(test_queries, 1):
            print(f"\n[{i}/{len(test_queries)}] User: {query}")

            conversation_input.append({"role": "user", "content": query})
            result = Runner.run_sync(agent, conversation_input)

            agent_response = result.final_output or ""
            print(f"     Agent: {agent_response}")

            conversation_input = result.to_input_list()
    finally:
        print()
        tracer_provider.force_flush()
        print("All traces flushed to OTLP receiver.")


if __name__ == "__main__":
    main()
