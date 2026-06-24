"""
Token estimation for LLM usage tracking.

Provides utilities to estimate token counts when exact counts aren't available
from the LLM provider (e.g., during streaming or with certain APIs).
"""

import re
from typing import Dict

from cams_otel_lib import Logger as logger, otel_trace


@otel_trace
def estimate_tokens(text: str) -> int:
    """
    Estimate token count for text using combined heuristics.

    Uses both character-based and word-based estimation for better accuracy.

    Args:
        text: The text to estimate tokens for

    Returns:
        Estimated token count

    Note:
        This is an approximation. For accurate counts, use tiktoken or
        the model's native tokenizer.
    """
    if not text:
        return 0

    # Character-based estimate: ~4 characters per token (GPT standard)
    char_estimate = len(text) // 4

    # Word-based estimate: ~0.75 tokens per word
    words = re.findall(r"\b\w+\b", text)
    word_estimate = int(len(words) * 0.75)

    # Average both methods for better accuracy
    return max((char_estimate + word_estimate) // 2, 1)


@otel_trace
def estimate_usage(prompt: str, completion: str) -> Dict[str, int]:
    """
    Estimate token usage for Langfuse from prompt and completion text.

    Args:
        prompt: The input prompt/messages sent to the LLM
        completion: The LLM's response/completion

    Returns:
        Dictionary with keys: input_tokens, output_tokens, total_tokens

    Example:
        >>> usage = estimate_usage("What is AI?", "AI is artificial intelligence...")
        >>> print(usage)
        {'input_tokens': 3, 'output_tokens': 8, 'total_tokens': 11}
    """
    input_tokens = estimate_tokens(prompt)
    output_tokens = estimate_tokens(completion)

    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
    }


@otel_trace
def estimate_messages_tokens(messages: list) -> int:
    """
    Estimate tokens for a list of chat messages.

    Args:
        messages: List of message dicts with 'role' and 'content' keys

    Returns:
        Estimated total token count for all messages

    Example:
        >>> messages = [
        ...     {"role": "user", "content": "Hello"},
        ...     {"role": "assistant", "content": "Hi there!"}
        ... ]
        >>> estimate_messages_tokens(messages)
        8
    """
    total_text = ""

    for msg in messages:
        # Add role prefix (costs tokens)
        role = msg.get("role", "")
        content = msg.get("content", "")

        # Format similar to how models see it: <role>: <content>
        total_text += f"{role}: {content}\n"

    return estimate_tokens(total_text)
