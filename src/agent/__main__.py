"""
Agent server entry point.
Run with: python -m src.agent
"""

import asyncio
import json
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

# Force line-buffered stdout/stderr (Windows/piped terminals default to
# fully block-buffered), so log lines show up immediately instead of
# sitting in a buffer that only flushes when it fills or the process exits.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(line_buffering=True)
    except (AttributeError, ValueError):
        pass  # stream doesn't support reconfigure

# Route logging through a QueueHandler: the real handlers (console + file)
# run on a dedicated QueueListener thread, so a stuck/unread console pipe
# can block only that listener thread — never migrations, the wake loop,
# or incoming requests. Tail logs\agent.log directly for a lag-free view.
import logging as _logging
import queue as _queue
from logging.handlers import QueueHandler as _QueueHandler, QueueListener as _QueueListener

_LOG_DIR = Path(__file__).resolve().parents[2] / "logs"
_LOG_DIR.mkdir(exist_ok=True)
_log_formatter = _logging.Formatter("%(asctime)s %(levelname)s:%(name)s:%(message)s")

try:
    sys.stdout.reconfigure(errors="backslashreplace")
except (AttributeError, ValueError):
    pass  # non-reconfigurable stream — fall back to default error handling
_console_handler = _logging.StreamHandler(sys.stdout)
_console_handler.setFormatter(_log_formatter)
_file_handler = _logging.FileHandler(_LOG_DIR / "agent.log", encoding="utf-8")
_file_handler.setFormatter(_log_formatter)

_log_queue: "_queue.Queue" = _queue.Queue(-1)
_queue_handler = _QueueHandler(_log_queue)
_queue_listener = _QueueListener(_log_queue, _console_handler, _file_handler, respect_handler_level=False)
_queue_listener.start()

_root_logger = _logging.getLogger()
_root_logger.handlers.clear()  # drop the default StreamHandler logging.basicConfig() will otherwise add
_root_logger.addHandler(_queue_handler)
_root_logger.setLevel(_logging.INFO)
# cams_otel_lib's Otel_Client.initialize_otel_client() calls logging.basicConfig(level=INFO) again
# later — make that a no-op for handler setup so it can't re-add a blocking direct StreamHandler.
_logging.basicConfig = lambda *a, **k: None  # noqa: E731

import uvicorn
from fastapi import Request, Body, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.tasks import InMemoryTaskStore
from a2a.server.apps import A2AFastAPIApplication
from a2a.types import AgentCard, AgentCapabilities, AgentProvider, AgentSkill

from .executor import AgentExecutor
from src.agent.edp.loop import EdpWakeLoop
from src.agent.edp.api import router as edp_router
from src.config.settings import settings
from src.middleware.claims_middleware import OtelContextMiddleware
from src.utils.health import get_health_checker
from cams_otel_lib import Logger as logger, Otel_Client, otel_trace
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.requests import RequestsInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor


def _read_agent_config_field(field: str, default: str = "N/A") -> str:
    """Read a top-level field from agent_config.json. APP_CONFIG_PATH takes priority."""
    try:
        ext = os.getenv("APP_CONFIG_PATH")
        if ext:
            p = Path(ext)
            if p.exists():
                with open(p) as f:
                    data = json.load(f)
                    # Support both top-level and nested runtime_context
                    return data.get(field) or data.get("runtime_context", {}).get(field, default)
        config_path = Path(__file__).parent.parent / "config" / "agent_config.json"
        with open(config_path) as f:
            data = json.load(f)
            return data.get(field) or data.get("runtime_context", {}).get(field, default)
    except Exception:
        return default







