# OTEL Transformation — Agent-Template

This document records every step executed on the Agent-Template project to apply the OTEL Transformation using the `platform_sdk_common` wheel.

---

## Overview

The transformation wires the **Covasant platform SDK** into the Agent-Template so that every agent scaffolded from this template automatically emits structured logs, traces, and spans to stdout. The OTEL Collector DaemonSet picks these up from pod stdout and forwards them to OpenSearch for analysis in the CAMS Control Tower.

---

## Steps Executed

### Step 1 — Install platform_sdk_common wheel
The wheel `platform_sdk_common-0.1.1-py3-none-any.whl` provides:
- `platform_sdk.common.otel_client` — `Otel_Client`, `Logger`, `otel_trace`
- `platform_sdk.common.request_context` — `RequestContext`

Add to `requirements.txt`:
```
platform_sdk_common @ file:///path/to/platform_sdk_common-0.1.1-py3-none-any.whl
```

---

### Step 2 — Replace all loggers with platform SDK Logger

**Rule applied:** Every file containing `import logging` + `logger = logging.getLogger(__name__)` (or `from src.utils.structured_logging import get_logger` + `logger = get_logger(__name__)`) had those lines replaced with:

```python
from platform_sdk.common.otel_client import Logger as logger, otel_trace
```

**Files modified:**

| File | Original pattern | Replaced with |
|------|-----------------|---------------|
| `src/agent/nodes/agent_node.py` | `import logging` + `logging.getLogger` | platform SDK Logger |
| `src/agent/nodes/query_processor.py` | same | platform SDK Logger |
| `src/agent/nodes/context_retriever.py` | same | platform SDK Logger |
| `src/agent/nodes/response_generator.py` | same | platform SDK Logger |
| `src/utils/langfuse_decorator.py` | same | platform SDK Logger |
| `src/utils/observability.py` | same | platform SDK Logger |
| `src/utils/llm_provider.py` | same | platform SDK Logger |
| `src/config/agent_config.py` | same | platform SDK Logger |
| `src/config/cams_config_adapter.py` | same | platform SDK Logger |
| `src/config/config_loader.py` | same | platform SDK Logger |
| `src/config/secrets_loader.py` | same | platform SDK Logger |
| `src/tools/registry.py` | same | platform SDK Logger |
| `src/utils/cost_calculator.py` | same | platform SDK Logger |
| `src/utils/token_estimator.py` | same | platform SDK Logger |
| `src/agent/__main__.py` | `get_logger` from structured_logging | platform SDK Logger |
| `src/agent/executor.py` | `get_logger` from structured_logging | platform SDK Logger |
| `src/utils/error_tracking.py` | `get_logger` from structured_logging | platform SDK Logger |
| `src/utils/health.py` | `get_logger` from structured_logging | platform SDK Logger |
| `src/utils/metrics.py` | `get_logger` from structured_logging | platform SDK Logger |
| `src/middleware/rate_limiting.py` | `get_logger` from structured_logging | platform SDK Logger |

---

### Step 3 — Created `src/middleware/claims_middleware.py`

New file created. This middleware:
1. Intercepts every HTTP request.
2. Decodes the JWT payload from the `Authorization: Bearer <token>` header (without verification — the CAMS gateway has already validated the token).
3. Extracts claims: `user_uuid` → `userid`, `tenant_uuid` → `tenant_name`, `scope` → `scope_name`, `app_name` → `app_name`.
4. Creates a `RequestContext` with those values plus `AGENT_ID` from env var.
5. Calls `RequestContext.set_current_request_context(requestContext)` so every log/span in the request gets the user context automatically.
6. Calls `Otel_Client.initialize_otel_client(service_name=settings.agent_name, environment=os.getenv("ENVIRONMENT"), agent_id=agent_id)`.

```python
# Key snippet added in ClaimsMiddleware.dispatch()
request_context = RequestContext(
    request_id=request_id,
    tenant_name=claims.get("tenant_uuid", "N/A"),
    scope_name=claims.get("scope", "N/A"),
    app_name=claims.get("app_name", "N/A"),
    userid=claims.get("user_uuid", "N/A"),
    agent_id=os.getenv("AGENT_ID", "N/A"),
)
RequestContext.set_current_request_context(request_context)
Otel_Client.initialize_otel_client(
    service_name=settings.agent_name,
    environment=os.getenv("ENVIRONMENT", os.getenv("ENV", "dev")),
    agent_id=os.getenv("AGENT_ID", "N/A"),
)
```

**Note on parameter names:** The manager's prompt used `tenant_id`, `scope`, `user_id` as kwargs but `RequestContext.__init__` requires `tenant_name`, `scope_name`, `userid`. The correct names were used.

---

### Step 4 — Updated `src/agent/__main__.py`

1. Added `import os`.
2. Removed `from src.utils.structured_logging import get_logger, configure_logging`.
3. Removed `configure_logging(...)` call and `logger = get_logger(__name__)`.
4. Added `from platform_sdk.common.otel_client import Logger as logger, Otel_Client, otel_trace`.
5. Added `from src.middleware.claims_middleware import ClaimsMiddleware`.
6. Added in `main()` as the **first statement** before any logging:
   ```python
   Otel_Client.initialize_otel_client(
       service_name=settings.agent_name,
       environment=os.getenv("ENVIRONMENT", os.getenv("ENV", "dev")),
   )
   ```
