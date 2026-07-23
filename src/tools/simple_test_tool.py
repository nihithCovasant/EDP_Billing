"""
Simple demo tools using LangChain @tool decorator.
Auto-discovered by the tool registry.
"""

import ast
import operator

from langchain_core.tools import tool

# Whitelisted operators only — no attribute access, no calls, no
# subscripting, no name lookups. This is the entire surface a safe
# arithmetic evaluator needs; anything else raises before evaluation.
_ALLOWED_BINOPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_ALLOWED_UNARYOPS = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}


def _safe_eval_arithmetic(node: ast.AST):
    """Recursively evaluate a parsed arithmetic-only AST node.

    Deliberately does NOT use eval()/exec() — a model-supplied string
    should never be handed to Python's interpreter directly, since that
    lets a crafted expression (or a prompt-injected one) execute arbitrary
    code (e.g. via __import__, __class__ traversal, etc.) with the
    process's own privileges. Only numeric literals and +-*/%** are
    supported; anything else (names, calls, attributes, subscripts,
    comprehensions, ...) is rejected before it can be evaluated.
    """
    if isinstance(node, ast.Constant):
        if isinstance(node.value, (int, float)):
            return node.value
        raise ValueError(f"Unsupported constant: {node.value!r}")
    if isinstance(node, ast.BinOp) and type(node.op) in _ALLOWED_BINOPS:
        return _ALLOWED_BINOPS[type(node.op)](
            _safe_eval_arithmetic(node.left), _safe_eval_arithmetic(node.right)
        )
    if isinstance(node, ast.UnaryOp) and type(node.op) in _ALLOWED_UNARYOPS:
        return _ALLOWED_UNARYOPS[type(node.op)](_safe_eval_arithmetic(node.operand))
    raise ValueError(f"Unsupported expression element: {type(node).__name__}")


@tool
def simple_calculator(expression: str) -> str:
    """Performs basic arithmetic operations. Input should be a mathematical expression like '2 + 2' or '10 * 5'."""
    try:
        parsed = ast.parse(expression, mode="eval")
        result = _safe_eval_arithmetic(parsed.body)
        return f"Result: {result}"
    except Exception as e:
        return f"Error evaluating expression: {e}"


@tool
def text_counter(text: str) -> str:
    """Counts the number of characters and words in the provided text."""
    char_count = len(text)
    word_count = len(text.split())
    return f"Characters: {char_count}, Words: {word_count}"
