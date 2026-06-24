# Secrets Architecture - Configuration Guide

## 🔐 Overview

This agent uses a **two-tier secrets architecture**:

1. **LLM API Keys** → Environment variables (from secret manager)
2. **All Other Secrets** → `agent_config.json` (mounted as ConfigMap)

---

## 📋 Configuration Structure

### Tier 1: API Keys (Environment Variables)

**Location:** `.env` file (local) or Secret Manager (production)

**What goes here:**
- `OPENAI_API_KEY`
- `ANTHROPIC_API_KEY`
- `GOOGLE_API_KEY`
- `TAVILY_API_KEY`
- `SERP_API_KEY`
- `PINECONE_API_KEY`
- `GCP_SERVICE_ACCOUNT_JSON` (path to file)

**Why environment variables?**
- Managed by deployment platform's secret manager
- Easy rotation without config changes
- Never committed to version control
- Platform-agnostic (works everywhere)

---

### Tier 2: Application Secrets (Config File)

**Location:** `src/config/agent_config.json` → `secrets` section

**What goes here:**
```json
{
  "agent_config": {
    "secrets": {
      "langfuse": {
        "enabled": true,
        "public_key": "pk-lf-your-key",
        "secret_key": "sk-lf-your-key",
        "host": "https://cloud.langfuse.com"
      },
      "sentry": {
        "enabled": true,
        "dsn": "https://your-sentry-dsn",
        "environment": "production",
        "traces_sample_rate": 0.1
      },
      "database": {
        "postgres": {
          "connection_string": "postgresql://user:pass@host:5432/db"
        }
      },
      "litellm": {
        "enabled": true,
        "base_url": "https://your-litellm-gateway/v1"
      },
      "gcp": {
        "project_id": "your-gcp-project",
        "location": "us-central1",
        "use_workload_identity": false
      },
      "pinecone": {
        "environment": "us-east-1-aws"
      },
      "remote_config": {
        "enabled": false,
        "registry_base_url": "",
        "registry_timeout_seconds": 5,
        "deployment_plan_id": "",
        "env_id": "",
        "agent_definition_id": "",
        "tenants": ""
      }
    }
  }
}
```

**Why in config file?**
- User controls all credentials (you never see them)
- Easy to update without rebuilding Docker image
- Supports multi-tenant deployments
- Mounted as ConfigMap in Kubernetes

---

## 🚀 Deployment Flow

### Local Development

```bash
# 1. Set API keys in .env
echo "OPENAI_API_KEY=sk-your-key" > .env

# 2. Configure secrets in agent_config.json
nano src/config/agent_config.json

# 3. Run agent
python -m src.agent
```

---

### Docker Deployment

```bash
# Build image (no secrets baked in!)
docker build -f docker/Dockerfile -t my-agent:v1.0 .

# Run with config mount + API keys from secret manager
docker run -d \
  -v /path/to/agent_config.json:/app/config/agent_config.json \
  -e OPENAI_API_KEY="${SECRET_MANAGER_OPENAI_KEY}" \
  -e ANTHROPIC_API_KEY="${SECRET_MANAGER_ANTHROPIC_KEY}" \
  my-agent:v1.0
```

---

### Kubernetes Deployment

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: my-agent-config
data:
  agent_config.json: |
    {
      "agent_config": {
        "secrets": {
          "langfuse": {
            "enabled": true,
            "public_key": "pk-lf-...",
            "secret_key": "sk-lf-..."
          }
        }
      }
    }
---
apiVersion: v1
kind: Secret
metadata:
  name: llm-api-keys
type: Opaque
stringData:
  openai-api-key: sk-proj-...
  anthropic-api-key: sk-ant-...
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: my-agent
spec:
  template:
    spec:
      containers:
      - name: agent
        image: registry.io/my-agent:v1.0
        volumeMounts:
        - name: config
          mountPath: /app/config/agent_config.json
          subPath: agent_config.json
        env:
        - name: OPENAI_API_KEY
          valueFrom:
            secretKeyRef:
              name: llm-api-keys
              key: openai-api-key
        - name: ANTHROPIC_API_KEY
          valueFrom:
            secretKeyRef:
              name: llm-api-keys
              key: anthropic-api-key
      volumes:
      - name: config
        configMap:
          name: my-agent-config
```

---

## 🔄 How Code Reads Secrets

### API Keys (from environment)

```python
from src.config.settings import settings

# API keys come from environment variables
openai_key = settings.openai_api_key
anthropic_key = settings.anthropic_api_key
```

### Application Secrets (from config)

```python
from src.config.agent_config import get_secrets

# Get secrets for a tenant
secrets = get_secrets("default", config)

# Access specific secrets
langfuse_config = secrets.get("langfuse", {})
sentry_config = secrets.get("sentry", {})
litellm_config = secrets.get("litellm", {})
```

### Example: Observability Module

```python
# src/utils/observability.py
from src.config.agent_config import get_secrets

# In executor.py
secrets = get_secrets("default", self.config)
self.observability = ObservabilityManager(
    langfuse_config=secrets.get("langfuse", {})
)
```

---

## ✅ Benefits

### For Users (Deploying the Agent)

✅ **Full control** - You manage all credentials  
✅ **No secrets in Docker image** - Image is safe to share  
✅ **Easy updates** - Change config without rebuilding  
✅ **Multi-tenant** - Different secrets per deployment  
✅ **Platform-agnostic** - Works on any deployment platform  

### For Developers (Building the Agent)

✅ **Never see user credentials** - Security by design  
✅ **Simple deployment** - Just mount config file  
✅ **Consistent pattern** - All secrets in one place  
✅ **Easy testing** - Mock secrets in config  

---

## 🛡️ Security Best Practices

### ✅ DO:
- Store `agent_config.json` in secure location
- Use ConfigMaps/Secrets in Kubernetes
- Rotate API keys regularly via secret manager
- Use different configs for dev/staging/prod
- Enable encryption at rest for ConfigMaps

### ❌ DON'T:
- Commit `agent_config.json` with real secrets to git
- Hardcode secrets in code
- Share config files via email/Slack
- Use same secrets across environments
- Bake secrets into Docker images

---

## 📝 Migration from Old Approach

### Old Way (Everything in .env)
```bash
# .env file
OPENAI_API_KEY=sk-...
LANGFUSE_PUBLIC_KEY=pk-lf-...
LANGFUSE_SECRET_KEY=sk-lf-...
SENTRY_DSN=https://...
POSTGRES_CONNECTION_STRING=postgresql://...
LITELLM_BASE_URL=https://...
```

### New Way (Split Architecture)
```bash
# .env file (API keys only)
OPENAI_API_KEY=sk-...
ANTHROPIC_API_KEY=sk-ant-...
```

```json
// agent_config.json (all other secrets)
{
  "agent_config": {
    "secrets": {
      "langfuse": { "public_key": "pk-lf-...", "secret_key": "sk-lf-..." },
      "sentry": { "dsn": "https://..." },
      "database": { "postgres": { "connection_string": "postgresql://..." } },
      "litellm": { "base_url": "https://..." }
    }
  }
}
```

---

## 🎯 Summary

| Secret Type | Location | Managed By | Why? |
|-------------|----------|------------|------|
| **LLM API Keys** | Environment variables | Secret Manager | Easy rotation, platform-managed |
| **Langfuse, Sentry, DB, LiteLLM** | `agent_config.json` | User (ConfigMap) | User controls, no rebuild needed |
| **Tool API Keys** | Environment variables | Secret Manager | Same as LLM keys |

**Result:** Clean separation, user privacy, easy deployment! 🚀
