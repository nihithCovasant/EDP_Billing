"""
Langfuse tracing decorator for LangGraph nodes.

This module is CORE — it is always included regardless of feature selection.
When the 'langfuse' feature is selected, observability.py is present and full
tracing is active.  When it is not selected, every trace_node call is a
transparent no-op: the decorated function runs unchanged with zero overhead
and zero import errors.
"""

import functools
import inspect
from collections.abc import Callable
from contextlib import contextmanager
from typing import Any

from cams_otel_lib import Logger as logger
from cams_otel_lib import otel_trace


def trace_node(
    node_name: str,
    capture_input: bool = True,
    capture_output: bool = True,
    estimate_tokens: bool = False,
    calculate_cost: bool = False,
    metadata_fn: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    llm_attr_name: str = "llm",
):
    """
    Decorator for tracing node execution in Langfuse.

    Gracefully degrades to a no-op when:
    - The 'langfuse' feature was not selected (observability.py absent), or
    - The langfuse SDK is not installed, or
    - Langfuse credentials are not configured.

    Args:
        node_name: Name of the node shown in Langfuse traces.
        capture_input: Capture state input fields in the span.
        capture_output: Capture result output fields in the span.
        estimate_tokens: Auto-wrap LLM calls to count tokens.
        calculate_cost: Calculate USD cost (requires estimate_tokens=True).
        metadata_fn: Optional callable ``fn(state) -> dict`` for extra metadata.
        llm_attr_name: Attribute name of the LLM on ``self`` (default "llm").
    """

    def decorator(func: Callable):

        @functools.wraps(func)
        async def async_wrapper(self, state: dict[str, Any], *args, **kwargs):
            logger.debug(f"Executing node: {node_name}")

            # --- Try to obtain observability client ----------------------------
            trace_id = state.get("_langfuse_trace_id")
            parent_span_id = state.get("_langfuse_span_id")
            span = None
            client = None
            original_llm = None

            try:
                if trace_id and parent_span_id:
                    from src.utils.observability import get_observability_manager

                    obs_manager = get_observability_manager()
                    client = obs_manager.client if obs_manager.enabled else None
            except ImportError:
                # observability.py not present (langfuse feature not selected)
                client = None
            except Exception as exc:
                logger.debug(f"Could not obtain observability manager: {exc}")
                client = None

            # --- Create span if we have a live client -------------------------
            if client and trace_id and parent_span_id:
                try:
                    input_data = {}
                    if capture_input:
                        messages = state.get("messages", [])
                        if messages:
                            last_msg = messages[-1]
                            user_query = last_msg.content if hasattr(last_msg, "content") else str(last_msg)
                            input_data["user_query"] = user_query[:500]
                            input_data["messages_count"] = len(messages)
                        if state.get("search_query"):
                            input_data["search_query"] = state["search_query"][:500]
                        if state.get("retrieved_context"):
                            ctx = state["retrieved_context"]
                            input_data["retrieved_context"] = ctx[:1000] + ("..." if len(ctx) > 1000 else "")
                            input_data["context_length"] = len(ctx)
                        input_data = {k: v for k, v in input_data.items() if v is not None}

                    metadata = {"tenant_id": state.get("tenant_id", "default")}
                    if metadata_fn:
                        try:
                            extra = metadata_fn(state)
                            if extra:
                                metadata.update(extra)
                        except Exception as e:
                            logger.warning(f"metadata_fn error for {node_name}: {e}")

                    span = client.span(
                        trace_id=trace_id,
                        parent_observation_id=parent_span_id,
                        name=f"node.{node_name}",
                        input=input_data,
                        metadata=metadata,
                    )
                    logger.debug(f"Created Langfuse span for node: {node_name}")

                    # Optionally wrap LLM attribute to auto-trace generations
                    if estimate_tokens and hasattr(self, llm_attr_name):
                        llm_obj = getattr(self, llm_attr_name)
                        if llm_obj:
                            original_llm = llm_obj

                            class LLMWrapper:
                                def __init__(self, llm, parent_span, node, state_dict):
                                    self._llm = llm
                                    self._span = parent_span
                                    self._node = node
                                    self._state = state_dict

                                async def ainvoke(self, messages, *args, **kw):
                                    prompt_content = (
                                        messages[0].content
                                        if (isinstance(messages, list) and messages and hasattr(messages[0], "content"))
                                        else str(messages)
                                    )
                                    response = await self._llm.ainvoke(messages, *args, **kw)
                                    response_content = (
                                        response.content if hasattr(response, "content") else str(response)
                                    )
                                    if self._span:
                                        try:
                                            from src.utils.token_estimator import estimate_usage

                                            model_name = getattr(self._llm, "model_name", "gpt-4o-mini")
                                            usage = estimate_usage(prompt_content, response_content)
                                            create_generation_trace(
                                                parent=self._span,
                                                name=f"{self._node}-generation",
                                                model=model_name,
                                                input_data={"prompt": prompt_content[:1000]},
                                                output_data={"response": response_content[:1000]},
                                                metadata={
                                                    "tenant_id": self._state.get("tenant_id", "default"),
                                                    "node": self._node,
                                                },
                                                usage=usage,
                                            )
                                        except Exception as e:
                                            logger.debug(f"Generation trace failed: {e}")
                                    return response

                                def __getattr__(self, name):
                                    return getattr(self._llm, name)

                            setattr(self, llm_attr_name, LLMWrapper(original_llm, span, node_name, state))

                except Exception as e:
                    logger.debug(f"Span creation failed for {node_name}: {e}")
                    span = None
                    client = None

            else:
                logger.debug(
                    f"No Langfuse span for {node_name}: "
                    f"client={bool(client)}, trace_id={bool(trace_id)}, parent_span_id={bool(parent_span_id)}"
                )

            # --- Execute the actual node --------------------------------------
            try:
                result = await func(self, state, *args, **kwargs)

                if span and capture_output:
                    try:
                        output_data = {}
                        if result.get("search_query"):
                            output_data["search_query"] = result["search_query"][:500]
                        if result.get("retrieved_context"):
                            ctx = result["retrieved_context"]
                            output_data["retrieved_context"] = ctx[:1000] + ("..." if len(ctx) > 1000 else "")
                            output_data["context_length"] = len(ctx)
                        if result.get("final_response"):
                            resp = result["final_response"]
                            output_data["final_response"] = resp[:1000] + ("..." if len(resp) > 1000 else "")
                            output_data["response_length"] = len(resp)
                        output_data = {k: v for k, v in output_data.items() if v}
                        if output_data:
                            span.update(output=output_data)
                    except Exception as e:
                        logger.debug(f"Span output update failed: {e}")

                return result

            except Exception as e:
                logger.error(f"Error in node {node_name}: {e}", exc_info=True)
                if span:
                    try:
                        span.update(level="ERROR", status_message=str(e))
                    except Exception:
                        pass
                raise

            finally:
                if original_llm is not None:
                    setattr(self, llm_attr_name, original_llm)
                if span:
                    try:
                        span.end()
                    except Exception:
                        pass
                if client:
                    try:
                        client.flush()
                    except Exception as e:
                        logger.debug(f"Flush failed after {node_name}: {e}")

        @functools.wraps(func)
        def sync_wrapper(self, state: dict[str, Any], *args, **kwargs):
            logger.debug(f"Executing node (sync): {node_name}")
            parent_span = state.get("_langfuse_parent_span")
            span = None

            if parent_span:
                try:
                    input_data = {}
                    if capture_input:
                        input_data = {
                            "messages_count": len(state.get("messages", [])),
                        }
                        if state.get("search_query"):
                            input_data["search_query"] = state["search_query"][:200]
                        input_data = {k: v for k, v in input_data.items() if v is not None}

                    metadata = {"tenant_id": state.get("tenant_id", "default")}
                    if metadata_fn:
                        try:
                            extra = metadata_fn(state)
                            if extra:
                                metadata.update(extra)
                        except Exception as e:
                            logger.warning(f"metadata_fn error for {node_name}: {e}")

                    span = parent_span.start_observation(
                        as_type="span",
                        name=f"node.{node_name}",
                        input=input_data,
                        metadata=metadata,
                    )
                except Exception as e:
                    logger.debug(f"Sync span creation failed for {node_name}: {e}")

            try:
                result = func(self, state, *args, **kwargs)
                if span and capture_output:
                    try:
                        output_data = {
                            k: v
                            for k, v in {
                                "search_query": (
                                    result.get("search_query", "")[:200] if result.get("search_query") else None
                                ),
                                "context_length": len(result.get("retrieved_context", ""))
                                if result.get("retrieved_context")
                                else 0,
                            }.items()
                            if v
                        }
                        if output_data:
                            span.update(output=output_data)
                    except Exception:
                        pass
                return result
            except Exception as e:
                logger.error(f"Error in node {node_name}: {e}", exc_info=True)
                if span:
                    try:
                        span.update(level="ERROR", status_message=str(e))
                    except Exception:
                        pass
                raise
            finally:
                if span:
                    try:
                        span.end()
                    except Exception:
                        pass

        return async_wrapper if inspect.iscoroutinefunction(func) else sync_wrapper

    return decorator


