"""
MCP Tool Loader — loads platform tools configured in agent_config.tools at runtime.

Tools in agent_config.json follow the format:
  { "name": "tool_name", "mcp_url": "http://mcp-server:5000/sse", "description": "..." }

They connect to MCP servers via SSE and are loaded once at agent startup.
The MCP client is held alive for the process lifetime so tool calls remain available.

--- How to add your OWN tools in code (hybrid approach) ---

Option A: @tool decorator (auto-discovered, simplest)
    Create a .py file in this directory (src/tools/) with functions decorated with @tool.
    The registry auto-discovers them on import.

    Example — create src/tools/my_tools.py:

        from langchain_core.tools import tool

        @tool
        def get_weather(city: str) -> str:
            \"\"\"Get current weather for a city.\"\"\"
            # your implementation here
            return f"Weather in {city}: sunny, 25°C"

        @tool
        def search_database(query: str) -> str:
            \"\"\"Search the internal database for information.\"\"\"
            # your implementation here
            return "Results: ..."

Option B: StructuredTool with input schema (for complex typed inputs)
    from langchain_core.tools import StructuredTool
    from pydantic import BaseModel

    class SearchInput(BaseModel):
        query: str
        top_k: int = 5

    def search_fn(query: str, top_k: int = 5) -> str:
        \"\"\"Search knowledge base.\"\"\"
        ...

    search_tool = StructuredTool.from_function(
        func=search_fn,
        name="search_knowledge_base",
        description="Search the internal knowledge base",
        args_schema=SearchInput,
    )

    Then register it in __init__.py or any auto-discovered file.

Config-based MCP tools (this file) are merged with code tools automatically.
"""

import json
import os
from pathlib import Path
from typing import Any, Dict, List

from cams_otel_lib import Logger as logger


def load_tool_configs() -> List[Dict[str, Any]]:
    """Read tools array from agent_config.json (checks agent_config section then tenant default)."""
    try:
        ext = os.getenv("APP_CONFIG_PATH")
        cfg_path = (
            Path(ext)
            if ext
            else Path(__file__).parent.parent / "config" / "agent_config.json"
        )
        logger.info(f"MCP loader: reading config from {cfg_path} (exists={cfg_path.exists()})")
        if not cfg_path.exists():
            logger.warning(f"MCP loader: config file not found at {cfg_path}")
            return []
        with open(cfg_path) as f:
            data = json.load(f)

        tools = data.get("agent_config", {}).get("tools", [])
        if not tools:
            for tenant in data.get("tenant_config", []):
                if tenant.get("tenant_name") == "default":
                    tools = tenant.get("agent_config", {}).get("tools", [])
                    break

        mcp_tools = [t for t in tools if isinstance(t, dict) and t.get("mcp_url")]
        logger.info(f"MCP loader: found {len(tools)} tools in config, {len(mcp_tools)} have mcp_url")
        return mcp_tools
    except Exception as e:
        logger.warning(f"Could not read tool configs from agent_config.json: {e}")
    return []


async def load_mcp_tools() -> List[Any]:
    """
    Load MCP tools from agent_config.tools as LangChain-compatible tool objects.

    Connects to each configured MCP server via SSE and fetches available tools.
    Uses langchain-mcp-adapters >= 0.1.0 API: await client.get_tools() (not context manager).
    Returns an empty list when no tools are configured or the package is unavailable.
    """
    tool_configs = load_tool_configs()
    if not tool_configs:
        logger.info("MCP loader: no tools with mcp_url found in config — skipping MCP init")
        return []

    try:
        from langchain_mcp_adapters.client import MultiServerMCPClient
    except ImportError:
        logger.warning(
            "langchain-mcp-adapters not installed — MCP tools from config will not load. "
            "Add 'langchain-mcp-adapters' to requirements.txt to enable."
        )
        return []

    # Group by unique mcp_url — one SSE connection per server, not per tool.
    # Multiple tools can share the same server; connecting twice duplicates all tools.
    from urllib.parse import urlparse

    seen_urls: Dict[str, str] = {}  # mcp_url → unique server alias
    alias_counts: Dict[str, int] = {}  # base alias → count (handles same-host different-path)

    for tool in tool_configs:
        mcp_url = (tool.get("mcp_url") or "").strip()
        if not mcp_url or mcp_url in seen_urls:
            continue
        try:
            hostname = urlparse(mcp_url).hostname or mcp_url[:30]
            base_alias = hostname.replace(".", "_").replace("-", "_")
        except Exception:
            base_alias = "server"

        count = alias_counts.get(base_alias, 0)
        alias = base_alias if count == 0 else f"{base_alias}_{count}"
        alias_counts[base_alias] = count + 1
        seen_urls[mcp_url] = alias

    servers: Dict[str, Any] = {
        alias: {"url": url, "transport": "sse"}
        for url, alias in seen_urls.items()
    }

    if not servers:
        return []

    try:
        logger.info(f"MCP loader: connecting to {len(servers)} server(s): {list(servers.keys())}")
        client = MultiServerMCPClient(servers)
        tools = await client.get_tools()
        logger.info(
            f"Loaded {len(tools)} MCP tools from config: {[t.name for t in tools]}"
        )
        return tools
    except Exception as e:
        logger.error(f"Failed to load MCP tools from config: {e}")
        return []
