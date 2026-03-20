"""LangChain agent with dice rolling and prime checking tools."""

import random

from langchain_core.tools import tool
from langchain_openai import ChatOpenAI


@tool
def roll_die(sides: int = 6) -> dict:
    """Roll a die with a specified number of sides.

    Args:
        sides: Number of sides on the die (default: 6)

    Returns:
        Dictionary with sides, result, and message
    """
    if sides < 2:
        return {"sides": sides, "result": None, "message": "Error: Die must have at least 2 sides"}

    result = random.randint(1, sides)
    return {"sides": sides, "result": result, "message": f"Rolled a {result} on a {sides}-sided die"}


@tool
def check_prime(nums: list[int]) -> dict:
    """Check if numbers are prime.

    Args:
        nums: List of integers to check

    Returns:
        Dictionary with results, prime_count, and prime_numbers
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

    results = {n: is_prime(n) for n in nums}
    prime_numbers = [n for n, is_p in results.items() if is_p]

    return {"results": results, "prime_count": len(prime_numbers), "prime_numbers": prime_numbers}


# def create_dice_agent(model: str = "gpt-3.5-turbo", temperature: float = 0.0):
def create_dice_agent(model: str = "gpt-4o-mini", temperature: float = 0.0):
    llm = ChatOpenAI(model=model, temperature=temperature)
    tools = [roll_die, check_prime]

    llm_with_tools = llm.bind_tools(tools)

    return llm_with_tools, tools
