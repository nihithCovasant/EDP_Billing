#!/bin/bash
set -e

echo "==================================="
echo "Agent Docker Container Starting"
echo "==================================="

# Function to inject config from environment or mounted volume
inject_config() {
    echo "Checking for configuration injection..."
    
    # Priority 1: If APP_CONFIG_PATH is set, use external config (for multi-instance support)
    if [ ! -z "$APP_CONFIG_PATH" ]; then
        echo "APP_CONFIG_PATH is set to: $APP_CONFIG_PATH"
        if [ -f "$APP_CONFIG_PATH" ]; then
            echo "✓ External config found at $APP_CONFIG_PATH"
            echo "✓ Agent will load config from external path (multi-instance mode)"
        else
            echo "⚠ APP_CONFIG_PATH set but file not found at $APP_CONFIG_PATH"
            echo "⚠ Will fall back to internal config if available"
        fi
        return
    fi
    
    # Priority 2: If CONFIG_JSON is provided as environment variable, write it to internal path
    if [ ! -z "$CONFIG_JSON" ]; then
        echo "Injecting config from CONFIG_JSON environment variable..."
        echo "$CONFIG_JSON" > /app/src/config/agent_config.json
        echo "✓ Config injected from environment variable to internal path"
        return
    fi
    
    # Priority 3: If config file is mounted at /app/config/agent_config.json, copy it to internal path
    if [ -f "/app/config/agent_config.json" ]; then
        echo "Copying config from mounted volume..."
        cp /app/config/agent_config.json /app/src/config/agent_config.json
        echo "✓ Config copied from mounted volume to internal path"
        return
    fi
    
    # Check if internal config exists
    if [ -f "/app/src/config/agent_config.json" ]; then
        echo "✓ Using internal agent_config.json from image"
    else
        echo "⚠ No agent_config.json found, using defaults"
    fi
}

# Function to display configuration summary
show_config() {
    echo ""
    echo "Configuration Summary:"
    echo "---------------------"
    echo "Agent Name: ${AGENT_NAME:-agent}"
    echo "Host: ${HOST:-0.0.0.0}"
    echo "Port: ${PORT:-9999}"
    echo "Log Level: ${LOG_LEVEL:-INFO}"
    echo "Streaming: ${STREAMING_ENABLED:-true}"
    echo "Multi-Tenant: ${MULTI_TENANT_ENABLED:-true}"
    echo "Langfuse: ${LANGFUSE_ENABLED:-false}"
    echo "LiteLLM Gateway: ${USE_LITELLM_GATEWAY:-false}"
    
    if [ ! -z "$APP_CONFIG_PATH" ]; then
        echo "Config Path: $APP_CONFIG_PATH (external)"
    else
        echo "Config Path: /app/src/config/agent_config.json (internal)"
    fi
    
    if [ ! -z "$ENV_ID" ]; then
        echo "Environment ID: $ENV_ID"
    fi
    
    if [ ! -z "$TENANT_IDS" ]; then
        echo "Tenant IDs: $TENANT_IDS"
    fi
    
    echo "---------------------"
    echo ""
}

# Inject configuration
inject_config

# Show configuration summary
show_config

# Execute the main command
echo "Starting agent..."
echo "==================================="
exec "$@"
