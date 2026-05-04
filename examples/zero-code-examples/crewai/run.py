"""Run a CrewAI dice agent with standard OTLP export — no agentevals SDK.

Demonstrates zero-code integration: any OTel-instrumented agent streams
traces and logs to agentevals by pointing the OTLP exporter at the receiver.

The only agentevals-specific setup is the OTLP endpoint and resource
attributes. The agent code itself is unchanged.

Prerequisites:
    1. pip install -r requirements.txt
    2. agentevals serve --dev
    3. export OPENAI_API_KEY="your-key-here"

Usage:
    python examples/zero-code-examples/crewai/run.py
"""

import os
import random

from dotenv import load_dotenv
from opentelemetry import trace
from opentelemetry._logs import set_logger_provider
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.crewai import CrewAIInstrumentor
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

load_dotenv(override=True)


def main():
    if not os.getenv("OPENAI_API_KEY"):
        print("OPENAI_API_KEY not set.")
        return

    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318")
    print(f"OTLP endpoint: {endpoint}")

    os.environ["OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT"] = "true"

    os.environ.setdefault(
        "OTEL_RESOURCE_ATTRIBUTES",
        "agentevals.eval_set_id=crewai_agent_eval,agentevals.session_name=crewai-zero-code",
    )

    resource = Resource.create()

    tracer_provider = TracerProvider(resource=resource)
    tracer_provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(), schedule_delay_millis=1000))
    trace.set_tracer_provider(tracer_provider)

    logger_provider = LoggerProvider(resource=resource)
    logger_provider.add_log_record_processor(BatchLogRecordProcessor(OTLPLogExporter(), schedule_delay_millis=1000))
    set_logger_provider(logger_provider)

    CrewAIInstrumentor().instrument()

    # crewai imported after OTel setup to avoid TracerProvider conflict
    from crewai import Agent, Crew, Task
    from crewai.tools import tool

    @tool("roll_die")
    def roll_die(n_sides: int) -> int:
        """Roll a single die with the given number of sides and return the result."""
        return random.randint(1, n_sides)

    @tool("check_prime")
    def check_prime(number: int) -> bool:
        """Return True if the given number is prime, False otherwise."""
        if number < 2:
            return False
        for i in range(2, int(number**0.5) + 1):
            if number % i == 0:
                return False
        return True

    dice_agent = Agent(
        role="Dice Roller",
        goal="Help the user roll dice and answer questions about the results.",
        backstory="You are an expert at rolling dice and checking mathematical properties of numbers.",
        tools=[roll_die, check_prime],
        verbose=True,
    )

    test_queries = [
        "Roll a 20-sided die for me",
        "Is the number you rolled prime?",
    ]

    for i, query in enumerate(test_queries, 1):
        print(f"\n[{i}/{len(test_queries)}] User: {query}")

        task = Task(
            description=query,
            expected_output="A direct answer to the user's question.",
            agent=dice_agent,
        )

        crew = Crew(agents=[dice_agent], tasks=[task], verbose=True)
        result = crew.kickoff()
        print(f"     Agent: {result}")

    print()
    tracer_provider.force_flush()
    logger_provider.force_flush()
    print("All traces and logs flushed to OTLP receiver.")


if __name__ == "__main__":
    main()