@otel_trace
def create_generation_trace(
    parent,
    name: str,
    model: str,
    input_data: Any,
    output_data: Any,
    metadata: dict[str, Any] | None = None,
    model_parameters: dict[str, Any] | None = None,
    usage: dict[str, int] | None = None,
):
    """
    Create a generation observation within an existing Langfuse span.

    Returns None silently if parent is None or if any Langfuse call fails.
    """
    if not parent:
        return None

    try:
        from src.utils.cost_calculator import calculate_cost_details

        generation_metadata = metadata or {}
        if model_parameters:
            generation_metadata["model_parameters"] = model_parameters

        generation = parent.start_observation(
            as_type="generation",
            name=name,
            model=model,
            input=input_data,
            metadata=generation_metadata,
        )

        update_params: dict[str, Any] = {"output": output_data}

        if usage:
            input_tokens = usage.get("input_tokens")
            output_tokens = usage.get("output_tokens")
            total_tokens = usage.get("total_tokens")
            if total_tokens is None and input_tokens and output_tokens:
                total_tokens = input_tokens + output_tokens

            usage_details: dict[str, int] = {}
            if input_tokens is not None:
                usage_details["input"] = input_tokens
            if output_tokens is not None:
                usage_details["output"] = output_tokens
            if total_tokens is not None:
                usage_details["total"] = total_tokens

            if usage_details:
                update_params["usage_details"] = usage_details

            cost_details = calculate_cost_details(model, input_tokens, output_tokens)
            if cost_details:
                update_params["cost_details"] = cost_details

        generation.update(**update_params)
        generation.end()
        return generation

    except ImportError:
        logger.debug("cost_calculator not available, skipping cost calculation")
        return None
    except Exception as e:
        logger.error(f"Failed to create generation trace for {name}: {e}", exc_info=True)
        return None


