"""Run a Strands dice agent inside AWS AgentCore with standard OTLP export.

Demonstrates zero-code integration: no agentevals SDK is needed.
StrandsTelemetry emits OTel spans which are forwarded to agentevals
via a plain OTLPSpanExporter.

The key difference from a plain Strands script is the AgentCore runtime:
BedrockAgentCoreApp wraps the agent as an HTTP server. The handler is an
async generator decorated with @app.entrypoint and the server starts with
app.run(), listening for POST /invocations requests.

Strands telemetry exports spans even when Bedrock raises NoCredentialsError,
so the OTel pipeline can be tested locally without AWS credentials.

The agent exposes two tools:
  roll_die    -- rolls a die with a given number of sides
  check_prime -- checks whether a number is prime

Prerequisites:
    1. pip install -r examples/zero-code-examples/agentcore/requirements.txt
    2. agentevals serve --dev
    3. Configure AWS credentials (AWS_DEFAULT_REGION, AWS_ACCESS_KEY_ID, etc.)
       or set AWS_DEFAULT_REGION=us-east-1 for local testing without Bedrock.

Usage:
    python examples/zero-code-examples/agentcore/run.py &
    curl http://localhost:8080/invocations -H "Content-Type: application/json" \\
         -d '{"prompt": "Roll a 20-sided die for me"}'
"""

import os
import random

from bedrock_agentcore import BedrockAgentCoreApp
from dotenv import load_dotenv
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from strands import Agent, tool
from strands.models import BedrockModel
from strands.telemetry import StrandsTelemetry

load_dotenv(override=True)
os.environ.setdefault("OTEL_SEMCONV_STABILITY_OPT_IN", "gen_ai_latest_experimental")
os.environ.setdefault(
    "OTEL_RESOURCE_ATTRIBUTES", "agentevals.eval_set_id=agentcore_eval,agentevals.session_name=agentcore-zero-code"
)

_telemetry = StrandsTelemetry()
_telemetry.tracer_provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(), schedule_delay_millis=1000))

app = BedrockAgentCoreApp()


@tool
def roll_die(sides: int = 6) -> int:
    """Roll a die with the given number of sides."""
    return random.randint(1, sides)


@tool
def check_prime(n: int) -> bool:
    """Return True if number is prime."""
    return n >= 2 and all(n % i for i in range(2, int(n**0.5) + 1))


@app.entrypoint
async def handler(payload):
    prompt = payload.get("prompt", "Hello!")
    agent = Agent(
        model=BedrockModel(model_id="us.amazon.nova-pro-v1:0"),
        tools=[roll_die, check_prime],
        system_prompt="You are a helpful assistant. You can roll dice and check if numbers are prime.",
    )
    async for event in agent.stream_async(prompt):
        yield event


app.run()
