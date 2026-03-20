"""LangChain agent with live streaming to agentevals.

This example demonstrates streaming traces and logs from a LangChain agent
to the agentevals dev server for real-time evaluation and visualization.

Key integration points:
1. OpenTelemetry GenAI instrumentation captures LLM calls
2. Spans (metadata) and logs (message content) are streamed via WebSocket
3. Real-time UI shows conversation, tool calls, and token usage

Prerequisites:
    1. Install dependencies:
       $ pip install -r requirements.txt

    2. Start agentevals dev server:
       $ agentevals serve --dev --port 8001

    3. (Optional) Start UI for visualization:
       $ cd ui && npm run dev

    4. Set OpenAI API key:
       $ export OPENAI_API_KEY="your-key-here"

Usage:
    $ python examples/langchain_agent/main.py

The example will run 3 test queries and stream all trace data to the dev server.
View live results at http://localhost:5173
"""

import asyncio
import os
import threading

from agent import create_dice_agent
from dotenv import load_dotenv
from langchain_core.messages import HumanMessage, ToolMessage
from opentelemetry import trace
from opentelemetry._logs import set_logger_provider
from opentelemetry.instrumentation.openai_v2 import OpenAIInstrumentor
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk.trace import TracerProvider

from agentevals.streaming.processor import (
    AgentEvalsLogStreamingProcessor,
    AgentEvalsStreamingProcessor,
)

load_dotenv(override=True)


def setup_otel_streaming(ws_url: str, session_id: str, eval_set_id: str | None = None):
    """Configure OpenTelemetry for streaming traces and logs to agentevals.

    Critical configuration:
    1. Set OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=true to capture
       message content in logs (required for conversation display)
    2. Create both TracerProvider (spans) and LoggerProvider (logs)
    3. Add streaming processors for both spans and logs
    4. Instrument OpenAI SDK AFTER importing LangChain

    Args:
        ws_url: WebSocket URL of agentevals dev server
        session_id: Unique session identifier
        eval_set_id: Optional evaluation set ID for matching

    Returns:
        tuple: (tracer_provider, logger_provider, processor, event_loop)
    """
    os.environ["OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT"] = "true"

    tracer_provider = TracerProvider()
    trace.set_tracer_provider(tracer_provider)

    logger_provider = LoggerProvider()
    set_logger_provider(logger_provider)

    processor = AgentEvalsStreamingProcessor(
        ws_url=ws_url,
        session_id=session_id,
        trace_id=os.urandom(16).hex(),
    )

    loop = asyncio.new_event_loop()

    def run_loop_in_background():
        asyncio.set_event_loop(loop)
        loop.run_forever()

    thread = threading.Thread(target=run_loop_in_background, daemon=True)
    thread.start()

    future = asyncio.run_coroutine_threadsafe(processor.connect(eval_set_id=eval_set_id), loop)
    future.result()

    tracer_provider.add_span_processor(processor)

    log_processor = AgentEvalsLogStreamingProcessor(processor)
    logger_provider.add_log_record_processor(log_processor)

    OpenAIInstrumentor().instrument()

    return tracer_provider, logger_provider, processor, loop


def main():
    if not os.getenv("OPENAI_API_KEY"):
        print("⚠️  OPENAI_API_KEY not set. Set it with:")
        print("   export OPENAI_API_KEY='your-key-here'")
        return

    session_id = f"langchain-session-{os.urandom(4).hex()}"

    print("Setting up OpenTelemetry streaming...")
    tracer_provider, logger_provider, processor, loop = setup_otel_streaming(
        ws_url="ws://localhost:8001/ws/traces",
        session_id=session_id,
        eval_set_id="langchain_agent_eval",
    )

    print("✓ Connected to agentevals dev server")
    print(f"  Session: {session_id}")
    print("  View live: http://localhost:5173")
    print()

    print("🎲 LangChain Dice Agent - Live Dev Mode")
    print("=" * 50)
    print()

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
                agent_response = response.content
                print(f"     Agent: {agent_response}")
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
    print("✓ Agent execution complete")
    print()

    tracer_provider.force_flush()
    logger_provider.force_flush()
    print("✓ All traces and logs flushed to server")

    future = asyncio.run_coroutine_threadsafe(processor.shutdown_async(), loop)
    future.result()
    print("✓ Session ended")


if __name__ == "__main__":
    main()