7. Added `app.add_middleware(ClaimsMiddleware)` after CORS middleware.
8. Added `@otel_trace` to all endpoint functions and `create_agent_card()`.
9. Did **NOT** annotate `main()` — it is the lifespan method where OTEL is initialized.

---

### Step 5 — Annotated all Python functions with `@otel_trace`

`@otel_trace` was added to every function and method in every `.py` file across the project, except:
- `main()` in `src/agent/__main__.py` (OTEL is initialized here)
- Decorator factories and `@contextmanager` decorated functions in `langfuse_decorator.py` (`trace_node`, `trace_generation`)
- `@property` methods

**Files annotated:**
- `src/agent/executor.py` — all methods of `AgentExecutor`
- `src/agent/nodes/agent_node.py` — `AgentNode`, `ToolNode`, `should_continue`
- `src/agent/nodes/query_processor.py` — `QueryProcessorNode`
- `src/agent/nodes/context_retriever.py` — `ContextRetrieverNode`
- `src/agent/nodes/response_generator.py` — `ResponseGeneratorNode`
- `src/utils/langfuse_decorator.py` — `create_generation_trace`
- `src/utils/observability.py` — all `ObservabilityManager` methods, module-level functions
- `src/utils/error_tracking.py` — all `ErrorTracker` methods, module-level functions
- `src/utils/health.py` — all `HealthChecker` methods, `get_health_checker`
- `src/utils/metrics.py` — all `MetricsCollector` methods, `get_metrics_collector`
- `src/middleware/rate_limiting.py` — all `SlidingWindowRateLimiter` methods, `get_rate_limiter`
- `src/middleware/claims_middleware.py` — `dispatch`, `_decode_jwt_claims`
- `src/utils/llm_provider.py` — all functions
- `src/config/agent_config.py` — all functions
- `src/config/cams_config_adapter.py` — all `CAMSConfigAdapter` methods, `load_cams_config`
- `src/config/config_loader.py` — all `ConfigLoader` methods, `get_config_loader`, `reset_config_loader`
- `src/config/secrets_loader.py` — all `SecretsLoader` methods, `get_secrets_loader`
- `src/tools/registry.py` — all `ToolRegistry` methods
- `src/utils/cost_calculator.py` — all functions
- `src/utils/token_estimator.py` — all functions

---

### Step 6 — Updated `src/utils/error_tracking.py` — `_before_send`

The `_before_send` Sentry hook was updated to read tenant/user context from `RequestContext` instead of the old `ContextVar`-based context vars (`tenant_id_var`, `thread_id_var`, `trace_id_var`), aligning it with the platform SDK pattern.

---

## Environment Variables Required at Deployment

CAMS must inject these env vars into the pod for full OTEL functionality:

| Variable | Purpose | Example |
|----------|---------|---------|
| `OTEL_ENABLED` | Enable/disable OTEL | `"true"` |
| `OTEL_CONSOLE_LOGGING` | Emit logs to stdout | `"true"` |
| `OTEL_LOGGING_ENABLED` | Enable log provider | `"true"` |
| `OTEL_TRACING_ENABLED` | Enable trace provider | `"true"` |
| `OTEL_CONSOLE_TRACING` | Emit traces to stdout | `"true"` |
| `OTEL_ENDPOINT` | Remote OTLP endpoint (optional) | `"http://otel-collector:4317"` |
| `OTEL_REMOTE_LOGGING` | Send logs to OTLP endpoint | `"true"` |
| `OTEL_REMOTE_TRACING` | Send traces to OTLP endpoint | `"true"` |
| `AGENT_ID` | Agent instance ID from CAMS | injected by CAMS |
| `ENVIRONMENT` | Deployment environment | `"dev"` / `"prod"` |
| `OTEL_CONFIG_UUID` | Stable UUID for OTEL config | injected by CAMS |

---

## Log Format

**At startup (no request context):**
```
[TIME=2026-05-08 10:30:45] - [MODULE=src.agent.__main__] - [FILE=/app/src/agent/__main__.py] -[FUNCTION=main] -[LINE_NUMBER=35] - [MESSAGE=Starting agent: MyAgent on 0.0.0.0:9999]
```

**During a live request (RequestContext set by ClaimsMiddleware):**
```
[TIME=2026-05-08 10:31:22] - [REQUEST_ID=x-req-abc-123] - [TENANT=tenant-uuid-xyz] - [SCOPE=default] - [APP=my-agent] - [USER=user-uuid-abc] - [AGENT_ID=agent-inst-001] - [MODULE=src.agent.nodes.query_processor] - [FILE=/app/src/agent/nodes/query_processor.py] -[FUNCTION=execute] -[LINE_NUMBER=88] - [MESSAGE=Generated search query: machine learning frameworks]
```

---

## Collection Flow

```
Agent Pod (stdout)
    ↓  OTEL-formatted logs, traces, spans
OTEL Collector DaemonSet (auto-collects from all pod stdout)
    ↓
OpenSearch
    ↓
CAMS Control Tower (analysis & dashboards)
```

Logs are collected **automatically** — no manual configuration needed in the agent code after deployment. The DaemonSet handles collection as long as the env vars above are set.
