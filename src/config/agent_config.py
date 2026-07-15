"""
Agent configuration loading with CAMS schema support.
"""

import json
from pathlib import Path
from typing import Dict, Any, Optional

from src.config.cams_config_adapter import load_cams_config
from src.config.settings import settings, load_effective_config_dict
from cams_otel_lib import Logger as logger, otel_trace

_MULTI_TENANT = settings.multi_tenant_enabled



def _deep_merge(base: Dict[str, Any], overrides: Dict[str, Any]) -> None:
    """Merge overrides into base in-place. Nested dicts are merged; other types are overwritten."""
    for key, value in overrides.items():
        if key in base and isinstance(base[key], dict) and isinstance(value, dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value


@otel_trace
def load_agent_config(config_path: Optional[Path] = None) -> Dict[str, Any]:
    """
    Load agent configuration (CAMS schema).

    Without an explicit config_path, this uses the same effective-config
    resolution as apply_config_env() (see settings.load_effective_config_dict):
    the CAMS-mounted/APP_CONFIG_PATH agent_config.json if present, else the
    committed one baked into the image, with any blank/missing field filled in
    from a local, gitignored local.agent_config.json when one exists (local
    dev only — never present in a real deployment).

    Args:
        config_path: Explicit file to load instead of the effective config
            resolution above (bypasses the local.agent_config.json fallback).

    Returns:
        Configuration dictionary in internal format
    """
    try:
        if config_path is not None:
            with open(config_path, "r") as f:
                cams_config = json.load(f)
            logger.info(f"✓ Successfully loaded CAMS agent configuration from: {config_path}")
        else:
            cams_config = load_effective_config_dict()
            logger.info("✓ Successfully loaded CAMS agent configuration (effective config)")

        # Convert CAMS schema to internal format
        return load_cams_config(cams_config)
    except Exception as e:
        logger.error(f"Error loading config from {config_path or 'effective config'}: {e}")
        return {"default": {}}


@otel_trace
def get_node_configuration(
    node_name: str, tenant_id: str, config: Dict[str, Any]
) -> Dict[str, Any]:
    """
    Get node-specific configuration for a tenant.

    Args:
        node_name: Name of the node (e.g., "search_query", "response")
        tenant_id: Tenant identifier
        config: Full configuration dictionary

    Returns:
        Node-specific configuration
    """
    effective_tenant = tenant_id if _MULTI_TENANT else "default"
    tenant_config = config.get(effective_tenant, config.get("default", {}))

    # Check for node-specific config
    node_config = tenant_config.get(f"{node_name}_config", {})

    return node_config


@otel_trace
def get_secrets(tenant_id: str, config: Dict[str, Any]) -> Dict[str, Any]:
    """
    Get secrets configuration for a tenant.
    
    Secrets include: Langfuse, Sentry, Database, LiteLLM, GCP, Pinecone, etc.
    API keys (OpenAI, Anthropic, Google) come from environment variables.

    Args:
        tenant_id: Tenant identifier
        config: Full configuration dictionary

    Returns:
        Secrets configuration dictionary
    """
    effective_tenant = tenant_id if _MULTI_TENANT else "default"
    tenant_config = config.get(effective_tenant, config.get("default", {}))
    secrets = tenant_config.get("secrets", {})

    return secrets


# FEATURE:kubernetes_configmap
def _merge_kubernetes_configmap(internal_config: Dict[str, Any]) -> None:
    """
    Merge per-tenant config and secrets from Kubernetes ConfigMap into internal_config.

    ConfigMap overrides take priority over values in agent_config.json so that
    CAMS can inject tenant-specific settings at deploy time without rebuilding
    the Docker image.

    Only runs when ENV_ID, AGENT_ID, and VERSION_ID environment variables are set
    (i.e. inside a real Kubernetes deployment).
    """
    config_loader = get_config_loader()
    secrets_loader = get_secrets_loader()

    if not config_loader.is_k8s_deployment():
        logger.debug("Not a K8s deployment — skipping ConfigMap merge")
        return

    tenant_ids = config_loader.tenant_ids or list(internal_config.keys())
    logger.info(f"Merging Kubernetes ConfigMap overrides for tenants: {tenant_ids}")

    for tenant_id in tenant_ids:
        if tenant_id not in internal_config:
            continue

        tenant_overrides = config_loader.load_tenant_config(tenant_id)
        if tenant_overrides:
            _deep_merge(internal_config[tenant_id], tenant_overrides)
            logger.info(f"Applied ConfigMap config overrides for tenant: {tenant_id}")

        tenant_secrets = secrets_loader.load_tenant_secrets(tenant_id)
        if tenant_secrets:
            existing_secrets = internal_config[tenant_id].get("secrets", {})
            _deep_merge(existing_secrets, tenant_secrets)
            internal_config[tenant_id]["secrets"] = existing_secrets
            logger.info(f"Applied ConfigMap secrets overrides for tenant: {tenant_id}")
