"""Strands dice agent with roll_die and check_prime tools."""

import random

from strands import Agent, tool
from strands.models.openai import OpenAIModel


@tool
def roll_die(sides: int = 6) -> dict:
    """Roll a die with the specified number of sides.

    Args:
        sides: Number of sides on the die

    Returns:
        Dictionary with the roll result and a message
    """
    result = random.randint(1, sides)
    return {
        "sides": sides,
        "result": result,
        "message": f"Rolled a {sides}-sided die and got {result}",
    }


@tool
def check_prime(nums: list[int]) -> dict:
    """Check if numbers are prime.

    Args:
        nums: List of numbers to check

    Returns:
        Dictionary with primality results and list of prime numbers
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
    prime_numbers = [n for n, is_p in results.items() if is_p]
    return {
        "results": results,
        "prime_numbers": prime_numbers,
    }


def create_dice_agent(model_id: str = "gpt-4o"):
    return Agent(
        model=OpenAIModel(model_id=model_id),
        tools=[roll_die, check_prime],
        system_prompt=(
            "You are a helpful assistant that can roll dice and check if numbers are prime. "
            "When asked to roll a die, use the roll_die tool with the appropriate number of sides. "
            "When asked about prime numbers, use the check_prime tool."
        ),
        name="dice_agent",
    )
