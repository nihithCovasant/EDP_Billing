"""
Tool Registry for centralizing tool management.
"""

import importlib.util
import inspect
import sys
from pathlib import Path
from typing import Any

from cams_otel_lib import Logger as logger
from cams_otel_lib import otel_trace

# Infrastructure modules in src/tools/ — not LangChain tools. Loading them via
# spec_from_file_location (instead of a normal package import) breaks @dataclass
# on Python 3.14+ and spams ERROR logs on every agent startup for no benefit.
_SKIP_TOOL_DISCOVERY = frozenset({"cbos_client", "mcp_loader", "registry", "__init__"})


class ToolRegistry:
    """
    Centralized registry for managing agent tools.

    Features:
    - Register tools with configuration
    - Discover tools from directories
    - Validate tool compatibility
    - Tenant-specific tool management
    """

    def __init__(self):
        """Initialize the tool registry."""
        self._tools: dict[str, type] = {}
        self._configs: dict[str, dict[str, Any]] = {}
        self._instances: dict[str, Any] = {}

    @otel_trace
    def register(
        self,
        tool_class: type,
        name: str | None = None,
        config: dict[str, Any] | None = None,
    ):
        """
        Register a tool class.

        Args:
            tool_class: Tool class to register
            name: Optional custom name (default: class name)
            config: Default configuration for the tool
        """
        tool_name = name or tool_class.__name__
        self._tools[tool_name] = tool_class
        self._configs[tool_name] = config or {}
        logger.debug(f"Registered tool: {tool_name}")

    @otel_trace
    def get_tools(self, tenant_id: str = "default") -> list[Any]:
        """
        Get initialized tool instances for a tenant.

        Args:
            tenant_id: Tenant identifier

        Returns:
            List of tool instances
        """
        tools = []
        for tool_name, tool_obj in self._tools.items():
            try:
                # Check if it's a langchain tool (has 'name' and 'description' attributes)
                if hasattr(tool_obj, "name") and hasattr(tool_obj, "description"):
                    # It's already a tool instance (e.g., langchain StructuredTool)
                    tools.append(tool_obj)
                elif inspect.isclass(tool_obj):
                    # It's a class, instantiate it
                    instance_key = f"{tenant_id}:{tool_name}"
                    if instance_key not in self._instances:
                        config = self._configs.get(tool_name, {})
                        self._instances[instance_key] = tool_obj(**config)
                    tools.append(self._instances[instance_key])
                else:
                    # Fallback: treat as already initialized
                    tools.append(tool_obj)
            except Exception as e:
                logger.warning(f"Failed to initialize tool {tool_name}: {e}")

        return tools

    @otel_trace
    def discover_tools(self, directory: Path):
        """
        Discover and register tools from a directory.

        Automatically finds:
        - Tool classes (classes ending with "Tool" or having __tool__ attribute)
        - @tool decorated functions (langchain StructuredTool instances)

        Args:
            directory: Directory to search for tool files
        """
        if not directory.exists():
            logger.warning(f"Tool directory does not exist: {directory}")
            return

        for file_path in directory.glob("*.py"):
            if file_path.stem.startswith("_") or file_path.stem in _SKIP_TOOL_DISCOVERY:
                continue

            try:
                # Load module — register in sys.modules BEFORE exec_module so
                # @dataclass (Python 3.14+) can resolve cls.__module__ correctly.
                module_name = f"src.tools.{file_path.stem}"
                spec = importlib.util.spec_from_file_location(module_name, file_path)
                if spec and spec.loader:
                    module = importlib.util.module_from_spec(spec)
                    sys.modules[module_name] = module
                    spec.loader.exec_module(module)

                    # Find tool classes
                    for name, obj in inspect.getmembers(module, inspect.isclass):
                        if hasattr(obj, "__tool__") or name.endswith("Tool"):
                            self.register(obj)
                            logger.info(f"Auto-discovered tool class: {name}")

                    # Find @tool decorated functions (langchain StructuredTool)
                    # Use module.__dict__ instead of inspect.getmembers to catch all module-level objects
                    for name, obj in module.__dict__.items():
                        # Skip private members, classes, and modules
                        if name.startswith("_") or inspect.isclass(obj) or inspect.ismodule(obj):
                            continue

                        # Check if it's a langchain StructuredTool (from @tool decorator)
                        # StructuredTool has name, description, and invoke method
                        if hasattr(obj, "name") and hasattr(obj, "description") and hasattr(obj, "invoke"):
                            # Register the tool instance directly
                            tool_name = getattr(obj, "name", name)
                            self._tools[tool_name] = obj
                            logger.info(f"Auto-discovered @tool function: {tool_name}")

            except Exception as e:
                logger.error(f"Could not load tools from {file_path}: {e}", exc_info=True)

    @otel_trace
    def validate_tools(self) -> dict[str, bool]:
        """
        Validate all registered tools.

        Returns:
            Dict mapping tool names to validation status
        """
        results = {}
        for tool_name, tool_class in self._tools.items():
            try:
                # Basic validation - check if tool can be instantiated
                config = self._configs.get(tool_name, {})
                tool_class(**config)
                results[tool_name] = True
            except Exception as e:
                logger.error(f"Tool {tool_name} validation failed: {e}")
                results[tool_name] = False

        return results
