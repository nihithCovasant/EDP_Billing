"""
Search query generation node with remote configuration support.
Extracts search terms from user input - customize this for your retrieval needs.
"""

from typing import Dict, Any
from langchain_core.messages import HumanMessage

from src.config.agent_config import get_node_configuration
from src.utils.llm_provider import get_llm_model, get_provider_from_model
from src.utils.langfuse_decorator import trace_node
from cams_otel_lib import Logger as logger, otel_trace

# FEATURE:prometheus
try:
    from src.utils.metrics import track_node_metrics, get_metrics_collector
    _METRICS_AVAILABLE = True
except ImportError:
    def track_node_metrics(name):
        def decorator(func): return func
        return decorator
    _METRICS_AVAILABLE = False


class QueryProcessorNode:
    """
    Node that generates search queries from user input.

    Supports node-level configuration from remote registry API:
    - Model selection and parameters
    - Prompt templates
    - Temperature and other LLM settings
    - Multi-provider support (OpenAI, Anthropic, Google)
    """

    @otel_trace
    def __init__(self, config: Dict[str, Any], tenant_id: str = "default"):
        """Initialize search query node with tenant-aware configuration."""
        self.global_config = config
        self.tenant_id = tenant_id

        # Get node-specific configuration
        self.node_config = get_node_configuration("search_query", tenant_id, config)

        # Get tenant-specific config
        tenant_config = config.get(tenant_id, config.get("default", {}))

        # Get LLM configuration
        llm_config = tenant_config.get("llm_config", {}).get("retrieval", {})
        self.model = llm_config.get("model", "gpt-4o-mini")
        self.temperature = llm_config.get("temperature", 0.1)
        self.max_tokens = llm_config.get("max_tokens")  # None → provider default
        self.provider = llm_config.get("provider")

        # Infer provider from model name if not specified
        if not self.provider:
            try:
                self.provider = get_provider_from_model(self.model)
            except ValueError:
                logger.warning(f"Could not infer provider for model {self.model}, defaulting to openai")
                self.provider = "openai"

        logger.debug(f"SearchQueryNode initialized for tenant {tenant_id} with provider={self.provider}, model={self.model}")

    # FEATURE:prometheus
    @track_node_metrics("query_processor")
    @trace_node(
        node_name="query_processor",
        capture_input=True,
        capture_output=True,
        estimate_tokens=True,
        calculate_cost=True,
        metadata_fn=lambda state: {
            "num_messages": len(state.get("messages", [])),
            "tenant_id": state.get("tenant_id", "default"),
        },
    )
    @otel_trace
    async def execute(self, state: Dict[str, Any]) -> Dict[str, Any]:
        """
        Extract search query from user input.

        Customize this method to:
        - Change how queries are extracted
        - Add query preprocessing steps
        - Implement different extraction strategies
        """
        messages = state.get("messages", [])
        if not messages:
            return {"search_query": "", "needs_retrieval": False}

        # Get the last user message
        last_message = messages[-1]
        user_query = (
            last_message.content
            if hasattr(last_message, "content")
            else str(last_message)
        )

        # Get tenant-specific config
        tenant_config = self.global_config.get(
            state.get("tenant_id", "default"), self.global_config.get("default", {})
        )

        # Get prompt template
        prompt_template = tenant_config.get("prompts", {}).get(
            "retrieval",
            "Extract 2-5 specific search terms from this query:\n\n{question}",
        )

        # Format conversation history for context
        conversation_context = "\n".join(
            [
                f"{msg.type}: {msg.content}"
                for msg in messages[-5:]  # Last 5 messages
                if hasattr(msg, "content")
            ]
        )

        # Format prompt
        prompt = prompt_template.format(
            question=user_query, conversation=conversation_context
        )

        try:
            # Get custom headers from state for LiteLLM gateway
            custom_headers = state.get("litellm_headers")

            # Create LLM instance with custom headers
            llm = get_llm_model(
                provider=self.provider,
                model_name=self.model,
                temperature=self.temperature,
                streaming=False,
                max_tokens=self.max_tokens,
                custom_headers=custom_headers,
            )

            # Generate search query (tracing handled automatically by decorator)
            import time as _time
            _llm_start = _time.time()
            response = await llm.ainvoke([HumanMessage(content=prompt)])
            _llm_duration = _time.time() - _llm_start
            search_query = response.content.strip()

            # FEATURE:prometheus
            if _METRICS_AVAILABLE:
                _mc = get_metrics_collector()
                _mc.track_llm_duration(self.provider, self.model, _llm_duration)
                _usage = getattr(response, "usage_metadata", None) or {}
                _in = _usage.get("input_tokens", 0)
                _out = _usage.get("output_tokens", 0)
                if _in or _out:
                    try:
                        from src.utils.cost_calculator import calculate_cost
                        _cost = calculate_cost(self.model, _in, _out)
                    except Exception:
                        _cost = 0.0
                    _mc.track_llm_tokens(self.provider, self.model, _in, _out, _cost, state.get("tenant_id", "default"))

            logger.info(f"Generated search query: {search_query}")

            return {"search_query": search_query, "needs_retrieval": bool(search_query)}

        except Exception as e:
            logger.error(f"Error generating search query: {e}")
            # Fallback to using the original query
            return {"search_query": user_query, "needs_retrieval": True}
