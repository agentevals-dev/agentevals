"""Run the LangChain dice agent with standard OTLP export — no agentevals SDK.

Demonstrates zero-code integration: any OTel-instrumented agent streams
traces and logs to agentevals by pointing the OTLP exporter at the receiver.

The only change vs. the original langchain_agent example is replacing
AgentEvalsStreamingProcessor/AgentEvalsLogStreamingProcessor with standard
OTLPSpanExporter and OTLPLogExporter.

Prerequisites:
    1. pip install -r requirements.txt
    2. agentevals serve --dev
    3. export OPENAI_API_KEY="your-key-here"

Usage:
    python examples/zero-code-examples/langchain/run.py
"""

import os
import sys

from dotenv import load_dotenv
from langchain_core.messages import HumanMessage, ToolMessage
from opentelemetry import trace
from opentelemetry._logs import set_logger_provider
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.openai_v2 import OpenAIInstrumentor
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "langchain_agent"))
from agent import create_dice_agent

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
        "agentevals.eval_set_id=langchain_agent_eval,agentevals.session_name=langchain-zero-code",
    )

    resource = Resource.create()

    tracer_provider = TracerProvider(resource=resource)
    tracer_provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(), schedule_delay_millis=1000))
    trace.set_tracer_provider(tracer_provider)

    logger_provider = LoggerProvider(resource=resource)
    logger_provider.add_log_record_processor(BatchLogRecordProcessor(OTLPLogExporter(), schedule_delay_millis=1000))
    set_logger_provider(logger_provider)

    OpenAIInstrumentor().instrument()

    llm_with_tools, tools = create_dice_agent()

    test_queries = [
        "Hi! Can you help me?",
        "Roll a 20-sided die for me",
        "Is the number you rolled prime?",
    ]

    messages = []

    for i, query in enumerate(test_queries, 1):
        print(f"\n[{i}/{len(test_queries)}] User: {query}")

        messages.append(HumanMessage(content=query))

        max_iterations = 5
        for iteration in range(max_iterations):
            response = llm_with_tools.invoke(messages)
            messages.append(response)

            if not response.tool_calls:
                print(f"     Agent: {response.content}")
                break

            for tool_call in response.tool_calls:
                tool_name = tool_call["name"]
                tool_args = tool_call["args"]

                selected_tool = {t.name: t for t in tools}.get(tool_name)
                if selected_tool:
                    tool_result = selected_tool.invoke(tool_args)
                    messages.append(ToolMessage(content=str(tool_result), tool_call_id=tool_call["id"]))
        else:
            print("     Agent: [Max iterations reached]")

    print()
    tracer_provider.force_flush()
    logger_provider.force_flush()
    print("All traces and logs flushed to OTLP receiver.")


if __name__ == "__main__":
    main()