@otel_trace
def create_agent_card() -> AgentCard:
    """Build the A2A agent card from agent_config.json agent_definition."""
    from src.config.cams_config_adapter import CAMSConfigAdapter

    agent_def = {}
    capabilities_cfg = {}
    provider_cfg = {}
    skills_cfg = []
    raw_version = "1.0.0"
    try:
        ext = os.getenv("APP_CONFIG_PATH")
        cfg_path = Path(ext) if ext else Path(__file__).parent.parent / "config" / "agent_config.json"
        if cfg_path.exists():
            import json as _json
            raw = _json.loads(cfg_path.read_text())
            adapter = CAMSConfigAdapter(raw)
            agent_def = adapter.get_agent_definition()
            capabilities_cfg = agent_def.get("capabilities", {})
            provider_cfg = agent_def.get("provider", {})
            skills_cfg = agent_def.get("skills", [])
            raw_version = agent_def.get("version", "1.0.0")
    except Exception as e:
        logger.warning(f"Could not load agent_definition from config, using defaults: {e}")

    if skills_cfg:
        skills = [
            AgentSkill(
                id=s.get("id", f"skill_{i}"),
                name=s.get("name", f"Skill {i}"),
                description=s.get("description", ""),
                tags=s.get("tags", []),
                examples=s.get("examples", []),
                input_modes=s.get("inputModes", ["text/plain"]),
                output_modes=s.get("outputModes", ["text/plain"]),
            )
            for i, s in enumerate(skills_cfg)
        ]
    else:
        skills = [
            AgentSkill(
                id="search_and_answer",
                name="Search & Answer",
                description="Search knowledge base and provide detailed answers",
                tags=["search", "qa", "knowledge"],
                examples=["What is machine learning?", "Explain quantum computing concepts"],
                input_modes=["text/plain"],
                output_modes=["text/plain"],
            ),
        ]

    return AgentCard(
        name=agent_def.get("name") or settings.agent_name,
        description=agent_def.get("description") or settings.agent_description,
        url=settings.agent_url,
        version=raw_version,
        protocol_version="0.3.0",
        preferred_transport="HTTP+JSON",
        default_input_modes=agent_def.get("defaultInputModes", ["text/plain"]),
        default_output_modes=agent_def.get("defaultOutputModes", ["text/plain"]),
        capabilities=AgentCapabilities(
            streaming=capabilities_cfg.get("streaming", settings.streaming_enabled),
            push_notifications=capabilities_cfg.get("push_notifications", False),
        ),
        skills=skills,
        supports_authenticated_extended_card=False,
        provider=AgentProvider(
            organization=provider_cfg.get("organization", "CAMS"),
            url=provider_cfg.get("url", ""),
        ),
        documentation_url=None,
        icon_url=None,
    )


