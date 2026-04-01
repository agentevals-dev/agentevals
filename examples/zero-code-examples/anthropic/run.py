"""Run an Anthropic Claude dice agent with standard OTLP export — no agentevals SDK.

Demonstrates zero-code integration: any OTel-instrumented agent streams
traces and logs to agentevals by pointing the OTLP exporter at the receiver.

The only agentevals-specific setup is the OTLP endpoint and resource
attributes. The agent code itself is unchanged.

Prerequisites:
    1. pip install -r requirements.txt
    2. agentevals serve --dev
    3. export ANTHROPIC_API_KEY="your-key-here"

Usage:
    python examples/zero-code-examples/anthropic/run.py
"""

import json
import os
import random

import anthropic
from dotenv import load_dotenv
from opentelemetry import trace
from opentelemetry._logs import set_logger_provider
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.anthropic import AnthropicInstrumentor
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

load_dotenv(override=True)

TOOLS = [
    {
        "name": "roll_die",
        "description": "Roll a single die with the given number of sides and return the result.",
        "input_schema": {
            "type": "object",
            "properties": {"n_sides": {"type": "integer", "description": "Number of sides on the die."}},
            "required": ["n_sides"],
        },
    },
    {
        "name": "check_prime",
        "description": "Return true if the given number is prime, false otherwise.",
        "input_schema": {
            "type": "object",
            "properties": {"number": {"type": "integer", "description": "The number to check."}},
            "required": ["number"],
        },
    },
]


def roll_die(n_sides: int) -> int:
    return random.randint(1, n_sides)


def check_prime(number: int) -> bool:
    if number < 2:
        return False
    for i in range(2, int(number**0.5) + 1):
        if number % i == 0:
            return False
    return True


def run_agent(client: anthropic.Anthropic, query: str) -> str:
    messages = [{"role": "user", "content": query}]

    while True:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            tools=TOOLS,
            messages=messages,
        )

        if response.stop_reason == "end_turn":
            for block in response.content:
                if hasattr(block, "text"):
                    return block.text
            return ""

        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                if block.name == "roll_die":
                    result = roll_die(**block.input)
                elif block.name == "check_prime":
                    result = check_prime(**block.input)
                else:
                    result = "Unknown tool"
                tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": json.dumps(result)})

        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": tool_results})


def main():
    if not os.getenv("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY not set.")
        return

    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318")
    print(f"OTLP endpoint: {endpoint}")

    os.environ["OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT"] = "true"

    os.environ.setdefault(
        "OTEL_RESOURCE_ATTRIBUTES",
        "agentevals.eval_set_id=anthropic_agent_eval,agentevals.session_name=anthropic-zero-code",
    )

    resource = Resource.create()

    tracer_provider = TracerProvider(resource=resource)
    tracer_provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(), schedule_delay_millis=1000))
    trace.set_tracer_provider(tracer_provider)

    logger_provider = LoggerProvider(resource=resource)
    logger_provider.add_log_record_processor(BatchLogRecordProcessor(OTLPLogExporter(), schedule_delay_millis=1000))
    set_logger_provider(logger_provider)

    AnthropicInstrumentor().instrument()

    client = anthropic.Anthropic()

    test_queries = [
        "Roll a 20-sided die for me",
        "Is the number you rolled prime?",
    ]

    for i, query in enumerate(test_queries, 1):
        print(f"\n[{i}/{len(test_queries)}] User: {query}")
        response = run_agent(client, query)
        print(f"     Agent: {response}")

    print()
    tracer_provider.force_flush()
    logger_provider.force_flush()
    print("All traces and logs flushed to OTLP receiver.")


if __name__ == "__main__":
    main()
