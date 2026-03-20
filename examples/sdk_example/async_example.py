"""Async context manager for ADK and other async agents.

Use session_async() when your agent code is async. This avoids the
background thread used by the sync context manager.

Prerequisites:
    1. Start agentevals dev server:
       $ agentevals serve --dev --port 8001

    2. Set your API key:
       $ export GOOGLE_API_KEY="your-key-here"

Usage:
    $ python examples/sdk_example/async_example.py
"""

import asyncio
import logging
from pathlib import Path

logging.basicConfig(level=logging.INFO)

# Import the dice_agent from the sibling example directory.
# In a real project this would be a normal package import.
import importlib.util

from dotenv import load_dotenv
from google.adk.runners import InMemoryRunner
from google.genai import types

_agent_path = Path(__file__).resolve().parent.parent / "dice_agent" / "agent.py"
_spec = importlib.util.spec_from_file_location("dice_agent_module", _agent_path)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
dice_agent = _mod.dice_agent

from agentevals import AgentEvals

load_dotenv(override=True)

app = AgentEvals()


async def main():
    async with app.session_async(
        eval_set_id="sdk-async-demo",
        metadata={"model": dice_agent.model},
    ):
        runner = InMemoryRunner(agent=dice_agent, app_name="dice_app")
        session = await runner.session_service.create_session(app_name="dice_app", user_id="demo_user")

        for query in ["Roll a 20-sided die", "Is that number prime?"]:
            print(f"User: {query}")
            content = types.Content(role="user", parts=[types.Part.from_text(text=query)])
            async for event in runner.run_async(user_id="demo_user", session_id=session.id, new_message=content):
                if event.content.parts and event.content.parts[0].text:
                    print(f"Agent: {event.content.parts[0].text}")


if __name__ == "__main__":
    asyncio.run(main())
