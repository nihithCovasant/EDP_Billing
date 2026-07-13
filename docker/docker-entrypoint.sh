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
    # Runtime settings (agent name, host, port, log level, OTEL, EDP, email,
    # ...) are loaded inside Python from agent_config.json's `env` block by
    # apply_config_env() — they are NOT shell env vars here, so we don't echo
    # misleading defaults. agent_config.json is the single source of truth.
    if [ ! -z "$APP_CONFIG_PATH" ]; then
        echo "Config Path: $APP_CONFIG_PATH (external)"
    else
        echo "Config Path: /app/src/config/agent_config.json (internal)"
    fi
    echo "Runtime settings: loaded from agent_config.json -> agent_config.env"

    # Show any explicit env-var overrides that are actually set (these win over
    # the config file via os.environ.setdefault()).
    for v in AGENT_NAME HOST PORT LOG_LEVEL EMAIL_DRY_RUN; do
        eval "val=\${$v:-}"
        if [ ! -z "$val" ]; then
            echo "Override: $v=$val"
        fi
    done

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
