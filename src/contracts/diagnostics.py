"""Diagnostic ping/pong messages exchanged between api and engine over Redis.

Diagnostic only: they carry no orders, change no state and never reach the broker. They prove
the api -> Redis -> engine -> Redis -> api path works and let a single trace span both
services. The round-trip time measured from them is meant to become the engine heartbeat
behind the dashboard's connection status.

pydantic models are like C# records with validation: immutable here (``frozen``), and
``extra="forbid"`` rejects unknown fields so a typo cannot slip through silently.
Every cross-process message carries ``schema_version`` (CLAUDE.md) so readers can evolve.
"""

from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict

PING_STREAM = "diagnostics:ping"
PONG_TTL_SECONDS = 30


def pong_key(ping_id: str) -> str:
    """Each ping gets its own reply stream, so concurrent probes never read each other's pong."""
    return f"diagnostics:pong:{ping_id}"


class DiagnosticPing(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1] = 1
    ping_id: str
    sent_at: AwareDatetime
    # W3C trace context of the sender, so the engine's span joins the api's trace.
    traceparent: str | None = None


class DiagnosticPong(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1] = 1
    ping_id: str
    received_at: AwareDatetime
