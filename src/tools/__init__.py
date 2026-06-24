"""
Tool registry for the agent.
Manages available tools and their initialization.
"""

import json
import os
from typing import List, Dict, Any, Optional
from pathlib import Path

# Load .env before anything else so OTEL_ENABLED, OTEL_CONFIG_UUID, etc. are
# visible to os.getenv() when we call initialize_otel_client() below.
from dotenv import load_dotenv
load_dotenv()

from .registry import ToolRegistry
from cams_otel_lib import Logger as logger, otel_trace, Otel_Client


def _read_agent_instance_id() -> str:
    """Read instance_id from runtime_context in config. APP_CONFIG_PATH takes priority."""
    try:
        ext = os.getenv("APP_CONFIG_PATH")
        cfg_path = (
            Path(ext)
            if ext and Path(ext).exists()
            else Path(__file__).parent.parent / "config" / "agent_config.json"
        )
        with open(cfg_path) as f:
            data = json.load(f)
        return (
            data.get("runtime_context", {}).get("instance_id")
            or data.get("instance_id", "N/A")
        )
    except Exception:
        return "N/A"


# Initialize OTEL before any @otel_trace calls happen at module import time.
# main() will call initialize_otel_client() again — the SDK skips re-init when
# OTEL_CONFIG_UUID hasn't changed (fixed value in .env), so this is safe.
Otel_Client.initialize_otel_client(
    service_name=os.getenv("AGENT_NAME", "agent"),
    environment=os.getenv("ENVIRONMENT", os.getenv("ENV", "dev")),
    agent_id=_read_agent_instance_id(),
)

# Global tool registry instance
_registry = ToolRegistry()


@otel_trace
def get_available_tools(tenant_id: str = "default") -> List[Any]:
    """
    Get available tools for a tenant.

    Args:
        tenant_id: Tenant identifier for tenant-specific tool configuration

    Returns:
        List of initialized tool instances
    """
    return _registry.get_tools(tenant_id)


@otel_trace
def register_tool(tool_class, name: Optional[str] = None, **config):
    """
    Register a tool with the registry.

    Args:
        tool_class: Tool class to register
        name: Optional custom name for the tool
        **config: Configuration parameters for the tool
    """
    _registry.register(tool_class, name=name, config=config)


@otel_trace
def discover_tools(directory: Optional[Path] = None):
    """
    Discover and register tools from a directory.

    Args:
        directory: Directory to search for tools (default: current tools directory)
    """
    if directory is None:
        directory = Path(__file__).parent

    _registry.discover_tools(directory)


@otel_trace
def validate_tools() -> Dict[str, bool]:
    """
    Validate all registered tools.

    Returns:
        Dict mapping tool names to validation status
    """
    return _registry.validate_tools()


# Auto-discover tools on import
# This finds both:
# - Tool classes (ending with "Tool" or having __tool__ attribute)
# - @tool decorated functions (langchain StructuredTool)
discover_tools()

__all__ = [
    "get_available_tools",
    "register_tool",
    "discover_tools",
    "validate_tools",
    "ToolRegistry",
]
