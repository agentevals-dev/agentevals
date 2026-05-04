"""LlamaIndex zero-code OTLP example."""
import asyncio
import os
import random

from dotenv import load_dotenv
from llama_index.core.agent.workflow import FunctionAgent
from llama_index.core.tools import FunctionTool
from llama_index.llms.openai_like import OpenAILike
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

load_dotenv(override=True)


def roll_die(sides: int) -> int:
    """Roll a die and return the result."""
    return random.randint(1, sides)


def check_prime(number: int) -> bool:
    """Check if a number is prime."""
    return number >= 2 and all(number % i for i in range(2, int(number**0.5) + 1))


async def main():
    if not os.getenv("OPENAI_API_KEY"):
        print("OPENAI_API_KEY not set.")
        return

    os.environ["OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT"] = "true"
    os.environ.setdefault("OTEL_RESOURCE_ATTRIBUTES",
        "agentevals.eval_set_id=llama_index_eval,agentevals.session_name=llama-index-zero-code")

    resource = Resource.create()

    tp = TracerProvider(resource=resource)
    tp.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(), schedule_delay_millis=1000))
    trace.set_tracer_provider(tp)

    lp = LoggerProvider(resource=resource)
    lp.add_log_record_processor(BatchLogRecordProcessor(OTLPLogExporter(), schedule_delay_millis=1000))
    set_logger_provider(lp)

    OpenAIInstrumentor().instrument()

    llm = OpenAILike(
        model=os.environ.get("OPENAI_MODEL", "gpt-4o-mini"),
        api_base=os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1"),
        is_chat_model=True, is_function_calling_model=True,
    )
    agent = FunctionAgent(
        tools=[FunctionTool.from_defaults(fn=roll_die), FunctionTool.from_defaults(fn=check_prime)],
        llm=llm,
        system_prompt="Use roll_die for dice rolls. Use check_prime to check if a number is prime.",
    )

    queries = ["Hi! Can you help me?", "Roll a 20-sided die.", "Roll a 6-sided die and check if the result is prime."]
    for i, query in enumerate(queries, 1):
        print(f"\n[{i}/{len(queries)}] User: {query}")
        result = await agent.run(query)
        print(f"     Response: {result.response.content}")

    print()
    tp.force_flush()
    lp.force_flush()
    print("All traces flushed.")


if __name__ == "__main__":
    asyncio.run(main())
