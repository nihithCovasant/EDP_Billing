# ProCode Agent - Complete Developer Guide

**Your scaffolded agent is ready!** This guide walks you through the complete journey from code to deployment.

---

## 🎯 The Complete Workflow

```
1. Scaffold Generated ✅ (You are here!)
   ↓
2. Write Your Agent Logic (30 mins - 2 hours)
   ↓
3. Test Locally with Docker Compose (5 mins)
   ↓
4. Build Docker Image (2 mins)
   ↓
5. Push to Registry (2 mins)
   ↓
6. Submit to Deployment Team
   ↓
7. Get Your Agent URL! 🚀
```

---

## 🚀 Quick Start (5 Minutes)

### Step 1: Setup Configuration

**Two types of configuration:**

1. **API Keys** (in `.env` file) - From your secret manager
2. **All Other Secrets** (in `agent_config.json`) - Langfuse, Sentry, Database, LiteLLM, etc.

```bash
# 1. Copy environment template for API keys
cp .env.example .env

# 2. Add your LLM API keys to .env
nano .env
```

**Minimum required in `.env`:**
```bash
# LLM API Keys (from secret manager)
OPENAI_API_KEY=sk-your-key-here
# OR
ANTHROPIC_API_KEY=sk-ant-your-key-here
# OR
GOOGLE_API_KEY=your-google-key-here
```

**Configure other secrets in `src/config/agent_config.json`:**
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
        "environment": "production"
      },
      "litellm": {
        "enabled": true,
        "base_url": "https://your-litellm-gateway/v1"
      }
    }
  }
}
```

### Step 2: Install Dependencies

```bash
pip install -r requirements.txt
```

### Step 3: Run Locally

```bash
# Start the agent
python -m src.agent

# Expected output:
# ✓ Agent server running on http://0.0.0.0:9999
```

### Step 4: Test It Works

```bash
# In another terminal
curl http://localhost:9999/health

