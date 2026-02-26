# Dice Agent - Live Streaming Example

A simple ADK agent that demonstrates live streaming evaluation with agentevals.

## What This Does

This agent can:
- Roll dice with any number of sides
- Check if numbers are prime
- Stream traces in real-time to agentevals dev server
- Get instant evaluation feedback

## Quick Start

### 1. Set up your API key

```bash
export GOOGLE_API_KEY="your-google-api-key"
```

### 2. Start agentevals dev server (Terminal 1)

```bash
cd /path/to/agentevals
agentevals serve --dev --port 8001
```

### 3. Start the UI (Terminal 2, optional)

```bash
cd /path/to/agentevals/ui
npm run dev
```

Open http://localhost:5173 and click "I am developing an agent" to see the streaming view.

### 4. Run the agent (Terminal 3)

```bash
cd /path/to/agentevals
python examples/dice_agent/main.py
```

## Iterate and Experiment

Try making changes and re-running to see how evaluations change:

### Change 1: Switch Models

Edit `agent.py` line 48:
```python
dice_agent = Agent(
    name="dice_agent",
    model="gemini-2.0-flash-thinking-exp-01-21",  # Try different models!
    instruction=...,
    tools=[roll_die, check_prime],
)
```

Re-run:
```bash
python examples/dice_agent/main.py
```

Watch in the UI:
- New session appears with model name in the session ID
- Compare tool calling behavior between models
- See if evaluation scores differ

### Change 2: Modify Instructions

Edit `agent.py`:
```python
dice_agent = Agent(
    name="dice_agent",
    model="gemini-2.5-flash",
    instruction="""You are a mathematical assistant specializing in dice and prime numbers.

Always explain your reasoning when checking prime numbers.
Use the tools provided to give accurate results.""",
    tools=[roll_die, check_prime],
)
```

### Change 3: Add More Tools

Add a new tool in `agent.py`:
```python
def roll_multiple(count: int, sides: int = 6) -> dict:
    """Roll multiple dice at once."""
    results = [random.randint(1, sides) for _ in range(count)]
    return {
        "count": count,
        "sides": sides,
        "results": results,
        "total": sum(results),
        "average": sum(results) / count
    }

dice_agent = Agent(
    name="dice_agent",
    model="gemini-2.5-flash",
    instruction=...,
    tools=[roll_die, roll_multiple, check_prime],  # Add new tool
)
```

Update `main.py` to test the new functionality.

## What You'll See

### In Terminal
```
🎲 Dice Agent - Live Streaming Example
==================================================

✓ Connected to agentevals dev server
  Session: dice-agent-gemini-2.5-flash
  Model: gemini-2.5-flash
  View live: http://localhost:5173

[1/3] User: Hi! Can you help me?
     Agent: Hello! I can help you roll dice and check prime numbers...

[2/3] User: Roll a 20-sided die for me
     Agent: I rolled a 20-sided die and got 13

[3/3] User: Is the number you rolled prime?
     Agent: Yes, 13 is a prime number!

✓ Agent execution complete
  Waiting for evaluation results...

⚡ Evaluation results:
  ✓ tool_trajectory_avg_score: 1.0
```

### In Browser (Live Streaming View)

**Before running agent:**
- Click "I am developing an agent" on welcome screen
- See "No active sessions" message

**While agent runs:**
- Session card appears immediately with status "ACTIVE"
- Span count increments in real-time as agent executes
- See eval set: "dice_agent_eval"

**After agent completes:**
- Status changes to "EVALUATED"
- Evaluation results appear as colored badges
- Each metric shows: name and score (e.g., "tool_trajectory_avg_score: 1.00")

**Multiple runs:**
- Each run creates a new session with model name in ID
- Compare sessions side-by-side
- See how different models affect span counts and scores

## Files

- `agent.py` - Agent definition with tools
- `main.py` - Main script with streaming setup
- `eval_set.json` - Evaluation cases for trajectory checking
- `README.md` - This file

## Tips

1. **Keep dev server running** - Leave it running across multiple agent runs
2. **Watch the UI** - See how different models/prompts affect the trace structure
3. **Check evaluations** - Use `tool_trajectory_avg_score` to measure correctness
4. **Iterate quickly** - No need to restart anything except the agent script

## Troubleshooting

**"Connection refused"**
- Make sure dev server is running: `agentevals serve --dev --port 8001`

**"GOOGLE_API_KEY not set"**
- Export your API key: `export GOOGLE_API_KEY="..."`

**"Module not found: agentevals"**
- Install agentevals: `pip install -e /path/to/agentevals`

**No evaluation results**
- The eval set needs to match agent behavior
- Check `eval_set.json` - it expects `roll_die` and `check_prime` to be called