def build_app() -> FastAPI:
    """
    Build the FastAPI app (agent card, A2A handler, EDP router, middleware,
    health endpoints). Synchronous and side-effect-light so it can be called
    fresh on every restart — used directly by main() for a normal run, and
    as a uvicorn factory target (see run_with_reload()) so --reload actually
    re-imports and rebuilds it on every code change instead of serving stale
    bytecode from the first import.
    """
    Otel_Client.initialize_otel_client(
        service_name=settings.agent_name,
        environment=os.getenv("ENVIRONMENT", os.getenv("ENV", "dev")),
        agent_id=_read_agent_config_field("instance_id", default="N/A"),
    )

    RequestsInstrumentor().instrument()
    HTTPXClientInstrumentor().instrument()

    logger.info(f"Starting agent: {settings.agent_name} on {settings.host}:{settings.port}")

    edp_loop = EdpWakeLoop()
    edp_loop_enabled = os.getenv("EDP_LOOP_ENABLED", "true").lower() == "true"

    agent_card = create_agent_card()
    executor = AgentExecutor()

    task_store = InMemoryTaskStore()
    request_handler = DefaultRequestHandler(
        agent_executor=executor,
        task_store=task_store,
    )

    a2a_app = A2AFastAPIApplication(
        agent_card=agent_card,
        http_handler=request_handler,
    )

    app = a2a_app.build()

    app.include_router(edp_router)

    @asynccontextmanager
    async def _edp_lifespan(app):
        if edp_loop_enabled:
            await edp_loop.start()
            logger.info("EDP 24/7 wake loop enabled")
        else:
            logger.info("EDP 24/7 wake loop DISABLED (EDP_LOOP_ENABLED=false)")
        logger.info("=" * 60)
        logger.info(">>> AGENT STARTUP COMPLETE — ready to serve requests <<<")
        logger.info("=" * 60)
        yield
        if edp_loop_enabled:
            await edp_loop.stop()

    app.router.lifespan_context = _edp_lifespan

    FastAPIInstrumentor().instrument_app(app)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "https://aifabric-frontend.dev.cams.covasant.io",
            "http://localhost:3000",
            "http://localhost:3001",
            "http://localhost:8000",
            "http://localhost:5173",
            "http://127.0.0.1:3000",
            "http://127.0.0.1:3001",
            "http://127.0.0.1:8000",
            "http://127.0.0.1:5173",
        ],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )


    app.add_middleware(OtelContextMiddleware)

    health_checker = get_health_checker()
    health_checker.register_liveness_check(edp_loop.liveness_check)

    @app.get("/health")
    async def health_check():
        health_status = await health_checker.get_health_status()
        status_code = 503 if health_status["status"] == "unhealthy" else 200
        from fastapi import Response
        return Response(
            content=json.dumps(health_status),
            status_code=status_code,
            media_type="application/json",
        )

    @app.get("/health/ready")
    async def readiness_check():
        is_ready = await health_checker.is_ready()
        if is_ready:
            return {"status": "ready"}
        from fastapi import Response
        return Response(
            content='{"status": "not ready"}',
            status_code=503,
            media_type="application/json",
        )

    @app.get("/health/live")
    async def liveness_check():
        is_alive = await health_checker.is_alive()
        if is_alive:
            return {"status": "alive"}
        from fastapi import Response
        return Response(
            content='{"status": "not alive"}',
            status_code=503,
            media_type="application/json",
        )



    @app.post("/agent/run")
    async def agent_run_endpoint(http_request: Request, body: dict = Body(...)):
        """
        Direct agent execution endpoint.

        Request format:
        {
            "query": "Your question here",
            "conversation_id": "optional-conversation-id"
        }

        Response format:
        {
            "response": "Agent's answer",
            "instance_id": "...",
            "conversation_id": "...",
            "user_id": "..."
        }
        """
        try:
            import uuid
            from langchain_core.messages import HumanMessage

            query = body.get("query", "")
            if not query:
                return {"error": "Query is required"}

            # Read runtime context from config (injected at scaffold time from JWT)
            tenant_id = _read_agent_config_field("tenant_id", default="default")
            user_id = _read_agent_config_field("user_id", default="")
            instance_id = _read_agent_config_field("instance_id", default="N/A")

            thread_id = body.get("conversation_id") or f"thread_{uuid.uuid4().hex[:16]}"

            logger.info(f"Agent request received: query_length={len(query)}, conversation_id={thread_id}")

            trace_id = uuid.uuid4().hex
            parent_span_id = uuid.uuid4().hex[:16]

            initial_state = {
                "messages": [HumanMessage(content=query)],
                "search_query": "",
                "retrieved_context": "",
                "final_response": "",
                "tenant_id": tenant_id,
                "thread_id": thread_id,
                "needs_retrieval": True,
                "_langfuse_trace_id": trace_id,
                "_langfuse_span_id": parent_span_id,
                "litellm_headers": None,
            }

            final_state = await executor._run_graph(initial_state, tenant_id, thread_id)
            response_text = final_state.get("final_response", "No response generated")

            logger.info(f"Agent response generated: response_length={len(response_text)}, conversation_id={thread_id}")

            return {
                "response": response_text,
                "instance_id": instance_id,
                "conversation_id": thread_id,
                "user_id": user_id,
            }

        except Exception as e:
            logger.error(f"Agent error: {str(e)}")
            return {"error": str(e)}

    return app


async def main():
    """Direct (non-reload) run: build the app once, serve once."""
    app = build_app()
    config = uvicorn.Config(
        app,
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.lower(),
    )
    server = uvicorn.Server(config)
    logger.info(f"Agent ready at http://{settings.host}:{settings.port}")
    try:
        await server.serve()
    except KeyboardInterrupt:
        logger.info("Shutting down gracefully")


def run_with_reload() -> None:
    """
    --reload run: hands off to uvicorn's own reloader supervisor, which
    watches reload_dirs and, on every change, spawns a fresh subprocess that
    re-imports build_app() via this string reference (factory=True) — so
    edits actually take effect, unlike the old flag which was parsed nowhere
    and silently did nothing.
    """
    project_root = Path(__file__).resolve().parents[2]
    uvicorn.run(
        "src.agent.__main__:build_app",
        factory=True,
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.lower(),
        reload=True,
        reload_dirs=[
            str(project_root / "src"),
            str(project_root / "global_email_service"),
            str(project_root / "mock_cbos"),
        ],
    )


if __name__ == "__main__":
    try:
        if "--reload" in sys.argv[1:]:
            run_with_reload()
        else:
            asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Agent stopped by user")
        sys.exit(0)
