"""Main script for dice_agent with live streaming to agentevals.

This example demonstrates:
1. Setting up OpenTelemetry with agentevals streaming
2. Running an ADK agent with the Runner API
3. Getting real-time evaluation feedback

Prerequisites:
    1. Start agentevals dev server in another terminal:
       $ agentevals serve --dev --port 8001

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

from agent import dice_agent
from dotenv import load_dotenv
from google.adk.runners import InMemoryRunner
from google.genai import types
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider

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

    try:
        from agentevals.streaming.processor import AgentEvalsStreamingProcessor
        from datetime import datetime

        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:21]
        session_id = f"dice-agent-{dice_agent.model}-{timestamp}"
        processor = AgentEvalsStreamingProcessor(
            ws_url="ws://localhost:8001/ws/traces",
            session_id=session_id,
            trace_id="dice-" + os.urandom(8).hex(),
        )

        await processor.connect(
            eval_set_id="dice_agent_eval",
            metadata={"model": dice_agent.model, "agent": dice_agent.name},
        )

        provider.add_span_processor(processor)

        print(f"✓ Connected to agentevals dev server")
        print(f"  Session: {session_id}")
        print(f"  Model: {dice_agent.model}")
        print(f"  View live: http://localhost:5173")
        print()

        app_name = "dice_agent_app"
        user_id = "demo_user"

        runner = InMemoryRunner(agent=dice_agent, app_name=app_name)
        session = await runner.session_service.create_session(
            app_name=app_name, user_id=user_id
        )

        test_queries = [
            "Hi! Can you help me?",
            "Roll a 20-sided die for me",
            "Is the number you rolled prime?",
        ]

        for i, query in enumerate(test_queries, 1):
            print(f"\n[{i}/{len(test_queries)}] User: {query}")

            content = types.Content(
                role="user", parts=[types.Part.from_text(text=query)]
            )

            agent_response = ""
            async for event in runner.run_async(
                user_id=user_id, session_id=session.id, new_message=content
            ):
                if event.content.parts and event.content.parts[0].text:
                    agent_response = event.content.parts[0].text

            print(f"     Agent: {agent_response}")

        print()
        print("✓ Agent execution complete")
        print("  View in UI: http://localhost:5173")
        print()

        await processor.shutdown_async()

    except ImportError:
        print("❌ agentevals streaming not installed")
        print("   Install with: pip install -e .")
        print()
        print("Running agent WITHOUT streaming:")
        print()

        app_name = "dice_agent_app"
        user_id = "demo_user"
        runner = InMemoryRunner(agent=dice_agent, app_name=app_name)
        session = await runner.session_service.create_session(
            app_name=app_name, user_id=user_id
        )

        content = types.Content(
            role="user", parts=[types.Part.from_text(text="Roll a 6-sided die")]
        )

        async for event in runner.run_async(
            user_id=user_id, session_id=session.id, new_message=content
        ):
            if event.content.parts and event.content.parts[0].text:
                print(f"Agent: {event.content.parts[0].text}")

    except Exception as e:
        print(f"❌ Error: {e}")
        print()
        print("Make sure agentevals dev server is running:")
        print("  $ agentevals serve --dev --port 8001")
        print()


if __name__ == "__main__":
    asyncio.run(main())
