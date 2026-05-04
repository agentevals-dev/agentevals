"""AWS AgentCore zero-code OTLP example -- no agentevals SDK.

Setup:
    pip install -r examples/zero-code-examples/agentcore/requirements.txt
    export AWS_DEFAULT_REGION=us-east-1
    agentevals serve --dev

Run:
    python examples/zero-code-examples/agentcore/run.py
    curl http://localhost:8080/invocations -d '{"prompt": "Roll a 20-sided die"}'
    agentcore dev  # alternative: npm install -g @aws/agentcore
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
os.environ.setdefault("OTEL_RESOURCE_ATTRIBUTES",
    "agentevals.eval_set_id=agentcore_eval,agentevals.session_name=agentcore-zero-code")

_telemetry = StrandsTelemetry()
_telemetry.tracer_provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(), schedule_delay_millis=1000))

app = BedrockAgentCoreApp()


@tool
def roll_die(sides: int = 6) -> dict:
    """Roll a die with the given number of sides."""
    result = random.randint(1, sides)
    return {"sides": sides, "result": result, "message": f"Rolled a {sides}-sided die and got {result}"}


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
        system_prompt="Use roll_die when asked to roll dice. Use check_prime when asked about prime numbers.",
        name="dice_agent",
    )
    async for event in agent.stream_async(prompt):
        yield event


app.run()
