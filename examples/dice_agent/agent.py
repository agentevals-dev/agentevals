"""Dice rolling agent with tool calling capabilities.

A simple agent that can roll dice and check if numbers are prime.
Perfect for testing live streaming and iterating on agent behavior.
"""

import random

from google.adk import Agent


def roll_die(sides: int = 6) -> dict:
    """Roll a die with the specified number of sides.

    Args:
        sides: Number of sides on the die (default: 6)

    Returns:
        Dictionary with the roll result
    """
    if sides < 2:
        return {"error": "Die must have at least 2 sides"}

    result = random.randint(1, sides)
    return {"sides": sides, "result": result, "message": f"Rolled a {sides}-sided die and got {result}"}


def check_prime(nums: list[int]) -> dict:
    """Check if numbers are prime.

    Args:
        nums: List of numbers to check

    Returns:
        Dictionary mapping each number to whether it's prime
    """

    def is_prime(n: int) -> bool:
        if n < 2:
            return False
        if n == 2:
            return True
        if n % 2 == 0:
            return False
        for i in range(3, int(n**0.5) + 1, 2):
            if n % i == 0:
                return False
        return True

    results = {num: is_prime(num) for num in nums}
    prime_nums = [n for n, is_p in results.items() if is_p]

    return {"results": results, "prime_count": len(prime_nums), "prime_numbers": prime_nums}


dice_agent = Agent(
    name="dice_agent",
    # model="gemini-2.5-flash",
    model="gemini-2.5-flash-lite",
    instruction="""You are a helpful assistant that can roll dice and check if numbers are prime.

When a user asks you to roll a die, use the roll_die tool with the appropriate number of sides.
When a user asks about prime numbers, use the check_prime tool.

Be friendly and concise in your responses.""",
    tools=[roll_die, check_prime],
)
