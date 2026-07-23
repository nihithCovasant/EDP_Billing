"""
CAMS Configuration Adapter
Converts CAMS agent config schema to internal format for backward compatibility.
"""

from typing import Any

from cams_otel_lib import Logger as logger
from cams_otel_lib import otel_trace


class CAMSConfigAdapter:
    """
    Adapter to convert CAMS agent configuration schema to internal format.

    This allows the agent code to work with the CAMS schema while maintaining
    backward compatibility with existing node implementations.
    """

    def __init__(self, cams_config: dict[str, Any]):
        """Initialize with CAMS configuration."""
        self.cams_config = cams_config

    @otel_trace
    def get_tenant_config(self, tenant_id: str = "default") -> dict[str, Any]:
        """
        Get tenant-specific configuration in internal format.

        Args:
            tenant_id: Tenant identifier

        Returns:
            Configuration dictionary compatible with existing node code
        """
        # Find tenant config
        tenant_configs = self.cams_config.get("tenant_config", [])
        tenant_config = None

        for tc in tenant_configs:
            if tc.get("tenant_name") == tenant_id:
                tenant_config = tc
                break

        # Fallback to default tenant
        if not tenant_config:
            for tc in tenant_configs:
                if tc.get("tenant_name") == "default":
                    tenant_config = tc
                    break

        # Fallback to root agent_config
        if not tenant_config:
            # Detect format: A2A card (extensions.cams_v2) vs scaffold format (agent_config)
            cams_v2 = self.cams_config.get("extensions", {}).get("cams_v2", {})
            if cams_v2:
                agent_config = self._extract_from_cams_v2(cams_v2)
            else:
                agent_config = self.cams_config.get("agent_config", {})
        else:
            agent_config = tenant_config.get("agent_config", {})

        # Convert to internal format
        return self._convert_to_internal_format(agent_config)

    @otel_trace
    def _extract_from_cams_v2(self, cams_v2: dict[str, Any]) -> dict[str, Any]:
        """
        Map A2A card extensions.cams_v2 → scaffold agent_config shape
        so _convert_to_internal_format can process either format.
        """
        llm_cfg = cams_v2.get("llm_config", {})
        return {
            "agent_id": cams_v2.get("app_id", ""),
            "agent_name": self.cams_config.get("name", ""),
            "app_name_adk": cams_v2.get("app_name_adk", ""),
            "instructions": cams_v2.get("instructions", ""),
            "llm_config": {
                "model": llm_cfg.get("model", "gpt-4o"),
                "temperature": llm_cfg.get("temperature", 0.3),
                "max_tokens": llm_cfg.get("max_tokens", "4096"),
            },
            # provider may be stored as llm_config.provider or top-level
            "llm_provider": llm_cfg.get("provider", cams_v2.get("llm_provider", "openai")),
            "max_steps": cams_v2.get("max_steps", 10),
            "max_iterations": cams_v2.get("max_iterations", 5),
            "tools": cams_v2.get("tools", []),
            # knowledge_bases is the A2A card name for datastores
            "datastores": cams_v2.get("knowledge_bases", cams_v2.get("datastores", [])),
            "output_key": cams_v2.get("output_key", "final_response"),
            "subagent_configs": cams_v2.get("subagent_configs", []),
            "domain_knowledge": cams_v2.get("domain_knowledge", {}),
            "prompts": cams_v2.get("prompts", {}),
            "secrets": cams_v2.get("secrets", {}),
        }

    @otel_trace
    def _convert_to_internal_format(self, agent_config: dict[str, Any]) -> dict[str, Any]:
        """
        Convert CAMS agent_config to internal format.

        CAMS Schema:
        {
          "agent_name": "...",
          "instructions": "...",
          "llm_config": {"model": "...", "temperature": 0.3},
          "llm_provider": "openai",
          "tools": [...],
          "datastores": [...]
        }

        Internal Format:
        {
          "agent_name": "...",
          "prompts": {
            "system": "...",
            "retrieval": "...",
            "response": "..."
          },
          "llm_config": {
            "retrieval": {"model": "...", "temperature": 0.1, "provider": "..."},
            "response": {"model": "...", "temperature": 0.3, "provider": "..."}
          }
        }
        """
        # Extract base values
        agent_name = agent_config.get("agent_name", "CAMS Agent")
        instructions = agent_config.get("instructions", "You are a helpful AI assistant.")
        llm_config = agent_config.get("llm_config", {})
        llm_provider = agent_config.get("llm_provider", "openai")

        # Build internal format
        internal_config = {
            "agent_name": agent_name,
            "description": agent_config.get("agent_id", ""),
            # Convert instructions to prompts
            "prompts": {
                "system": instructions,
                "retrieval": f"{instructions}\n\nAnalyze this query to extract the most relevant "
                "search terms.\n\nQuery: {{question}}\nContext: {{conversation}}\n\n"
                "Extract 2-5 specific, focused search terms:",
                "response": f"{instructions}\n\nRETRIEVED INFORMATION:\n{{context}}\n\n"
                "CONVERSATION HISTORY:\n{{conversation}}\n\nUSER QUESTION: {{question}}\n\n"
                "Provide a helpful response based on the information above.",
            },
            # Convert LLM config to node-specific configs
            # max_tokens is coerced to int — agent_config.json stores it as string "4096"
            "llm_config": {
                "retrieval": {
                    "provider": llm_provider,
                    "model": llm_config.get("model", "gpt-4o"),
                    "temperature": 0.1,
                    "max_tokens": int(llm_config.get("max_tokens", 4096)),
                },
                "response": {
                    "provider": llm_provider,
                    "model": llm_config.get("model", "gpt-4o"),
                    "temperature": llm_config.get("temperature", 0.3),
                    "max_tokens": int(llm_config.get("max_tokens", 4096)),
                },
                # Used by AgentNode (ReAct graph) — same model as response by default
                "agent": {
                    "provider": llm_provider,
                    "model": llm_config.get("model", "gpt-4o"),
                    "temperature": llm_config.get("temperature", 0.3),
                    "max_tokens": int(llm_config.get("max_tokens", 4096)),
                },
            },
            # Pass through additional config
            "max_steps": int(agent_config.get("max_steps", 10)),
            "max_iterations": int(agent_config.get("max_iterations", 5)),
            "tools": agent_config.get("tools", []),
            "datastores": agent_config.get("datastores", []),
            "output_key": agent_config.get("output_key", "final_response"),
            # Pass through secrets (Langfuse, LiteLLM, etc.)
            "secrets": agent_config.get("secrets", {}),
            # Pass through domain knowledge — all sub-fields used by response_generator
            "domain_knowledge": agent_config.get("domain_knowledge", {}),
            # EDP Billing orchestration config
            "edp": agent_config.get("edp", {}),
        }

        # Override prompts if they exist in agent_config
        if "prompts" in agent_config:
            internal_config["prompts"].update(agent_config["prompts"])

        return internal_config

    @otel_trace
    def get_all_tenant_configs(self) -> dict[str, dict[str, Any]]:
        """
        Get all tenant configurations.

        Returns:
            Dictionary mapping tenant_id to configuration
        """
        configs = {}

        for tenant_config in self.cams_config.get("tenant_config", []):
            tenant_name = tenant_config.get("tenant_name", "default")
            agent_config = tenant_config.get("agent_config", {})
            configs[tenant_name] = self._convert_to_internal_format(agent_config)

        return configs

    @otel_trace
    def get_agent_definition(self) -> dict[str, Any]:
        """Get agent definition from CAMS config."""
        return self.cams_config.get("agent_definition", {})

    @otel_trace
    def get_dependant_agents(self) -> list:
        """Get list of dependant agents."""
        return self.cams_config.get("dependantAgents", [])


@otel_trace
def load_cams_config(config: dict[str, Any]) -> dict[str, Any]:
    """
    Load CAMS configuration and convert to internal format.

    Args:
        config: CAMS configuration dictionary

    Returns:
        Dictionary with tenant configurations in internal format
    """
    adapter = CAMSConfigAdapter(config)

    # Get all tenant configs
    tenant_configs = adapter.get_all_tenant_configs()

    # Add default config if not present
    if "default" not in tenant_configs:
        tenant_configs["default"] = adapter.get_tenant_config("default")

    logger.info(f"Loaded CAMS config with {len(tenant_configs)} tenant(s)")

    return tenant_configs
