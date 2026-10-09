"""GenAI semantic convention keys read and written by agentevals.

Pinned to open-telemetry/semantic-conventions-genai at 4f85037 (main, 2026-10-06; the
repository has no releases yet and its manifest declares ``gen-ai-dev/1.42.0-dev``). Every key
here is at stability *development*, so this module is revisited when GenAI conventions are
released. Classification never relies on ``schema_url`` because producers stamp unrelated
versions.
"""

from __future__ import annotations

OPERATION_NAME = "gen_ai.operation.name"
PROVIDER_NAME = "gen_ai.provider.name"
CONVERSATION_ID = "gen_ai.conversation.id"
AGENT_NAME = "gen_ai.agent.name"
AGENT_ID = "gen_ai.agent.id"
WORKFLOW_NAME = "gen_ai.workflow.name"

REQUEST_MODEL = "gen_ai.request.model"
RESPONSE_MODEL = "gen_ai.response.model"
RESPONSE_ID = "gen_ai.response.id"
RESPONSE_FINISH_REASONS = "gen_ai.response.finish_reasons"

INPUT_MESSAGES = "gen_ai.input.messages"
OUTPUT_MESSAGES = "gen_ai.output.messages"
SYSTEM_INSTRUCTIONS = "gen_ai.system_instructions"
TOOL_DEFINITIONS = "gen_ai.tool.definitions"

USAGE_INPUT_TOKENS = "gen_ai.usage.input_tokens"
USAGE_OUTPUT_TOKENS = "gen_ai.usage.output_tokens"
USAGE_CACHE_READ_INPUT_TOKENS = "gen_ai.usage.cache_read.input_tokens"
USAGE_CACHE_WRITE_INPUT_TOKENS = "gen_ai.usage.cache_write.input_tokens"
USAGE_REASONING_OUTPUT_TOKENS = "gen_ai.usage.reasoning.output_tokens"

TOOL_NAME = "gen_ai.tool.name"
TOOL_TYPE = "gen_ai.tool.type"
TOOL_CALL_ID = "gen_ai.tool.call.id"
TOOL_CALL_ARGUMENTS = "gen_ai.tool.call.arguments"
TOOL_CALL_RESULT = "gen_ai.tool.call.result"

EVALUATION_NAME = "gen_ai.evaluation.name"
EVALUATION_SCORE_VALUE = "gen_ai.evaluation.score.value"
EVALUATION_SCORE_LABEL = "gen_ai.evaluation.score.label"
EVALUATION_EXPLANATION = "gen_ai.evaluation.explanation"

ERROR_TYPE = "error.type"
ERROR_TYPE_OTHER = "_OTHER"
SESSION_ID = "session.id"
SERVICE_NAME = "service.name"

OP_CHAT = "chat"
OP_GENERATE_CONTENT = "generate_content"
OP_TEXT_COMPLETION = "text_completion"
OP_INVOKE_AGENT = "invoke_agent"
OP_INVOKE_WORKFLOW = "invoke_workflow"
OP_EXECUTE_TOOL = "execute_tool"

INFERENCE_OPERATIONS = frozenset({OP_CHAT, OP_GENERATE_CONTENT, OP_TEXT_COMPLETION})
AGENTIC_OPERATIONS = frozenset({OP_INVOKE_AGENT, OP_INVOKE_WORKFLOW})
OTHER_GENAI_OPERATIONS = frozenset(
    {
        "embeddings",
        "retrieval",
        "fetch_response",
        "create_agent",
        "plan",
        "search_memory",
        "create_memory",
        "update_memory",
        "upsert_memory",
        "delete_memory",
        "create_memory_store",
        "delete_memory_store",
    }
)

EVENT_INFERENCE_DETAILS = "gen_ai.client.inference.operation.details"
EVENT_EVALUATION_RESULT = "gen_ai.evaluation.result"
EVENT_OPERATION_EXCEPTION = "gen_ai.client.operation.exception"

EVENT_SYSTEM_MESSAGE = "gen_ai.system.message"
EVENT_USER_MESSAGE = "gen_ai.user.message"
EVENT_ASSISTANT_MESSAGE = "gen_ai.assistant.message"
EVENT_TOOL_MESSAGE = "gen_ai.tool.message"
EVENT_CHOICE = "gen_ai.choice"
LEGACY_MESSAGE_EVENTS = frozenset(
    {EVENT_SYSTEM_MESSAGE, EVENT_USER_MESSAGE, EVENT_ASSISTANT_MESSAGE, EVENT_TOOL_MESSAGE, EVENT_CHOICE}
)

# Deprecated attribute -> current key. Values are copied as is unless the overlay applies a
# shape rule (finish_reason is a scalar wrapped in a list).
DEPRECATED_ALIASES: dict[str, str] = {
    "gen_ai.system": PROVIDER_NAME,
    "gen_ai.usage.prompt_tokens": USAGE_INPUT_TOKENS,
    "gen_ai.usage.completion_tokens": USAGE_OUTPUT_TOKENS,
    "gen_ai.usage.cache_creation.input_tokens": USAGE_CACHE_WRITE_INPUT_TOKENS,
    "gen_ai.response.finish_reason": RESPONSE_FINISH_REASONS,
}

# gen_ai.system values that name a framework rather than a model provider.
FRAMEWORK_SYSTEM_VALUES = frozenset({"gcp.vertex.agent"})

# Placeholders producers write when content is not captured or not known.
MESSAGE_PLACEHOLDERS = frozenset({"<REDACTED>", "<elided>", ""})
TOOL_PLACEHOLDERS = frozenset({"<not specified>"})
