# ProCode Agent - Complete Technical Guide

This comprehensive guide covers all technical aspects of building, customizing, and deploying your ProCode agent.

---

## Table of Contents

1. [Architecture Overview](#architecture-overview)
2. [Project Structure](#project-structure)
3. [Agent Workflow System](#agent-workflow-system)
4. [Custom Tool Development](#custom-tool-development)
5. [Configuration System](#configuration-system)
6. [Multi-Tenant Architecture](#multi-tenant-architecture)
7. [Observability & Monitoring](#observability--monitoring)
8. [Docker & Deployment](#docker--deployment)
9. [Advanced Customization](#advanced-customization)
10. [Troubleshooting](#troubleshooting)

---

## Architecture Overview

### Core Components

```
┌─────────────────────────────────────────────────────────────┐
│                     A2A Protocol Layer                       │
│              (Agent-to-Agent Communication)                  │
└─────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────┐
│                    FastAPI Server                            │
│         (Health Checks, Middleware, Routing)                 │
└─────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────┐
│                  Agent Executor                              │
│         (LangGraph Workflow Orchestration)                   │
└─────────────────────────────────────────────────────────────┘
                              ↓
┌──────────────┬──────────────┬──────────────┬──────────────┐
│ Query        │ Context      │ Response     │ Custom       │
│ Processor    │ Retriever    │ Generator    │ Nodes        │
└──────────────┴──────────────┴──────────────┴──────────────┘
                              ↓
┌──────────────┬──────────────┬──────────────┬──────────────┐
│ LLM Provider │ Tool Registry│ Observability│ Error        │
│ (Multi-LLM)  │ (Auto-disc.) │ (Langfuse)   │ Tracking     │
└──────────────┴──────────────┴──────────────┴──────────────┘
```

### Data Flow

```
User Request
    ↓
A2A Protocol (JSON-RPC)
    ↓
Rate Limiting Middleware (if enabled)
    ↓
Agent Executor
    ↓
LangGraph Workflow (State Machine)
    ↓
Node 1 → Node 2 → Node 3 → ... → Response
    ↓
Observability Tracing (Langfuse)
    ↓
Response to User
```

---

## Project Structure

### Complete Directory Layout

```
my-agent/
├── src/
│   ├── agent/
│   │   ├── __init__.py
│   │   ├── __main__.py              # FastAPI server entry point
│   │   ├── executor.py              # LangGraph workflow orchestrator
│   │   ├── state.py                 # Agent state definition
│   │   └── nodes/                   # Workflow nodes (steps)
│   │       ├── __init__.py
│   │       ├── query_processor.py   # Step 1: Understand user intent
│   │       ├── context_retriever.py # Step 2: Get relevant info
│   │       └── response_generator.py# Step 3: Generate answer
│   ├── config/
│   │   ├── __init__.py
│   │   ├── agent_config.json        # CAMS configuration
│   │   ├── cams_config_adapter.py   # CAMS schema adapter
│   │   ├── config_loader.py         # Config loading logic
│   │   └── settings.py              # Environment settings
│   ├── middleware/
│   │   ├── __init__.py
│   │   └── rate_limiting.py         # Rate limiting (if enabled)
│   ├── tools/
│   │   ├── __init__.py
│   │   ├── simple_calculator.py     # Example tool
│   │   └── text_counter.py          # Example tool
│   └── utils/
│       ├── __init__.py
│       ├── cost_calculator.py       # Token cost tracking
│       ├── error_tracking.py        # Sentry integration
│       ├── health.py                # Health check system
│       ├── langfuse_decorator.py    # Langfuse tracing
│       ├── llm_provider.py          # Multi-LLM support
│       ├── metrics.py               # Prometheus metrics
│       ├── observability.py         # Observability manager
│       ├── structured_logging.py    # JSON logging
│       └── token_estimator.py       # Token estimation
├── docker/
│   ├── Dockerfile                   # Multi-stage Docker build
│   └── docker-entrypoint.sh         # Container entrypoint
├── docs/
│   └── COMPLETE_GUIDE.md            # This file
├── .dockerignore                    # Docker build exclusions
├── .env.example                     # Environment template
├── .gitignore                       # Git exclusions
├── docker-compose.yml               # Local dev with PostgreSQL
├── DOCKER_COMPOSE.md                # Docker Compose guide
├── FEATURES.md                      # Selected features (generated)
├── README.md                        # Main user guide
└── requirements.txt                 # Python dependencies
```

---

## Agent Workflow System

### Understanding LangGraph Workflows

Your agent uses **LangGraph** to define a state machine workflow.

#### Key Concepts

**1. State:**
- Data that flows through the workflow
- Defined in `src/agent/state.py`
- Contains messages, intermediate results, metadata

**2. Nodes:**
- Individual steps in the workflow
- Each node is a function that processes state
- Located in `src/agent/nodes/`

**3. Edges:**
- Connections between nodes
- Define the flow (A → B → C)
- Can be conditional (if/else routing)

### Default Workflow

```python
# src/agent/executor.py

def build_graph(self, tenant_id: str = "default") -> StateGraph:
    workflow = StateGraph(AgentState)
    
    # Add nodes (steps)
    workflow.add_node("query_processor", query_processor_node.execute)
    workflow.add_node("context_retriever", context_retriever_node.execute)
    workflow.add_node("response_generator", response_generator_node.execute)
    
    # Define flow
    workflow.set_entry_point("query_processor")
    workflow.add_edge("query_processor", "context_retriever")
    workflow.add_edge("context_retriever", "response_generator")
    workflow.add_edge("response_generator", END)
    
    return workflow
```

**Flow:**
```
User Query
    ↓
query_processor (understand intent)
    ↓
context_retriever (get relevant info)
    ↓
response_generator (create answer)
    ↓
Return Response
```

### Customizing Workflows

#### Example 1: Add Validation Step

```python
def build_graph(self, tenant_id: str = "default") -> StateGraph:
    workflow = StateGraph(AgentState)
    
    workflow.add_node("query_processor", query_processor_node.execute)
    workflow.add_node("context_retriever", context_retriever_node.execute)
    workflow.add_node("response_generator", response_generator_node.execute)
    workflow.add_node("validator", validator_node.execute)  # NEW
    
    workflow.set_entry_point("query_processor")
    workflow.add_edge("query_processor", "context_retriever")
    workflow.add_edge("context_retriever", "response_generator")
    workflow.add_edge("response_generator", "validator")  # NEW
    workflow.add_edge("validator", END)
    
    return workflow
```

#### Example 2: Conditional Routing

```python
def route_by_intent(state: AgentState) -> str:
    """Route based on user intent."""
    intent = state.get("intent", "")
    
    if "search" in intent.lower():
        return "web_search"
    elif "calculate" in intent.lower():
        return "calculator"
    else:
        return "direct_answer"

def build_graph(self, tenant_id: str = "default") -> StateGraph:
    workflow = StateGraph(AgentState)
    
    workflow.add_node("query_processor", query_processor_node.execute)
    workflow.add_node("web_search", web_search_node.execute)
    workflow.add_node("calculator", calculator_node.execute)
    workflow.add_node("response_generator", response_generator_node.execute)
    
    workflow.set_entry_point("query_processor")
    
    # Conditional routing
    workflow.add_conditional_edges(
        "query_processor",
        route_by_intent,
        {
            "web_search": "web_search",
            "calculator": "calculator",
            "direct_answer": "response_generator"
        }
    )
    
    workflow.add_edge("web_search", "response_generator")
    workflow.add_edge("calculator", "response_generator")
    workflow.add_edge("response_generator", END)
    
    return workflow
```

#### Example 3: Loop with Retry Logic

```python
def should_retry(state: AgentState) -> str:
    """Check if response needs improvement."""
    if state.get("validation_passed", False):
        return "done"
    elif state.get("retry_count", 0) < 3:
        return "retry"
    else:
        return "done"

def build_graph(self, tenant_id: str = "default") -> StateGraph:
    workflow = StateGraph(AgentState)
    
    workflow.add_node("query_processor", query_processor_node.execute)
    workflow.add_node("response_generator", response_generator_node.execute)
    workflow.add_node("validator", validator_node.execute)
    
    workflow.set_entry_point("query_processor")
    workflow.add_edge("query_processor", "response_generator")
    workflow.add_edge("response_generator", "validator")
    
    # Loop back if validation fails
    workflow.add_conditional_edges(
        "validator",
        should_retry,
        {
            "retry": "response_generator",  # Try again
            "done": END
        }
    )
    
    return workflow
```

---

## Custom Tool Development

### Tool Basics

Tools are functions your agent can call to perform actions or retrieve information.

### Creating a Tool

**File:** `src/tools/my_custom_tool.py`

```python
from langchain_core.tools import tool
from typing import Optional

@tool
async def search_product_catalog(
    query: str,
    category: Optional[str] = None,
    max_results: int = 10
) -> dict:
    """
    Search the product catalog for items matching the query.
    
    This docstring is important! The LLM uses it to understand
    when and how to call this tool.
    
    Args:
        query: Search keywords
        category: Optional category filter (e.g., "electronics", "clothing")
        max_results: Maximum number of results to return
        
    Returns:
        Dictionary with search results and metadata
    """
    # Your implementation here
    # Example: Call your database or API
    
    try:
        # Simulate database query
        results = await your_database.search(
            query=query,
            category=category,
            limit=max_results
        )
        
        return {
            "success": True,
            "query": query,
            "category": category,
            "results": results,
            "count": len(results)
        }
    except Exception as e:
        return {
            "success": False,
            "error": str(e),
            "results": []
        }
```

### Tool Best Practices

1. **Clear Docstrings:** LLM uses this to understand the tool
2. **Type Hints:** Helps with validation
3. **Error Handling:** Always return structured errors
4. **Async Functions:** Use `async def` for I/O operations
5. **Structured Returns:** Return dicts with consistent schema

### Tool Auto-Discovery

Tools are automatically discovered from `src/tools/` directory:

```python
# src/tools/__init__.py handles auto-discovery
# Just add your file and it's registered!

src/tools/
├── __init__.py              # Auto-discovery logic
├── my_tool_1.py            # ✅ Automatically registered
├── my_tool_2.py            # ✅ Automatically registered
└── helper_functions.py     # ❌ No @tool decorator = not registered
```

### Using Tools in Nodes

```python
# src/agent/nodes/context_retriever.py

async def execute(self, state: AgentState) -> AgentState:
    """Retrieve context using tools."""
    user_query = state["messages"][-1].content
    
    # Call your custom tool
    search_results = await self.tool_registry.execute_tool(
        "search_product_catalog",
        query=user_query,
        category="electronics",
        max_results=5
    )
    
    return {
        **state,
        "search_results": search_results,
        "context": format_results(search_results)
    }
```

---

## Configuration System

### CAMS Configuration Schema

Your agent uses the **CAMS (Covasant Agent Management System)** configuration schema.

**File:** `src/config/agent_config.json`

```json
{
  "agent_instance_id": "unique-instance-id",
  "deployment_id": "deployment-id",
  "app_id": "application-id",
  "url": "https://my-agent.example.com",
  
  "agent_definition": {
    "agent_id": "my-agent",
    "name": "My Agent",
    "agent_type": "PRO_CODE",
    "version": "1.0.0",
    "description": "What your agent does",
    "capabilities": {
      "streaming": true,
      "multi_turn": true,
      "context_aware": true
    }
  },
  
  "agent_config": {
    "agent_id": "my-agent",
    "agent_name": "My Agent",
    "instructions": "You are a helpful assistant...",
    "llm_config": {
      "model": "gpt-4o",
      "temperature": 0.3,
      "max_tokens": "4096"
    },
    "llm_provider": "openai"
  },
  
  "tenant_config": [
    {
      "tenant_name": "default",
      "agent_config": {
        "instructions": "Default behavior...",
        "llm_config": {"model": "gpt-4o"}
      }
    }
  ]
}
```

### Node-Specific Configuration

You can configure different LLM settings for each node:

```json
{
  "default": {
    "agent_name": "Product Expert",
    "prompts": {
      "system": "You are a product recommendation expert...",
      "query_processor": "Analyze this query and extract intent...",
      "response_generator": "Generate a helpful response..."
    },
    "llm_config": {
      "query_processor": {
        "model": "gpt-4o",
        "temperature": 0.7,
        "max_tokens": "1000"
      },
      "context_retriever": {
        "model": "gpt-3.5-turbo",
        "temperature": 0.1,
        "max_tokens": "500"
      },
      "response_generator": {
        "model": "gpt-4o",
        "temperature": 0.3,
        "max_tokens": "2000"
      }
    }
  }
}
```

**Why different models per node?**
- **Query Processor:** Needs reasoning → Use GPT-4
- **Context Retriever:** Simple extraction → Use GPT-3.5 (cheaper)
- **Response Generator:** Quality matters → Use GPT-4

---

## Multi-Tenant Architecture

### How Multi-Tenancy Works

**One Docker image → Multiple deployments with different configs**

```
Docker Image: my-agent:v1.0
    ↓
┌─────────────────┬─────────────────┬─────────────────┐
│   Tenant A      │   Tenant B      │   Tenant C      │
│   (Premium)     │   (Budget)      │   (Enterprise)  │
├─────────────────┼─────────────────┼─────────────────┤
│ Config A        │ Config B        │ Config C        │
│ GPT-4o          │ Claude          │ Gemini          │
│ Luxury prompts  │ Budget prompts  │ Tech prompts    │
└─────────────────┴─────────────────┴─────────────────┘
```

### Tenant Configuration

```json
{
  "tenant_config": [
    {
      "tenant_name": "tenant_a",
      "tenant_description": "Premium tier",
      "agent_config": {
        "agent_name": "Premium Product Expert",
        "instructions": "You are a luxury product specialist...",
        "llm_config": {
          "response_generator": {
            "model": "gpt-4o",
            "temperature": 0.3
          }
        },
        "max_steps": 15
      }
    },
    {
      "tenant_name": "tenant_b",
      "tenant_description": "Budget tier",
      "agent_config": {
        "agent_name": "Budget Shopping Assistant",
        "instructions": "You help find affordable products...",
        "llm_config": {
          "response_generator": {
            "model": "claude-3-5-sonnet-20241022",
            "temperature": 0.2
          }
        },
        "max_steps": 10
      }
    }
  ]
}
```

### Tenant-Specific Features

**Per-Tenant Database:**
```json
{
  "tenant_a": {
    "postgres_connection_string": "postgresql://user:pass@host:5432/tenant_a_db"
  },
  "tenant_b": {
    "postgres_connection_string": "postgresql://user:pass@host:5432/tenant_b_db"
  }
}
```

**Per-Tenant Observability:**
```json
{
  "tenant_a": {
    "langfuse": {
      "public_key": "pk-lf-tenant-a",
      "secret_key": "sk-lf-tenant-a"
    }
  }
}
```

---

## Observability & Monitoring

### Langfuse Tracing (If Enabled)

**Automatic tracing of:**
- Agent execution flow
- LLM calls (prompts, responses, tokens)
- Tool invocations
- Node execution times
- Errors and exceptions

**Configuration** (in `src/config/agent_config.json`):
```json
{
  "agent_config": {
    "secrets": {
      "langfuse": {
        "enabled": true,
        "public_key": "pk-lf-xxx",
        "secret_key": "sk-lf-xxx",
        "host": "https://cloud.langfuse.com"
      }
    }
  }
}
```

**View traces at:** https://cloud.langfuse.com

### Sentry Error Tracking (If Enabled)

**Automatic capture of:**
- Exceptions with full stack traces
- Request context (tenant_id, thread_id, trace_id)
- Performance monitoring
- Breadcrumbs (execution trail)

**Configuration** (in `src/config/agent_config.json`):
```json
{
  "agent_config": {
    "secrets": {
      "sentry": {
        "enabled": true,
        "dsn": "https://xxx@xxx.ingest.sentry.io/xxx",
        "environment": "production",
        "traces_sample_rate": 0.1
      }
    }
  }
}
```

### Prometheus Metrics (If Enabled)

**Metrics exposed at:** `/metrics`

**Available metrics:**
- Request count by tenant
- Request latency (p50, p95, p99)
- Error rate
- LLM token usage
- Tool invocation count

### Health Checks

**Endpoints:**
- `/health` - Overall health status
- `/health/ready` - Kubernetes readiness probe
- `/health/live` - Kubernetes liveness probe

**Health check includes:**
- LLM provider connectivity
- Database connectivity (if PostgreSQL enabled)
- Tool availability
- Memory usage

---

## Docker & Deployment

### Multi-Stage Docker Build

**File:** `docker/Dockerfile`

```dockerfile
# Stage 1: Builder
FROM python:3.12-slim as builder
WORKDIR /app
RUN apt-get update && apt-get install -y gcc
COPY requirements.txt .
RUN pip install --no-cache-dir --user -r requirements.txt

# Stage 2: Production
FROM python:3.12-slim
WORKDIR /app
COPY --from=builder /root/.local /root/.local
COPY src/ ./src/
COPY docker/docker-entrypoint.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/docker-entrypoint.sh
ENV PATH=/root/.local/bin:$PATH
EXPOSE 9999
HEALTHCHECK --interval=30s --timeout=10s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:9999/health/live')"
ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["python", "-m", "src.agent"]
```

**Benefits:**
- ✅ Smaller final image (no build tools)
- ✅ Faster builds (cached layers)
- ✅ Production-optimized

### Build Commands

```bash
# Build
docker build -t my-agent:v1.0 -f docker/Dockerfile .

# Test
docker run -p 9999:9999 -e OPENAI_API_KEY=sk-xxx my-agent:v1.0

# Push
docker push registry.example.com/my-agent:v1.0
```

### Kubernetes Deployment

**What deployment team does:**

1. **Create Deployment:**
```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: my-agent
spec:
  replicas: 3
  template:
    spec:
      containers:
      - name: agent
        image: registry.example.com/my-agent:v1.0
        env:
        - name: OPENAI_API_KEY
          valueFrom:
            secretKeyRef:
              name: agent-secrets
              key: openai-api-key
```

2. **Inject ConfigMap:**
```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: agent-config
data:
  agent_config.json: |
    {
      "agent_config": {...}
    }
```

3. **Create Service & Ingress:**
```yaml
apiVersion: v1
kind: Service
metadata:
  name: my-agent
spec:
  ports:
  - port: 80
    targetPort: 9999
---
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: my-agent
spec:
  rules:
  - host: my-agent.example.com
    http:
      paths:
      - path: /
        backend:
          service:
            name: my-agent
            port:
              number: 80
```

---

## Advanced Customization

### Custom State Fields

**Edit:** `src/agent/state.py`

```python
from typing import TypedDict, Annotated, Sequence, Optional
from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages

class AgentState(TypedDict):
    """Custom state for your agent."""
    
    # Core fields (required)
    messages: Annotated[Sequence[BaseMessage], add_messages]
    
    # Your custom fields
    user_query: str
    intent: str
    search_results: list[dict]
    product_recommendations: list[dict]
    user_preferences: dict
    conversation_history: list[dict]
    metadata: dict
```

### Custom Middleware

**Create:** `src/middleware/custom_middleware.py`

```python
from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware

class CustomMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        # Pre-processing
        request.state.custom_data = "value"
        
        # Call endpoint
        response = await call_next(request)
        
        # Post-processing
        response.headers["X-Custom-Header"] = "value"
        
        return response
```

**Register in:** `src/agent/__main__.py`

```python
from src.middleware.custom_middleware import CustomMiddleware

app.add_middleware(CustomMiddleware)
```

### Custom LLM Provider

**Edit:** `src/utils/llm_provider.py`

```python
def get_llm(model: str, temperature: float = 0.7):
    """Get LLM instance."""
    if model.startswith("custom-"):
        # Your custom provider
        return YourCustomLLM(
            model=model,
            temperature=temperature
        )
    # ... existing providers
```

---

## Troubleshooting

### Common Issues

#### 1. Import Errors

**Problem:** `ModuleNotFoundError: No module named 'src'`

**Solution:**
```bash
# Run from project root
python -m src.agent

# Not: python src/agent/__main__.py
```

#### 2. Docker Build Fails

**Problem:** `COPY ../src/ ./src/` fails

**Solution:**
```bash
# Build from project root
docker build -t my-agent:v1.0 -f docker/Dockerfile .

# Build context must be root (.)
```

#### 3. PostgreSQL Connection Fails

**Problem:** `could not connect to server`

**Solution:**
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
docker-compose exec postgres psql -U postgres
```

#### 4. LLM API Errors

**Problem:** `AuthenticationError: Invalid API key`

**Solution:**
```bash
# Check .env file
cat .env | grep API_KEY

# Ensure no quotes around keys
OPENAI_API_KEY=sk-xxx  # ✅ Correct
OPENAI_API_KEY="sk-xxx"  # ❌ Wrong (quotes included)
```

#### 5. Langfuse Not Tracing

**Problem:** No traces appearing in Langfuse

**Solution:**
```bash
# Check keys are set in src/config/agent_config.json under secrets.langfuse
# Verify enabled: true and correct public_key / secret_key values

# Check logs for errors
python -m src.agent 2>&1 | grep -i langfuse
```

### Debug Mode

**Enable debug logging:**
```bash
# .env
LOG_LEVEL=DEBUG
```

**View detailed traces:**
```python
# Add to your code
import logging
logging.basicConfig(level=logging.DEBUG)
```

---

## Performance Optimization

### Caching Strategies

**1. LLM Response Caching:**
```python
from functools import lru_cache

@lru_cache(maxsize=1000)
async def cached_llm_call(prompt: str, model: str):
    return await llm.ainvoke(prompt)
```

**2. Tool Result Caching:**
```python
import redis
cache = redis.Redis(host='localhost', port=6379)

async def search_with_cache(query: str):
    cached = cache.get(f"search:{query}")
    if cached:
        return json.loads(cached)
    
    result = await search(query)
    cache.setex(f"search:{query}", 3600, json.dumps(result))
    return result
```

### Async Best Practices

**Use async for I/O operations:**
```python
# ✅ Good
async def fetch_data():
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as response:
            return await response.json()

# ❌ Bad (blocking)
def fetch_data():
    response = requests.get(url)
    return response.json()
```

---

## Security Best Practices

### 1. API Key Management

**Never hardcode keys:**
```python
# ❌ Bad
openai_api_key = "sk-xxx"

# ✅ Good
openai_api_key = os.getenv("OPENAI_API_KEY")
```

### 2. Input Validation

**Validate user inputs:**
```python
from pydantic import BaseModel, validator

class UserQuery(BaseModel):
    query: str
    
    @validator('query')
    def validate_query(cls, v):
        if len(v) > 10000:
            raise ValueError("Query too long")
        return v
```

### 3. Rate Limiting

**Protect your endpoints:**
```python
# Already included if rate_limiting feature enabled
# Configurable in .env:
RATE_LIMIT_PER_MINUTE=60
RATE_LIMIT_PER_HOUR=1000
```

---

## Additional Resources

- **LangGraph Documentation:** https://langchain-ai.github.io/langgraph/
- **LangChain Documentation:** https://python.langchain.com/
- **A2A Protocol Spec:** https://github.com/covasant/a2a-sdk
- **Langfuse Docs:** https://langfuse.com/docs
- **Sentry Docs:** https://docs.sentry.io/

---

**Need help?** Contact your deployment team or check the main README.md for quick start guide.
