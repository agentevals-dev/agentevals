"""Main script for dice_agent with live streaming to agentevals.

This example demonstrates:
1. Streaming the agent's OpenTelemetry spans to agentevals with an AgentEvals session
2. Running an ADK agent with the Runner API
3. Getting real-time evaluation feedback

Prerequisites:
    1. Start agentevals dev server in another terminal:
       $ agentevals serve --dev

    2. Start the UI (optional, to see live visualization):
       $ cd agentevals/ui && npm run dev
       Then click "I am developing an agent"

    3. Set your GOOGLE_API_KEY:
       $ export GOOGLE_API_KEY="your-key-here"

Usage:
    $ python examples/dice_agent/main.py

Try changing the model in agent.py and re-running to see
how the evaluation results change in real-time!
"""

import asyncio
import os
from datetime import datetime

from agent import dice_agent
from dotenv import load_dotenv
from google.adk.runners import InMemoryRunner
from google.genai import types
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider

from agentevals import AgentEvals

load_dotenv(override=True)


async def main():
    """Run dice agent with live streaming enabled."""

    if not os.getenv("GOOGLE_API_KEY"):
        print("⚠️  GOOGLE_API_KEY not set. Set it with:")
        print("   export GOOGLE_API_KEY='your-key-here'")
        print()
        return

    print("🎲 Dice Agent - Live Streaming Example")
    print("=" * 50)
    print()

    provider = TracerProvider()
    trace.set_tracer_provider(provider)

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:21]
    session_name = f"dice-agent-{dice_agent.model}-{timestamp}"
    app = AgentEvals(auto_instrument=False)

    try:
        async with app.session_async(
            eval_set_id="dice_agent_eval",
            session_name=session_name,
            metadata={"model": dice_agent.model, "agent": dice_agent.name},
            tracer_provider=provider,
        ):
            print("✓ Streaming to the agentevals dev server")
            print(f"  Session: {session_name}")
            print(f"  Model: {dice_agent.model}")
            print("  View live: http://localhost:5173")
            print()

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
                    if event.content.parts and event.content.parts[0].text:
                        agent_response = event.content.parts[0].text

                print(f"     Agent: {agent_response}")

        print()
        print("✓ Agent execution complete")
        print("  View in UI: http://localhost:5173")
        print()

    except ConnectionError as e:
        print(f"❌ {e}")
        print()
        print("Make sure agentevals dev server is running:")
        print("  $ agentevals serve --dev")
        print()


if __name__ == "__main__":
    asyncio.run(main())