@contextmanager
def trace_generation(
    observability,
    model_name: str,
    prompt: str,
    trace_id: str | None = None,
    parent_span_id: str | None = None,
    metadata: dict[str, Any] | None = None,
):
    """
    Context manager for tracing an individual LLM call.

    Yields (span, completion_callback).  Both are no-ops when observability
    is disabled or unavailable.

    Example::

        with trace_generation(self.observability, "gpt-4o", prompt) as (span, complete):
            response = await llm.ainvoke(prompt)
            complete(response.content)
    """
    if not observability or not observability.enabled:
        yield (None, lambda x: None)
        return

    import uuid

    if not trace_id:
        trace_id = uuid.uuid4().hex

    try:
        generation_span = observability.client.generation(
            name=f"llm_call_{model_name}",
            trace_id=trace_id,
            parent_observation_id=parent_span_id,
            model=model_name,
            input={"prompt": prompt[:500]},
            metadata=metadata or {},
        )
    except Exception as e:
        logger.debug(f"Failed to start generation span: {e}")
        yield (None, lambda x: None)
        return

    completion: dict[str, Any] = {"text": None}

    def set_completion(response_text: str):
        completion["text"] = response_text
        try:
            from src.utils.cost_calculator import calculate_cost_details
            from src.utils.token_estimator import estimate_usage

            usage = estimate_usage(prompt, response_text)
            cost = calculate_cost_details(
                model_name=model_name,
                input_tokens=usage.get("input_tokens"),
                output_tokens=usage.get("output_tokens"),
            )
            generation_span.end(
                output={"completion": response_text[:500]},
                usage=usage,
                calculated_costs=cost,
            )
        except Exception as e:
            logger.debug(f"Error ending generation span: {e}")
            try:
                generation_span.end(output={"completion": response_text[:500]})
            except Exception:
                pass

    try:
        yield (generation_span, set_completion)
    except Exception as e:
        try:
            generation_span.end(level="ERROR", status_message=str(e))
        except Exception:
            pass
        raise
