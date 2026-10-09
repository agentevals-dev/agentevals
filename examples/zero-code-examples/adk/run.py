"""Run the ADK dice agent with standard OTLP export -- no agentevals SDK.

Demonstrates zero-code integration: ADK auto-instruments itself through
the global TracerProvider, so the only setup needed is a standard
OTLPSpanExporter pointing at the agentevals receiver.

Unlike the LangChain and Strands zero-code examples, ADK needs no
instrumentor call, no LoggerProvider, and no special environment variables.
ADK puts message content directly on span attributes (gcp.vertex.agent.*).

Prerequisites:
    1. pip install -r requirements.txt
    2. agentevals serve --dev
    3. export GOOGLE_API_KEY="your-key-here"

Usage:
    python examples/zero-code-examples/adk/run.py
"""

import asyncio
import os
import sys
import uuid

from dotenv import load_dotenv
from google.adk.runners import InMemoryRunner
from google.genai import types
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "dice_agent"))
from agent import dice_agent

load_dotenv(override=True)


async def main():
    if not os.getenv("GOOGLE_API_KEY"):
        print("GOOGLE_API_KEY not set.")
        return

    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318")
    print(f"OTLP endpoint: {endpoint}")

    os.environ.setdefault("OTEL_SERVICE_NAME", "adk-agent")
    os.environ.setdefault(
        "OTEL_RESOURCE_ATTRIBUTES",
        "agentevals.eval_set_id=dice_agent_eval,agentevals.session_name=adk-zero-code",
    )
    # A process level identity, so agentevals can tell a rerun of this script from the next turn.
    if "service.instance.id=" not in os.environ["OTEL_RESOURCE_ATTRIBUTES"]:
        os.environ["OTEL_RESOURCE_ATTRIBUTES"] += f",service.instance.id={uuid.uuid4()}"

    resource = Resource.create()

    tracer_provider = TracerProvider(resource=resource)
    tracer_provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(), schedule_delay_millis=1000))
    trace.set_tracer_provider(tracer_provider)

    app_name = "dice_agent_app"
    user_id = "demo_user"

    runner = InMemoryRunner(agent=dice_agent, app_name=app_name)
    session = await runner.session_service.create_session(app_name=app_name, user_id=user_id)

    test_queries = [
        "Hi! Can you help me?",
        "Roll a 20-sided die for me",
        "Is the number you rolled prime?",
    ]

    for i, query in enumerate(test_queries, 1):
        print(f"\n[{i}/{len(test_queries)}] User: {query}")

        content = types.Content(role="user", parts=[types.Part.from_text(text=query)])

        agent_response = ""
        async for event in runner.run_async(user_id=user_id, session_id=session.id, new_message=content):
            if event.content and event.content.parts and event.content.parts[0].text:
                agent_response = event.content.parts[0].text

        print(f"     Agent: {agent_response}")

    print()
    tracer_provider.force_flush()
    print("All traces flushed to OTLP receiver.")


if __name__ == "__main__":
    asyncio.run(main())
