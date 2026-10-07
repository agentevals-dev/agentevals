"""LangChain agent with live streaming to agentevals.

This example demonstrates streaming traces and logs from a LangChain agent
to the agentevals dev server for real-time evaluation and visualization.

Key integration points:
1. OpenTelemetry GenAI instrumentation (openai-v2) captures LLM calls
2. An AgentEvals session exports the spans and GenAI log events produced inside it over OTLP
3. Real-time UI shows conversation, tool calls, and token usage

Prerequisites:
    1. Install dependencies:
       $ pip install -r requirements.txt

    2. Start agentevals dev server:
       $ agentevals serve --dev

    3. (Optional) Start UI for visualization:
       $ cd ui && npm run dev

    4. Set OpenAI API key:
       $ export OPENAI_API_KEY="your-key-here"

Usage:
    $ python examples/langchain_agent/main.py

The example will run 3 test queries and stream all trace data to the dev server.
View live results at http://localhost:5173
"""

import os

from agent import create_dice_agent
from dotenv import load_dotenv
from langchain_core.messages import HumanMessage, ToolMessage

from agentevals import AgentEvals

load_dotenv(override=True)


def main():
    if not os.getenv("OPENAI_API_KEY"):
        print("⚠️  OPENAI_API_KEY not set. Set it with:")
        print("   export OPENAI_API_KEY='your-key-here'")
        return

    session_name = f"langchain-session-{os.urandom(4).hex()}"
    # The SDK instruments the OpenAI client (openai-v2) and sets up the log provider that
    # carries message content, so the agent module only needs to be imported first.
    app = AgentEvals(eval_set_id="langchain_agent_eval")

    with app.session(session_name=session_name):
        print("✓ Streaming to the agentevals dev server")
        print(f"  Session: {session_name}")
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
    print("✓ Agent execution complete, session flushed to the server")


if __name__ == "__main__":
    main()
