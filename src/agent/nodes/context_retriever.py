"""
Retrieval node with remote configuration support.
Executes retrieval tools to gather context - customize for your knowledge sources.
"""

import asyncio
from typing import Dict, Any, List

from src.config.agent_config import get_node_configuration
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


class ContextRetrieverNode:
    """
    Node that retrieves context using configured tools.

    Supports node-level configuration from remote registry API:
    - Tool selection
    - Retrieval parameters (top_k, score_threshold)
    - Parallel tool execution
    """

    @otel_trace
    def __init__(
        self, config: Dict[str, Any], tools: List[Any], tenant_id: str = "default"
    ):
        """Initialize retrieval node with tenant-aware configuration."""
        self.global_config = config
        self.tools = tools
        self.tenant_id = tenant_id

        # Get node-specific configuration
        self.node_config = get_node_configuration("retrieval", tenant_id, config)

        logger.debug(f"RetrievalNode initialized for tenant {tenant_id} with {len(tools)} tools")

    # FEATURE:prometheus
    @track_node_metrics("context_retriever")
    @trace_node(
        node_name="context_retriever",
        capture_input=True,
        capture_output=True,
        estimate_tokens=False,  # No LLM calls in retrieval
        calculate_cost=False,
        metadata_fn=lambda state: {
            "search_query_length": len(state.get("search_query", "")),
            "num_tools": len(state.get("tools", [])),
            "tenant_id": state.get("tenant_id", "default"),
        },
    )
    @otel_trace
    async def execute(self, state: Dict[str, Any]) -> Dict[str, Any]:
        """
        Retrieve context using available tools.

        Customize this method to:
        - Add custom retrieval logic
        - Implement tool chaining
        - Add context filtering/ranking
        - Integrate different knowledge sources
        """
        search_query = state.get("search_query", "")

        if not search_query:
            logger.debug("No search query provided, skipping retrieval")
            return {"retrieved_context": ""}

        # Get tenant-specific retrieval config
        tenant_config = self.global_config.get(
            state.get("tenant_id", "default"), self.global_config.get("default", {})
        )
        retrieval_config = tenant_config.get("retrieval_config", {})

        top_k = retrieval_config.get("top_k", 5)
        score_threshold = retrieval_config.get("score_threshold", 0.7)

        logger.info(f"Retrieving context for query: {search_query}")

        # Execute retrieval (customize this for your tools)
        retrieved_context = await self._retrieve_context(
            search_query, top_k=top_k, score_threshold=score_threshold
        )

        logger.info(f"Retrieved context length: {len(retrieved_context)}")

        return {"retrieved_context": retrieved_context}

    @otel_trace
    async def _retrieve_context(
        self, query: str, top_k: int = 5, score_threshold: float = 0.7
    ) -> str:
        """
        Execute retrieval using available tools.

        Invokes all available tools with the query and combines results.
        """
        if not self.tools:
            logger.warning("No tools available for retrieval")
            return ""

        results = []

        # Execute each tool
        for tool in self.tools:
            tool_name = getattr(tool, "name", str(tool))
            try:
                logger.debug(f"Invoking tool: {tool_name}")

                import time as _time
                _tool_start = _time.time()
                result = await asyncio.to_thread(tool.invoke, query)
                _tool_duration = _time.time() - _tool_start

                # FEATURE:prometheus
                if _METRICS_AVAILABLE:
                    get_metrics_collector().track_tool_invocation(tool_name, _tool_duration, "success")

                if result:
                    results.append(
                        {
                            "tool": tool_name,
                            "content": str(result),
                        }
                    )
                    logger.debug(f"Tool {tool_name} returned {len(str(result))} chars")
            except Exception as e:
                logger.error(f"Error invoking tool {tool_name}: {e}")
                # FEATURE:prometheus
                if _METRICS_AVAILABLE:
                    get_metrics_collector().track_tool_invocation(tool_name, 0.0, "error")
                continue

        if not results:
            logger.warning("No results from any tools")
            return ""

        # Combine results into context
        context_parts = []
        for i, result in enumerate(results[:top_k], 1):
            context_parts.append(f"[Tool: {result['tool']}]\n{result['content']}\n")

        return "\n".join(context_parts)

    @otel_trace
    def _format_results(self, results: List[Any]) -> str:
        """
        Format retrieval results into context string.

        Customize this to match your result format.
        """
        if not results:
            return ""

        formatted = []
        for i, result in enumerate(results, 1):
            # Customize this based on your result structure
            content = getattr(result, "content", str(result))
            source = getattr(result, "source", "Unknown")
            formatted.append(f"[{i}] {content}\nSource: {source}\n")

        return "\n".join(formatted)
