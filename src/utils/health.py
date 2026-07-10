"""
Health check utilities for component and system health monitoring.
Provides Kubernetes-compatible readiness and liveness checks.
"""

import asyncio
from typing import Awaitable, Callable, Dict, Any, Optional, List, Tuple
from enum import Enum
from datetime import datetime

from cams_otel_lib import Logger as logger, otel_trace

# A liveness probe returns (is_alive, reason) — reason is always logged when
# is_alive is False, so a tripped check is diagnosable from logs alone.
LivenessCheck = Callable[[], Awaitable[Tuple[bool, str]]]


class HealthStatus(str, Enum):
    """Health status levels."""

    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"


class ComponentHealth:
    """
    Individual component health check result.

    Attributes:
        name: Component name
        status: Health status
        message: Status message
        details: Additional details
        checked_at: Timestamp of check
    """

    def __init__(
        self,
        name: str,
        status: HealthStatus,
        message: str = "",
        details: Optional[Dict[str, Any]] = None,
    ):
        self.name = name
        self.status = status
        self.message = message
        self.details = details or {}
        self.checked_at = datetime.utcnow().isoformat() + "Z"

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "name": self.name,
            "status": self.status.value,
            "message": self.message,
            "details": self.details,
            "checked_at": self.checked_at,
        }


