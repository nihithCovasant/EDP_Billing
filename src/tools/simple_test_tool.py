"""
Simple demo tools using LangChain @tool decorator.
Auto-discovered by the tool registry.
"""

from langchain_core.tools import tool


@tool
def simple_calculator(expression: str) -> str:
    """Performs basic arithmetic operations. Input should be a mathematical expression like '2 + 2' or '10 * 5'."""
    try:
        result = eval(expression, {"__builtins__": {}}, {})
        return f"Result: {result}"
    except Exception as e:
        return f"Error evaluating expression: {e}"


@tool
def text_counter(text: str) -> str:
    """Counts the number of characters and words in the provided text."""
    char_count = len(text)
    word_count = len(text.split())
    return f"Characters: {char_count}, Words: {word_count}"
