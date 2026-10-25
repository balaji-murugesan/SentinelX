"""FastAPI application factory.

FastAPI is Python's ASP.NET Core minimal-API equivalent: functions decorated with
``@app.post(...)`` become endpoints, and pydantic models describe request/response bodies.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from pydantic import BaseModel
from redis.asyncio import Redis

from api.config import ApiSettings
from api.diagnostics import EngineNotResponding, ping_engine
from observability.tracing import get_provider


class ProbeResult(BaseModel):
    ok: bool
    rtt_ms: float


def create_app(settings: ApiSettings, redis_client: Redis | None = None) -> FastAPI:
    """Build the app. Tests pass ``redis_client``; production creates one from settings."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # Code before ``yield`` runs at startup, after it at shutdown (like IHostedService).
        if redis_client is not None:  # injected (tests): the caller owns and closes it
            yield
            return
        app.state.redis = Redis.from_url(
            settings.redis_url.get_secret_value(), decode_responses=True
        )
        try:
            yield
        finally:
            await app.state.redis.aclose()

    app = FastAPI(title="SentinelX api", lifespan=lifespan)
    if redis_client is not None:
        app.state.redis = redis_client  # set eagerly: test transports skip the lifespan hook

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        """Liveness for the container health check. Deliberately does not touch Redis: the api
        process being up is what is checked, and Redis has its own health check."""
        return {"status": "ok"}

    @app.post("/diagnostics/ping", response_model=ProbeResult)
    async def probe() -> ProbeResult:
        """Round trip api -> Redis -> engine -> Redis -> api. Diagnostic only."""
        try:
            rtt_ms = await ping_engine(app.state.redis)
        except EngineNotResponding as exc:
            raise HTTPException(status_code=504, detail=str(exc)) from None
        return ProbeResult(ok=True, rtt_ms=rtt_ms)

    # healthz is polled every few seconds; tracing it would only add noise.
    FastAPIInstrumentor.instrument_app(app, tracer_provider=get_provider(), excluded_urls="healthz")
    return app