# Should return: {"status": "healthy"}
```

**✅ Your agent is running!** Now let's customize it.

---

## ✏️ Where to Write Your Code

### 📁 Project Structure

```
my-agent/
├── src/
│   ├── agent/
│   │   ├── executor.py        # ⭐ EDIT: Define workflow (A→B→C)
│   │   └── nodes/             # ⭐ EDIT: Implement logic for each step
│   │       ├── query_processor.py
│   │       ├── context_retriever.py
│   │       └── response_generator.py
│   ├── tools/                 # ⭐ ADD: Your custom tools here
│   │   └── example_tool.py
│   └── config/
│       ├── agent_config.json  # ⭐ EDIT: Agent personality & settings
│       └── settings.py        # Don't modify (env loading)
├── docker-compose.yml         # Local testing with PostgreSQL
├── requirements.txt           # Dependencies (auto-generated)
└── .env                       # Your API keys & config
```

### 🎯 Main Files to Edit

| File | Purpose | When to Edit |
|------|---------|--------------|
| `src/agent/executor.py` | Define workflow (A→B→C) | Change agent flow |
| `src/agent/nodes/*.py` | Implement each step's logic | Add business logic |
| `src/tools/*.py` | Add custom tools | Need new capabilities |
| `src/config/agent_config.json` | Agent personality & LLM settings | Change behavior/model |

---

## 🔧 Customize Your Agent

### 1. Define Your Workflow

**Edit:** `src/agent/executor.py`

```python
def build_graph(self, tenant_id: str = "default") -> StateGraph:
    """Define your agent's workflow."""
    workflow = StateGraph(AgentState)
    
    # Add your steps (nodes)
    workflow.add_node("query_processor", query_processor_node.execute)
    workflow.add_node("context_retriever", context_retriever_node.execute)
    workflow.add_node("response_generator", response_generator_node.execute)
    
    # Define the flow (A → B → C)
    workflow.set_entry_point("query_processor")
    workflow.add_edge("query_processor", "context_retriever")
    workflow.add_edge("context_retriever", "response_generator")
    workflow.add_edge("response_generator", END)
    
    return workflow
```

### 2. Implement Node Logic

**Edit:** `src/agent/nodes/query_processor.py`, `context_retriever.py`, `response_generator.py`

```python
async def execute(self, state: AgentState) -> AgentState:
    """Your business logic here."""
    user_query = state["messages"][-1].content
    
    # Your custom logic
    result = await self.do_something(user_query)
    
    return {**state, "result": result}
```

### 3. Add Custom Tools

**Create:** `src/tools/my_tool.py`

```python
from langchain_core.tools import tool

@tool
async def search_products(query: str) -> dict:
    """Search product catalog."""
    # Your implementation
    return {"products": [...]}
```

**Tools are auto-discovered!** Just add the file and it's available to your agent.

### 4. Configure Agent Personality

**Edit:** `src/config/agent_config.json`

```json
{
  "default": {
    "agent_name": "Product Expert",
    "prompts": {
      "system": "You are a helpful product recommendation expert..."
    },
    "llm_config": {
      "query_processor": {
        "model": "gpt-4o",
        "temperature": 0.7
      },
      "response_generator": {
        "model": "gpt-4o",
        "temperature": 0.3
      }
    }
  }
}
```

**Supported Models:**
- OpenAI: `gpt-4o`, `gpt-4-turbo`, `gpt-3.5-turbo`
- Anthropic: `claude-3-5-sonnet-20241022`, `claude-3-opus-20240229`
- Google: `gemini-2.0-flash-exp`, `gemini-1.5-pro`

---

## 🧪 Test Locally with Docker Compose

### Start PostgreSQL + Agent

```bash
# Start everything (PostgreSQL + Agent)
docker-compose up -d

# View logs
docker-compose logs -f agent

# Test health
curl http://localhost:9999/health
```

### Stop Services

```bash
# Stop everything
docker-compose down

# Stop and delete data
docker-compose down -v
```

---

## 🐳 Build Docker Image

### Step 1: Build the Image

```bash
# Build with version tag
docker build -t my-agent:v1.0 -f docker/Dockerfile .

# Or with registry URL
docker build -t registry.example.com/my-company/my-agent:v1.0 -f docker/Dockerfile .
```

**Build context:** Run from project root (where `docker-compose.yml` is)

### Step 2: Test the Image Locally

```bash
# Run your image (LLM API key required; all other secrets come from agent_config.json)
docker run -p 9999:9999 \
  -e OPENAI_API_KEY=sk-xxx \
  my-agent:v1.0

# Test it
curl http://localhost:9999/health
```

### Step 3: Push to Registry

```bash
# Login to your registry
docker login registry.example.com

# Push the image
docker push registry.example.com/my-company/my-agent:v1.0
```

**Common Registries:**
- Docker Hub: `docker.io/username/my-agent:v1.0`
- Google GCR: `gcr.io/project-id/my-agent:v1.0`
- AWS ECR: `123456789.dkr.ecr.us-east-1.amazonaws.com/my-agent:v1.0`
- Azure ACR: `myregistry.azurecr.io/my-agent:v1.0`

---

## 📦 What to Submit to Deployment Team

### Required Items:

#### 1. Docker Image URL
```
registry.example.com/my-company/my-agent:v1.0
```

#### 2. Config File with Secrets
**File:** `src/config/agent_config.json`

This contains **EVERYTHING** except LLM API keys:
- Agent personality/instructions
- LLM model settings
- Tool configurations
- **Langfuse credentials** (public_key, secret_key, host)
- **Sentry configuration** (dsn, environment)
- **Database connection** (postgres connection_string)
- **LiteLLM gateway** (base_url, enabled)
- **GCP/Pinecone settings** (project_id, environment)
- Tenant-specific configs (if multi-tenant)

**Example `agent_config.json` with secrets:**
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
        "environment": "production"
      },
      "database": {
        "postgres": {
          "connection_string": "postgresql://user:pass@host:5432/db"
        }
      },
      "litellm": {
        "enabled": true,
        "base_url": "https://your-litellm-gateway/v1"
      }
    }
  }
}
```

#### 3. LLM API Keys (ONLY)

**These come from deployment platform's secret manager:**

```bash
# LLM API Keys (Required - from secret manager)
OPENAI_API_KEY=sk-xxx
ANTHROPIC_API_KEY=sk-ant-xxx
GOOGLE_API_KEY=xxx

# Optional Tool API Keys
TAVILY_API_KEY=xxx
SERP_API_KEY=xxx
PINECONE_API_KEY=xxx
```

### What Deployment Team Does:

1. ✅ Pulls your Docker image
2. ✅ Mounts your `agent_config.json` as ConfigMap (contains all secrets except API keys)
3. ✅ Injects LLM API keys from secret manager as environment variables
4. ✅ Deploys to Kubernetes
5. ✅ Returns your agent URL

### Deployment Command Example:

```bash
docker run -d \
  -v /path/to/agent_config.json:/app/config/agent_config.json \
  -e OPENAI_API_KEY="${SECRET_MANAGER_OPENAI_KEY}" \
  -e ANTHROPIC_API_KEY="${SECRET_MANAGER_ANTHROPIC_KEY}" \
  registry.example.com/my-company/my-agent:v1.0
```

**OR in Kubernetes:**
```yaml
apiVersion: v1
kind: Pod
spec:
  containers:
  - name: agent
    image: registry.example.com/my-company/my-agent:v1.0
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
  volumes:
  - name: config
    configMap:
      name: my-agent-config
```

### What You Get Back:

```
Agent URL: https://my-agent.example.com
Health Check: https://my-agent.example.com/health
Agent Card: https://my-agent.example.com/.well-known/agent-card.json
```

---

## 🔄 Updating Your Agent

### Config-Only Changes (No Code)

**Example:** Change agent personality, LLM model, temperature

```bash
# 1. Edit agent_config.json
nano src/config/agent_config.json

# 2. Submit updated config to deployment team
# No need to rebuild Docker image!
```

Deployment team will redeploy with same image + new config.

### Code Changes (New Features)

**Example:** Add new tools, change workflow

```bash
# 1. Edit your code
nano src/agent/nodes/my_node.py

# 2. Build new image
docker build -t my-agent:v1.1 -f docker/Dockerfile .

# 3. Push new image
docker push registry.example.com/my-agent:v1.1

# 4. Submit new image URL + config to deployment team
```

---

## 🏢 Multi-Tenant Deployment

**One Docker image → Multiple deployments with different configs**

### Example: Same Agent, Different Tenants

**Tenant A (Premium):**
```json
{
  "tenant_a": {
    "agent_name": "Premium Assistant",
    "prompts": {
      "system": "You are a premium luxury product expert..."
    },
    "llm_config": {
      "response_generator": {"model": "gpt-4o"}
    }
  }
}
```
→ Deployed at: `https://tenant-a-agent.example.com`

**Tenant B (Budget):**
```json
{
  "tenant_b": {
    "agent_name": "Budget Assistant",
    "prompts": {
      "system": "You are a budget-conscious shopping assistant..."
    },
    "llm_config": {
      "response_generator": {"model": "claude-3-5-sonnet-20241022"}
    }
  }
}
```
→ Deployed at: `https://tenant-b-agent.example.com`

**Same code, different behavior!** 🎯

---

## 📊 Monitoring & Observability

### Health Checks

```bash
# Overall health
curl https://my-agent.example.com/health

# Kubernetes readiness probe
curl https://my-agent.example.com/health/ready

# Kubernetes liveness probe
curl https://my-agent.example.com/health/live
```

### Langfuse Tracing (If Enabled)

View traces at: https://cloud.langfuse.com

All agent executions are automatically traced with:
- Node execution times
- LLM calls and token usage
- Tool invocations
- Error tracking

### Sentry Error Tracking (If Enabled)

View errors at: https://sentry.io

Automatic exception capture with:
- Full stack traces
- Request context (tenant_id, thread_id)
- Performance monitoring

---

## 🛠️ Troubleshooting

### Agent won't start locally

```bash
# Check logs
python -m src.agent

# Common issues:
# - Missing API key in .env
# - Wrong Python version (need 3.12+)
# - Missing dependencies: pip install -r requirements.txt
```

### Docker build fails

```bash
# Make sure you're in project root
cd /path/to/my-agent

# Build from root, not docker/ folder
docker build -t my-agent:v1.0 -f docker/Dockerfile .
```

### PostgreSQL connection issues

```json
// Check connection string in src/config/agent_config.json
{
  "agent_config": {
    "secrets": {
      "database": {
        "postgres": {
          "connection_string": "postgresql://user:password@host:5432/database"
        }
      }
    }
  }
}
```
```bash
# Test with docker-compose
docker-compose up -d
docker-compose logs postgres
```

---

## 📚 Additional Documentation

- **`FEATURES.md`** - Detailed feature documentation (auto-generated)
- **`docs/COMPLETE_GUIDE.md`** - Deep technical guide
- **`DOCKER_COMPOSE.md`** - Local development with Docker Compose

---

## ✅ Deployment Checklist

Before submitting to deployment team:

- [ ] Code customization complete
- [ ] Tested locally with `python -m src.agent`
- [ ] Tested with docker-compose (if using PostgreSQL)
- [ ] Docker image built successfully
- [ ] Docker image pushed to registry
- [ ] `agent_config.json` finalized
- [ ] Environment variables documented
- [ ] Image URL ready to share

**Ready to deploy?** Submit these to deployment team:
1. ✅ Docker image URL
2. ✅ `agent_config.json` file
3. ✅ Environment variables list

---

## 🎉 Summary

**Your Journey:**
```
Scaffold → Customize → Test → Build → Push → Deploy → 🚀
```

**Key Commands:**
```bash
# Local development
python -m src.agent

# Docker Compose testing
docker-compose up -d

# Build image
docker build -t my-agent:v1.0 -f docker/Dockerfile .

# Push image
docker push registry.example.com/my-agent:v1.0
```

**What makes this powerful:**
- ✅ One Docker image, infinite configurations
- ✅ Multi-tenant support out of the box
- ✅ Production-ready observability
- ✅ Kubernetes-native deployment

**Need help?** Check `docs/COMPLETE_GUIDE.md` for detailed technical documentation.

---

