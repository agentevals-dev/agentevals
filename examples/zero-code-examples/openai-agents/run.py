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

from agents import Agent, Runner, function_tool
from dotenv import load_dotenv
from opentelemetry import trace
from opentelemetry._logs import set_logger_provider
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.openai_agents import OpenAIAgentsInstrumentor
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

load_dotenv(override=True)


# ── Tool definitions ──────────────────────────────────────────────────────────

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

    # openai-agents-v2 uses ContentCaptureMode — use "span_and_event" for maximum
    # content capture. agentevals reads from span attributes, so content will be
    # visible in the UI.
    os.environ["OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT"] = "span_and_event"

    os.environ.setdefault(
        "OTEL_RESOURCE_ATTRIBUTES",
        "agentevals.eval_set_id=openai_agents_eval,agentevals.session_name=openai-agents-zero-code",
    )

    # OTel setup flow:
    #
    #   Resource (session/eval attrs)
    #       │
    #       ├── TracerProvider → BatchSpanProcessor → OTLPSpanExporter → :4318
    #       │
    #       └── LoggerProvider → BatchLogRecordProcessor → OTLPLogExporter → :4318
    #           (openai-agents-v2 may route message content to span attributes
    #            rather than log records; OTLPLogExporter is a no-op if unused)
    #
    #   OpenAIAgentsInstrumentor.instrument()
    #       └── hooks into agents SDK → emits spans + (optionally) log records
    #
    #   Runner.run_sync(agent, input)  ← called with accumulated conversation

    resource = Resource.create()

    tracer_provider = TracerProvider(resource=resource)
    tracer_provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(), schedule_delay_millis=1000))
    trace.set_tracer_provider(tracer_provider)

    logger_provider = LoggerProvider(resource=resource)
    logger_provider.add_log_record_processor(BatchLogRecordProcessor(OTLPLogExporter(), schedule_delay_millis=1000))
    set_logger_provider(logger_provider)

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

    # Accumulate conversation context so turn 3 ("Is it prime?") can reference
    # the number rolled in turn 2. openai-agents Runner.run_sync() is stateless
    # per call, so we thread prior messages via result.to_input_list() — the SDK's
    # canonical way to carry full response history (tool calls, tool results, etc.)
    # into the next turn. Raw role/content dicts can silently drop tool-call context.
    conversation_input: list = []

    try:
        for i, query in enumerate(test_queries, 1):
            print(f"\n[{i}/{len(test_queries)}] User: {query}")

            conversation_input.append({"role": "user", "content": query})
            result = Runner.run_sync(agent, conversation_input)

            agent_response = result.final_output or ""
            print(f"     Agent: {agent_response}")

            # to_input_list() returns the full turn history including tool calls and
            # results, which is what the SDK expects as input for the next turn.
            conversation_input = result.to_input_list()
    finally:
        # Always flush — even if a turn raises (rate limit, network error, etc.)
        # so that whatever spans were recorded make it to the OTLP receiver.
        print()
        tracer_provider.force_flush()
        logger_provider.force_flush()
        print("All traces and logs flushed to OTLP receiver.")


if __name__ == "__main__":
    main()
