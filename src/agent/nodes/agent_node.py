"""
Agent node with tool-calling capability.
This node allows the LLM to decide which tools to call and when.
"""

from typing import Dict, Any, List

from langchain_core.messages import ToolMessage, SystemMessage
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


class AgentNode:
    """
    Agent node that uses LLM with tool-calling capability.

    The LLM can decide to:
    1. Call one or more tools
    2. Return a final answer
    """

    @otel_trace
    def __init__(
        self, config: Dict[str, Any], tools: List[Any], tenant_id: str = "default"
    ):
        """Initialize agent node with tools."""
        self.global_config = config
        self.tools = tools
        self.tenant_id = tenant_id

        # Get tenant-specific config
        tenant_config = config.get(tenant_id, config.get("default", {}))
        llm_config = tenant_config.get("llm_config", {}).get("agent", tenant_config.get("llm_config", {}).get("response", {}))
        self.model = llm_config.get("model", "gpt-4o")
        self.temperature = llm_config.get("temperature", 0.1)
        self.max_tokens = llm_config.get("max_tokens")
        self.provider = llm_config.get("provider")

        if not self.provider:
            try:
                self.provider = get_provider_from_model(self.model)
            except ValueError:
                logger.warning(f"Could not infer provider for model {self.model}, defaulting to openai")
                self.provider = "openai"

        self.llm = get_llm_model(
            provider=self.provider,
            model_name=self.model,
            temperature=self.temperature,
            streaming=True,
            max_tokens=self.max_tokens,
        )

        # System prompt from config instructions
        self.system_prompt = tenant_config.get("prompts", {}).get("system", "")

        # Bind tools to LLM
        if self.tools:
            self.llm_with_tools = self.llm.bind_tools(self.tools)
            logger.info(f"Agent node initialized with {len(self.tools)} tools")
        else:
            self.llm_with_tools = self.llm
            logger.warning("Agent node initialized with no tools")

    # FEATURE:prometheus
    @track_node_metrics("agent")
    @trace_node(
        node_name="agent",
        capture_input=True,
        capture_output=True,
        estimate_tokens=True,
        calculate_cost=True,
        metadata_fn=lambda state: {
            "message_count": len(state.get("messages", [])),
            "tenant_id": state.get("tenant_id", "default"),
        },
    )
    @otel_trace
    async def execute(self, state: Dict[str, Any]) -> Dict[str, Any]:
        """
        Execute agent with tool-calling capability.

        The LLM will decide whether to:
        - Call tools to gather information
        - Return a final answer
        """
        messages = state.get("messages", [])

        # Prepend system prompt if configured and not already present
        if self.system_prompt and not (messages and isinstance(messages[0], SystemMessage)):
            messages = [SystemMessage(content=self.system_prompt)] + list(messages)

        # Get LiteLLM custom headers if available
        litellm_headers = state.get("litellm_headers")

        # Invoke LLM with tools
        if litellm_headers:
            response = await self.llm_with_tools.ainvoke(
                messages,
                config={"configurable": {"litellm_headers": litellm_headers}}
            )
        else:
            response = await self.llm_with_tools.ainvoke(messages)

        logger.info(f"Agent response generated: has_tool_calls={bool(response.tool_calls) if hasattr(response, 'tool_calls') else False}")

        return {"messages": [response]}


class ToolNode:
    """
    Tool execution node.
    Executes the tools requested by the agent.
    """

    @otel_trace
    def __init__(self, tools: List[Any]):
        """Initialize tool node."""
        self.tools_by_name = {tool.name: tool for tool in tools}
        logger.info(f"Tool node initialized with {len(tools)} tools: {list(self.tools_by_name.keys())}")

    # FEATURE:prometheus
    @track_node_metrics("tools")
    @trace_node(
        node_name="tools",
        capture_input=True,
        capture_output=True,
        estimate_tokens=False,
        calculate_cost=False,
        metadata_fn=lambda state: {
            "tool_calls_count": len(state.get("messages", [])[-1].tool_calls) if state.get("messages") and hasattr(state["messages"][-1], 'tool_calls') else 0,
        },
    )
    @otel_trace
    async def execute(self, state: Dict[str, Any]) -> Dict[str, Any]:
        """
        Execute tools requested by the agent.
        """
        messages = state.get("messages", [])
        last_message = messages[-1] if messages else None

        if not last_message or not hasattr(last_message, 'tool_calls'):
            logger.warning("No tool calls found in last message")
            return {"messages": []}

        tool_calls = last_message.tool_calls
        logger.info(f"Executing {len(tool_calls)} tool calls")

        tool_messages = []

        for tool_call in tool_calls:
            tool_name = tool_call["name"]
            tool_args = tool_call["args"]
            tool_call_id = tool_call["id"]

            logger.debug(f"Calling tool: {tool_name} with args: {tool_args}")

            tool = self.tools_by_name.get(tool_name)
            if not tool:
                error_msg = f"Tool '{tool_name}' not found"
                logger.error(error_msg)
                tool_messages.append(
                    ToolMessage(
                        content=error_msg,
                        tool_call_id=tool_call_id,
                        name=tool_name
                    )
                )
                continue

            try:
                # Execute tool
                import time as _time
                _tool_start = _time.time()
                result = await tool.ainvoke(tool_args)
                _tool_duration = _time.time() - _tool_start

                # Convert result to string
                result_str = str(result)

                # FEATURE:prometheus
                if _METRICS_AVAILABLE:
                    get_metrics_collector().track_tool_invocation(tool_name, _tool_duration, "success", state.get("tenant_id", "default"))

                logger.info(f"Tool {tool_name} executed successfully: result_length={len(result_str)}")

                tool_messages.append(
                    ToolMessage(
                        content=result_str,
                        tool_call_id=tool_call_id,
                        name=tool_name
                    )
                )

            except Exception as e:
                error_msg = f"Error executing tool {tool_name}: {str(e)}"
                logger.error(error_msg)
                # FEATURE:prometheus
                if _METRICS_AVAILABLE:
                    get_metrics_collector().track_tool_invocation(tool_name, 0.0, "error", state.get("tenant_id", "default"))
                tool_messages.append(
                    ToolMessage(
                        content=error_msg,
                        tool_call_id=tool_call_id,
                        name=tool_name
                    )
                )

        return {"messages": tool_messages}


@otel_trace
def should_continue(state: Dict[str, Any]) -> str:
    """
    Determine if we should continue to tools or end.

    Returns:
        "tools" if the last message has tool calls
        "end" if the last message is a final response
    """
    messages = state.get("messages", [])
    last_message = messages[-1] if messages else None

    # If the last message has tool calls, route to tools
    if last_message and hasattr(last_message, 'tool_calls') and last_message.tool_calls:
        return "tools"

    # Otherwise, we're done
    return "end"
