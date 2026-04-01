"""Run the LangChain dice agent against local Ollama via ChatOpenAI with OTLP export.

Demonstrates zero-code integration with a local LLM provider using Ollama's
OpenAI-compatible endpoint. The agent emits standard OpenTelemetry spans/logs
and sends them to agentevals via OTLP, without using the agentevals Python SDK
in agent code.

Prerequisites:
    1. pip install -r requirements.txt
    2. agentevals serve --dev
    3. ollama serve
    4. ollama pull llama3.2:3b

Usage:
    python examples/zero-code-examples/ollama/run.py

Optional environment variables:
    LOCAL_OPENAI_BASE_URL (default: http://localhost:11434/v1)
    LOCAL_LLM_MODEL (default: llama3.2:3b)
"""

import json
import os
import sys

from dotenv import load_dotenv
from langchain_core.messages import HumanMessage, ToolMessage
from langchain_openai import ChatOpenAI
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
from agent import check_prime, roll_die

load_dotenv(override=True)


def _json_or_value(value):
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def _to_int_if_numeric(value):
    value = _json_or_value(value)
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.lstrip("-").isdigit():
            return int(stripped)
        if "," in stripped:
            parts = [p.strip() for p in stripped.split(",") if p.strip()]
            if parts and all(p.lstrip("-").isdigit() for p in parts):
                return [int(p) for p in parts]
    return value


def _normalize_nums(values):
    parsed = _json_or_value(values)
    if not isinstance(parsed, list):
        parsed = [parsed]

    normalized = []
    for item in parsed:
        item = _to_int_if_numeric(item)
        if isinstance(item, list):
            normalized.extend(item)
        else:
            normalized.append(item)
    return normalized


def _normalize_tool_args(tool_name: str, tool_args):
    parsed = _json_or_value(tool_args)
    if tool_name == "roll_die":
        if isinstance(parsed, dict):
            if "sides" in parsed:
                parsed["sides"] = _to_int_if_numeric(parsed["sides"])
            return parsed
        return {"sides": _to_int_if_numeric(parsed)}

    if tool_name == "check_prime":
        if isinstance(parsed, dict):
            parsed["nums"] = _normalize_nums(parsed.get("nums", []))
            return parsed
        if isinstance(parsed, list):
            return {"nums": _normalize_nums(parsed)}
        return {"nums": _normalize_nums(parsed)}

    return parsed


def main():
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318")
    base_url = os.environ.get("LOCAL_OPENAI_BASE_URL", "http://localhost:11434/v1")
    model = os.environ.get("LOCAL_LLM_MODEL", "llama3.2:3b")

    print(f"OTLP endpoint: {endpoint}")
    print(f"Local model endpoint: {base_url}")
    print(f"Local model: {model}")

    os.environ["OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT"] = "true"
    os.environ.setdefault(
        "OTEL_RESOURCE_ATTRIBUTES",
        "agentevals.eval_set_id=langchain_local_ollama_openai_eval,agentevals.session_name=langchain-ollama-openai-zero-code",
    )

    resource = Resource.create()

    tracer_provider = TracerProvider(resource=resource)
    tracer_provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(), schedule_delay_millis=1000))
    trace.set_tracer_provider(tracer_provider)

    logger_provider = LoggerProvider(resource=resource)
    logger_provider.add_log_record_processor(BatchLogRecordProcessor(OTLPLogExporter(), schedule_delay_millis=1000))
    set_logger_provider(logger_provider)

    OpenAIInstrumentor().instrument()

    llm = ChatOpenAI(model=model, temperature=0.0, base_url=base_url, api_key="ollama")
    tools = [roll_die, check_prime]
    llm_with_tools = llm.bind_tools(tools)

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
        for _ in range(max_iterations):
            response = llm_with_tools.invoke(messages)
            messages.append(response)

            if not response.tool_calls:
                print(f"     Agent: {response.content}")
                break

            for tool_call in response.tool_calls:
                tool_name = tool_call["name"]
                tool_args = tool_call["args"]
                tool_call_id = tool_call.get("id", f"tool-call-{tool_name}")
                normalized_args = _normalize_tool_args(tool_name, tool_args)

                selected_tool = {t.name: t for t in tools}.get(tool_name)
                if selected_tool:
                    try:
                        tool_result = selected_tool.invoke(normalized_args)
                    except Exception as exc:
                        tool_result = {
                            "isError": True,
                            "error": str(exc),
                            "tool_name": tool_name,
                            "args": normalized_args,
                        }

                    tool_content = json.dumps(tool_result) if isinstance(tool_result, dict) else str(tool_result)
                    messages.append(ToolMessage(content=tool_content, tool_call_id=tool_call_id))
                else:
                    messages.append(
                        ToolMessage(
                            content=json.dumps({"isError": True, "error": f"Unknown tool: {tool_name}"}),
                            tool_call_id=tool_call_id,
                        )
                    )
        else:
            print("     Agent: [Max iterations reached]")

    print()
    tracer_provider.force_flush()
    logger_provider.force_flush()
    print("All traces and logs flushed to OTLP receiver.")


if __name__ == "__main__":
    main()