class HealthChecker:
    """
    Health checker with component-level checks.

    Supports:
    - LLM API availability checks
    - Database connectivity checks
    - Tool readiness checks
    - Kubernetes readiness/liveness probes
    """

    def __init__(self):
        """Initialize health checker."""
        self.startup_time = datetime.utcnow()
        self._components: List[ComponentHealth] = []
        self._liveness_checks: List[LivenessCheck] = []

    def register_liveness_check(self, check: LivenessCheck) -> None:
        """
        Register an additional liveness probe beyond the default "can we
        execute code" check.

        Without this, is_alive() was a no-op that always returned True —
        if any background task (e.g. EdpWakeLoop's 24/7 cycle) got wedged
        on an await that never returns (an unresponsive downstream call
        with no timeout), the HTTP server would keep answering the
        Kubernetes liveness probe with 200 forever, and the pod would never
        get restarted even though the actual work silently stopped. Each
        registered check is awaited on every /health/live call; any one of
        them reporting not-alive fails the whole probe.
        """
        self._liveness_checks.append(check)

    @otel_trace
    async def check_llm_availability(self) -> ComponentHealth:
        """
        Check if an LLM is available — either a direct provider API key
        (settings.*_api_key) or the LiteLLM gateway configured in
        agent_config.json -> secrets.litellm (see src.utils.llm_provider,
        which routes through the gateway whenever it's enabled, independent
        of whether a direct provider key is set).

        Returns:
            ComponentHealth with LLM status
        """
        try:
            from src.config.settings import settings
            from src.utils.llm_provider import _get_litellm_config

            # Check which direct-key providers are configured
            available_providers = []

            if settings.openai_api_key:
                available_providers.append("openai")

            if settings.anthropic_api_key:
                available_providers.append("anthropic")

            if settings.google_api_key:
                available_providers.append("google")

            litellm_config = _get_litellm_config()
            litellm_enabled = bool(litellm_config.get("enabled")) and bool(
                litellm_config.get("base_url")
            )

            if not available_providers and not litellm_enabled:
                return ComponentHealth(
                    name="llm",
                    status=HealthStatus.UNHEALTHY,
                    message="No LLM API keys and no LiteLLM gateway configured",
                    details={"configured_providers": [], "litellm_enabled": False},
                )

            # Try a lightweight check — don't make an actual API call, just
            # instantiate the model client for whichever path is active.
            try:
                from src.config.agent_config import load_agent_config
                from src.utils.llm_provider import get_llm_model

                if litellm_enabled:
                    tenant_cfg = load_agent_config().get("default", {})
                    response_cfg = tenant_cfg.get("llm_config", {}).get("response", {})
                    provider = response_cfg.get("provider", "openai")
                    model = response_cfg.get("model")
                else:
                    provider = available_providers[0]
                    model = None

                get_llm_model(provider, model_name=model)

                return ComponentHealth(
                    name="llm",
                    status=HealthStatus.HEALTHY,
                    message=(
                        f"LLM available via LiteLLM gateway ({litellm_config.get('base_url')})"
                        if litellm_enabled
                        else f"LLM providers available: {', '.join(available_providers)}"
                    ),
                    details={
                        "configured_providers": available_providers,
                        "litellm_enabled": litellm_enabled,
                        "primary_provider": provider,
                    },
                )
            except Exception as e:
                logger.warning(f"LLM health check failed: {e}")
                return ComponentHealth(
                    name="llm",
                    status=HealthStatus.DEGRADED,
                    message=f"LLM configured but instantiation warning: {str(e)}",
                    details={
                        "configured_providers": available_providers,
                        "litellm_enabled": litellm_enabled,
                    },
                )

        except Exception as e:
            logger.error(f"LLM health check error: {e}")
            return ComponentHealth(
                name="llm",
                status=HealthStatus.UNHEALTHY,
                message=f"LLM check failed: {str(e)}",
            )

    @otel_trace
    async def check_database_connectivity(self) -> ComponentHealth:
        """
        Check connectivity of the actual EDP database (sqlite or postgres,
        whichever src.agent.edp.config resolved at startup — see
        EdpBootstrapConfig.database_url).

        Returns:
            ComponentHealth with database status
        """
        try:
            from sqlalchemy import text

            from src.agent.edp.database import get_session

            async with get_session() as session:
                await session.execute(text("SELECT 1"))

            return ComponentHealth(
                name="database",
                status=HealthStatus.HEALTHY,
                message="EDP database connection successful",
            )

        except RuntimeError as e:
            # get_session() raises this when init_database() hasn't run yet.
            return ComponentHealth(
                name="database",
                status=HealthStatus.UNHEALTHY,
                message=f"EDP database not initialized: {e}",
            )
        except Exception as e:
            logger.error(f"Database health check failed: {e}")
            return ComponentHealth(
                name="database",
                status=HealthStatus.UNHEALTHY,
                message=f"Database connection failed: {str(e)}",
            )

    @otel_trace
    async def check_tools_readiness(self) -> ComponentHealth:
        """
        Check if tools are loaded and ready.

        Returns:
            ComponentHealth with tools status
        """
        try:
            from src.tools import get_available_tools

            tools = get_available_tools()
            tool_count = len(tools)
            tool_names = [t.name for t in tools]

            if tool_count == 0:
                return ComponentHealth(
                    name="tools",
                    status=HealthStatus.DEGRADED,
                    message="No tools loaded (agent will work but with limited capabilities)",
                    details={"tool_count": 0, "tools": []},
                )

            return ComponentHealth(
                name="tools",
                status=HealthStatus.HEALTHY,
                message=f"{tool_count} tools loaded and ready",
                details={"tool_count": tool_count, "tools": tool_names},
            )

        except Exception as e:
            logger.error(f"Tools health check error: {e}")
            return ComponentHealth(
                name="tools",
                status=HealthStatus.UNHEALTHY,
                message=f"Tools check failed: {str(e)}",
            )

    @otel_trace
    async def check_observability(self) -> ComponentHealth:
        """
        Check observability (Langfuse) configuration.

        Returns:
            ComponentHealth with observability status
        """
        try:
            from src.utils.observability import get_observability_manager

            obs = get_observability_manager()

            if not obs.enabled:
                return ComponentHealth(
                    name="observability",
                    status=HealthStatus.HEALTHY,
                    message="Observability not configured (optional)",
                    details={"enabled": False},
                )

            return ComponentHealth(
                name="observability",
                status=HealthStatus.HEALTHY,
                message="Langfuse observability configured",
                details={"enabled": True, "provider": "langfuse"},
            )

        except Exception as e:
            logger.warning(f"Observability health check warning: {e}")
            return ComponentHealth(
                name="observability",
                status=HealthStatus.HEALTHY,
                message="Observability check skipped (optional feature)",
                details={"enabled": False},
            )

    @otel_trace
    async def check_metrics(self) -> ComponentHealth:
        """
        Check metrics collector status.

        Returns:
            ComponentHealth with metrics status
        """
        try:
            from src.config.settings import settings

            if not settings.metrics_enabled:
                return ComponentHealth(
                    name="metrics",
                    status=HealthStatus.HEALTHY,
                    message="Metrics disabled",
                    details={"enabled": False},
                )

            from src.utils.metrics import get_metrics_collector

            metrics = get_metrics_collector()

            return ComponentHealth(
                name="metrics",
                status=HealthStatus.HEALTHY,
                message="Prometheus metrics active",
                details={"enabled": metrics.enabled, "endpoint": "/metrics"},
            )

        except ModuleNotFoundError:
            return ComponentHealth(
                name="metrics",
                status=HealthStatus.HEALTHY,
                message="Metrics collector not implemented in this build",
                details={"enabled": False},
            )
        except Exception as e:
            logger.error(f"Metrics health check error: {e}")
            return ComponentHealth(
                name="metrics",
                status=HealthStatus.UNHEALTHY,
                message=f"Metrics check failed: {str(e)}",
            )

    @otel_trace
    async def check_rate_limiter(self) -> ComponentHealth:
        """
        Check rate limiter status.

        Returns:
            ComponentHealth with rate limiter status
        """
        try:
            from src.config.settings import settings

            if not settings.rate_limit_enabled:
                return ComponentHealth(
                    name="rate_limiter",
                    status=HealthStatus.HEALTHY,
                    message="Rate limiting disabled",
                    details={"enabled": False},
                )

            from src.middleware.rate_limiting import get_rate_limiter

            rate_limiter = get_rate_limiter()
            all_stats = rate_limiter.get_all_stats()

            return ComponentHealth(
                name="rate_limiter",
                status=HealthStatus.HEALTHY,
                message=f"Rate limiting active ({len(all_stats)} tenants tracked)",
                details={
                    "enabled": True,
                    "per_minute": settings.rate_limit_per_minute,
                    "per_hour": settings.rate_limit_per_hour,
                    "tracked_tenants": len(all_stats),
                },
            )

        except ModuleNotFoundError:
            return ComponentHealth(
                name="rate_limiter",
                status=HealthStatus.HEALTHY,
                message="Rate limiter not implemented in this build",
                details={"enabled": False},
            )
        except Exception as e:
            logger.error(f"Rate limiter health check error: {e}")
            return ComponentHealth(
                name="rate_limiter",
                status=HealthStatus.UNHEALTHY,
                message=f"Rate limiter check failed: {str(e)}",
            )

    @otel_trace
    async def check_all_components(self) -> List[ComponentHealth]:
        """
        Run all component health checks in parallel.

        Returns:
            List of ComponentHealth results
        """
        logger.debug("Running health checks for all components")

        # Run checks in parallel
        results = await asyncio.gather(
            self.check_llm_availability(),
            self.check_database_connectivity(),
            self.check_tools_readiness(),
            self.check_observability(),
            self.check_metrics(),
            self.check_rate_limiter(),
            return_exceptions=True,
        )

        # Filter out exceptions and convert to ComponentHealth
        component_results = []
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                component_name = [
                    "llm",
                    "database",
                    "tools",
                    "observability",
                    "metrics",
                    "rate_limiter",
                ][i]
                logger.error(f"{component_name} health check exception: {result}")
                component_results.append(
                    ComponentHealth(
                        name=component_name,
                        status=HealthStatus.UNHEALTHY,
                        message=f"Health check exception: {str(result)}",
                    )
                )
            else:
                component_results.append(result)

        return component_results

    @otel_trace
    async def get_health_status(self) -> Dict[str, Any]:
        """
        Get overall health status with component details.

        Returns:
            Dictionary with overall status and component details
        """
        components = await self.check_all_components()

        # Determine overall status — only critical components (LLM) can cause UNHEALTHY.
        # Optional component failures (DB, observability, metrics, rate_limiter) produce DEGRADED.
        critical_components = {"llm"}

        critical_unhealthy = any(
            c.status == HealthStatus.UNHEALTHY and c.name in critical_components
            for c in components
        )
        any_unhealthy_or_degraded = any(
            c.status != HealthStatus.HEALTHY for c in components
        )

        if critical_unhealthy:
            overall_status = HealthStatus.UNHEALTHY
        elif any_unhealthy_or_degraded:
            overall_status = HealthStatus.DEGRADED
        else:
            overall_status = HealthStatus.HEALTHY

        uptime_seconds = (datetime.utcnow() - self.startup_time).total_seconds()

        return {
            "status": overall_status.value,
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "uptime_seconds": uptime_seconds,
            "components": [c.to_dict() for c in components],
        }

    @otel_trace
    async def is_ready(self) -> bool:
        """
        Check if application is ready (Kubernetes readiness probe).

        Returns:
            True if ready, False otherwise
        """
        components = await self.check_all_components()

        # Ready if critical components (LLM) are at least degraded
        critical_components = ["llm"]

        for component in components:
            if component.name in critical_components:
                if component.status == HealthStatus.UNHEALTHY:
                    logger.warning(f"Readiness check failed: component={component.name} status={component.status.value}")
                    return False

        return True

    @otel_trace
    async def is_alive(self) -> bool:
        """
        Check if application is alive (Kubernetes liveness probe).

        Runs every registered liveness check (see register_liveness_check)
        in addition to the base "we can execute code" guarantee. Any single
        failing check fails the whole probe — a wedged background loop is
        just as much a liveness failure as an unresponsive HTTP server.

        Returns:
            True if alive
        """
        for check in self._liveness_checks:
            try:
                ok, reason = await check()
            except Exception as exc:
                logger.error(f"Liveness check raised an exception — treating as not alive: {exc}")
                return False
            if not ok:
                logger.error(f"Liveness check failed: {reason}")
                return False
        return True


# Global health checker instance
_health_checker: Optional[HealthChecker] = None


@otel_trace
def get_health_checker() -> HealthChecker:
    """
    Get global health checker instance.

    Returns:
        HealthChecker instance
    """
    global _health_checker
    if _health_checker is None:
        _health_checker = HealthChecker()
    return _health_checker
