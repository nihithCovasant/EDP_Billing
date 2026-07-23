"""
Cost calculation utilities for LLM usage tracking in Langfuse.

Provides pricing data and calculation functions for major LLM providers
to track and monitor agent execution costs.
"""

from cams_otel_lib import Logger as logger
from cams_otel_lib import otel_trace

# Pricing per 1M tokens (USD) - Updated as of January 2026
# Source: Provider pricing pages
MODEL_PRICING = {
    # OpenAI Models
    "gpt-4o": {"input": 2.50, "output": 10.00},
    "gpt-4o-mini": {"input": 0.15, "output": 0.60},
    "gpt-4-turbo": {"input": 10.00, "output": 30.00},
    "gpt-4-turbo-preview": {"input": 10.00, "output": 30.00},
    "gpt-4": {"input": 30.00, "output": 60.00},
    "gpt-3.5-turbo": {"input": 0.50, "output": 1.50},
    "gpt-3.5-turbo-16k": {"input": 3.00, "output": 4.00},
    "o1-preview": {"input": 15.00, "output": 60.00},
    "o1-mini": {"input": 3.00, "output": 12.00},
    # Anthropic Claude Models
    "claude-3-5-sonnet-20241022": {"input": 3.00, "output": 15.00},
    "claude-3-5-sonnet-20240620": {"input": 3.00, "output": 15.00},
    "claude-3-5-haiku-20241022": {"input": 0.80, "output": 4.00},
    "claude-3-opus-20240229": {"input": 15.00, "output": 75.00},
    "claude-3-sonnet-20240229": {"input": 3.00, "output": 15.00},
    "claude-3-haiku-20240307": {"input": 0.25, "output": 1.25},
    "claude-3-5-sonnet": {"input": 3.00, "output": 15.00},  # Alias
    "claude-3-opus": {"input": 15.00, "output": 75.00},  # Alias
    "claude-3-sonnet": {"input": 3.00, "output": 15.00},  # Alias
    "claude-3-haiku": {"input": 0.25, "output": 1.25},  # Alias
    # Google Gemini Models
    "gemini-2.0-flash": {"input": 0.075, "output": 0.30},
    "gemini-2.0-flash-exp": {"input": 0.00, "output": 0.00},
    "gemini-1.5-flash": {"input": 0.075, "output": 0.30},
    "gemini-1.5-flash-8b": {"input": 0.0375, "output": 0.15},
    "gemini-1.5-pro": {"input": 1.25, "output": 5.00},
    "gemini-2.5-pro": {"input": 1.25, "output": 5.00},
    "gemini-pro": {"input": 0.50, "output": 1.50},
    # Google Vertex AI - Claude Models
    "claude-3-5-sonnet@20240620": {"input": 3.00, "output": 15.00},
    "claude-3-5-haiku@20241022": {"input": 0.80, "output": 4.00},
    "claude-3-opus@20240229": {"input": 15.00, "output": 75.00},
    "claude-3-sonnet@20240229": {"input": 3.00, "output": 15.00},
    "claude-3-haiku@20240307": {"input": 0.25, "output": 1.25},
}


@otel_trace
def calculate_cost_details(
    model_name: str,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
) -> dict[str, float] | None:
    """
    Calculate cost details for Langfuse tracking.

    Args:
        model_name: The name/identifier of the LLM model
        input_tokens: Number of input/prompt tokens
        output_tokens: Number of output/completion tokens

    Returns:
        Dictionary with cost breakdown: {"input": float, "output": float, "total": float}
        All values in USD. Returns None if pricing not available or tokens missing.

    Example:
        >>> cost = calculate_cost_details("gpt-4o", 1000, 500)
        >>> print(f"Total cost: ${cost['total']:.4f}")
        Total cost: $0.0075
    """
    if input_tokens is None or output_tokens is None:
        logger.debug(f"Cannot calculate cost for {model_name}: token counts missing")
        return None

    # Normalize model name for lookup
    normalized_model = model_name.lower().strip()
    pricing = MODEL_PRICING.get(normalized_model)

    # Try partial match if exact match fails
    if not pricing:
        for model_key in MODEL_PRICING:
            if model_key in normalized_model or normalized_model in model_key:
                pricing = MODEL_PRICING[model_key]
                logger.debug(f"Matched model '{model_name}' to pricing key '{model_key}'")
                break

    if not pricing:
        logger.warning(f"No pricing data available for model: {model_name}")
        return None

    # Calculate cost: (tokens / 1,000,000) * price_per_million
    input_cost = (input_tokens / 1_000_000) * pricing["input"]
    output_cost = (output_tokens / 1_000_000) * pricing["output"]
    total_cost = input_cost + output_cost

    return {
        "input": round(input_cost, 6),
        "output": round(output_cost, 6),
        "total": round(total_cost, 6),
    }


@otel_trace
def get_model_pricing(model_name: str) -> dict[str, float] | None:
    """
    Get pricing information for a specific model.

    Args:
        model_name: The name/identifier of the LLM model

    Returns:
        Dictionary with pricing per 1M tokens: {"input": float, "output": float}
        Returns None if model not found.

    Example:
        >>> pricing = get_model_pricing("gpt-4o")
        >>> print(f"Input: ${pricing['input']}/1M tokens")
        Input: $2.50/1M tokens
    """
    normalized_model = model_name.lower().strip()
    pricing = MODEL_PRICING.get(normalized_model)

    if not pricing:
        # Try partial match
        for model_key in MODEL_PRICING:
            if model_key in normalized_model or normalized_model in model_key:
                return MODEL_PRICING[model_key]

    return pricing


@otel_trace
def add_custom_model_pricing(model_name: str, input_price: float, output_price: float) -> None:
    """
    Add custom pricing for a model not in the default pricing table.

    Useful for custom/fine-tuned models or new models not yet in the table.

    Args:
        model_name: The name/identifier of the model
        input_price: Cost per 1M input tokens (USD)
        output_price: Cost per 1M output tokens (USD)

    Example:
        >>> add_custom_model_pricing("my-custom-gpt4", 5.0, 15.0)
        >>> cost = calculate_cost_details("my-custom-gpt4", 1000, 500)
    """
    MODEL_PRICING[model_name.lower().strip()] = {
        "input": input_price,
        "output": output_price,
    }
    logger.info(f"Added custom pricing for model '{model_name}': ${input_price}/1M input, ${output_price}/1M output")
