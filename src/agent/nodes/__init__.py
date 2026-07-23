"""
Agent workflow nodes.
Each node represents a step in the agent's processing pipeline.

Core pipeline nodes (used by the default executor):
    QueryProcessorNode    — extract search terms from user input
    ContextRetrieverNode  — retrieve relevant context with tools
    ResponseGeneratorNode — generate the final answer

Extended nodes (available for custom graphs):
    AgentNode      — LLM with tool-calling capability
    ToolNode       — execute tools requested by the agent
    should_continue — routing helper for tool-calling loops
"""

from .agent_node import AgentNode, ToolNode, should_continue
from .context_retriever import ContextRetrieverNode
from .query_processor import QueryProcessorNode
from .response_generator import ResponseGeneratorNode

__all__ = [
    "AgentNode",
    "ContextRetrieverNode",
    "QueryProcessorNode",
    "ResponseGeneratorNode",
    "ToolNode",
    "should_continue",
]
