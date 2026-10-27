"""DhanFeed: Dhan's live market feed over WebSocket, as a MarketDataProvider (design §2, §6).

Connection: ``wss://api-feed.dhan.co?version=2&token=…&clientId=…&authType=2``. Requests are
JSON, responses are binary packets (engine.broker.dhan.packets). We subscribe in Quote mode
(RequestCode 17: LTP, last trade size/time, volume), at most 100 instruments per message.

Behaviour (spec market-feed):
  * reconnects after any unexpected drop with growing, jittered back-off, then resubscribes
    every instrument that was subscribed before;
  * stops for good (status DOWN) on credential/plan errors: Dhan disconnect codes 806-810 or an
    HTTP 401/403 on the upgrade. Retrying a dead token only adds noise and load;
  * during market hours, a connection silent for FEED_SILENCE_S is treated as dead and replaced.

The connection URL carries the access token, so it is built in exactly one place, never stored,
and never logged; the ``websockets`` library's own logger is held at WARNING, and every error
text is scrubbed before logging.
"""

import asyncio
import json
import logging
import random
from collections.abc import Callable, Iterable, Sequence
from datetime import datetime
from decimal import Decimal
from typing import Any
from urllib.parse import urlencode

import structlog
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import InvalidStatus, WebSocketException

from contracts.market import InstrumentKey
from engine.broker.dhan.packets import (
    Disconnect,
    TradeUpdate,
    Undecodable,
    decode_message,
    round_to_tick,
)
from engine.market_data.bus import TickBus
from engine.market_data.clock import is_market_hours, utc_now
from engine.market_data.provider import FeedState, FeedStatus, Subscriptions, Tick
from observability.masking import scrub_text

log = structlog.get_logger("engine.dhan.feed")
# websockets logs connection details (including the URL) at DEBUG; keep it quiet.
logging.getLogger("websockets").setLevel(logging.WARNING)

SUBSCRIBE_QUOTE, UNSUBSCRIBE_QUOTE = 17, 18
MAX_PER_MESSAGE = 100
# Data API error codes (annexure): plan not subscribed, token expired, auth failed, token
# invalid, client ID invalid. None of these fix themselves, so the feed stops.
TERMINAL_CODES = {806: "Data APIs not subscribed", 807: "access token expired",
                  808: "authentication failed", 809: "access token invalid",
                  810: "client ID invalid"}  # fmt: skip
TOO_MANY_CONNECTIONS = 805


class _Terminal(Exception):
    pass


class _Silent(Exception):
    pass


class Backoff:
    """Reconnect delays: initial, doubling, capped, +-20 % jitter; reset after a stable link."""

    def __init__(self, initial_s: float, max_s: float, rng: random.Random | None = None) -> None:
        self._initial = initial_s
        self._max = max_s
        self._next = initial_s
        self._rng = rng or random.Random()

    def reset(self) -> None:
        self._next = self._initial

    def jump_to_max(self) -> None:
        self._next = self._max

    def next_delay(self) -> float:
        base = self._next
        self._next = min(self._next * 2, self._max)
        return base * self._rng.uniform(0.8, 1.2)


class DhanFeed:
    def __init__(
        self,
        bus: TickBus,
        subscriptions: Subscriptions,
        *,
        url: str,
        client_id: str,
        token: str,
        tick_size: Callable[[InstrumentKey], Decimal | None] = lambda _key: None,
        reconnect_max_s: float = 30.0,
        reconnect_initial_s: float = 1.0,
        stable_after_s: float = 60.0,
        silence_s: float = 30.0,
        market_hours: Callable[[datetime], bool] = is_market_hours,
        now: Callable[[], datetime] = utc_now,
    ) -> None:
        self._bus = bus
        self._subscriptions = subscriptions
        self._url = url
        self._client_id = client_id
        self._token = token
        self._tick_size = tick_size
        self._backoff = Backoff(reconnect_initial_s, reconnect_max_s)
        self._stable_after = stable_after_s
        self._silence = silence_s
        self._market_hours = market_hours
        self._now = now
        self._ws: ClientConnection | None = None
        self.undecodable = 0  # unknown/truncated packets seen (spec "Unknown packet type")

    # ---- MarketDataProvider ---------------------------------------------------------

    async def subscribe(self, keys: Iterable[InstrumentKey], owner: str) -> None:
        new = self._subscriptions.add(keys, owner)  # raises over the cap, nothing sent
        if new and self._ws is not None:
            await self._send(self._ws, SUBSCRIBE_QUOTE, new)

    async def unsubscribe(self, keys: Iterable[InstrumentKey], owner: str) -> None:
        gone = self._subscriptions.remove(keys, owner)
        if gone and self._ws is not None:
            await self._send(self._ws, UNSUBSCRIBE_QUOTE, gone)

    async def run(self, stop: asyncio.Event) -> None:
        state = FeedState.CONNECTING
        reason = "starting"
        while not stop.is_set():
            self._status(state, reason)
            try:
                await self._session(stop)
                return  # stop was set
            except _Terminal as exc:
                self._status(FeedState.DOWN, str(exc))
                log.error("dhan feed stopped; restart the engine with a new token", reason=str(exc))
                return
            except (_Silent, WebSocketException, OSError, TimeoutError) as exc:
                if stop.is_set():
                    return  # our own close on shutdown, not a disconnect
                state, reason = FeedState.RECONNECTING, self._describe(exc)
            delay = self._backoff.next_delay()
            log.warning("dhan feed disconnected; reconnecting", reason=reason, retry_in=delay)
            if await _stopped_within(stop, delay):
                return

    # ---- one connection -------------------------------------------------------------

    async def _session(self, stop: asyncio.Event) -> None:
        try:
            ws = await connect(self._connection_url(), open_timeout=10, ping_interval=None)
        except InvalidStatus as exc:
            if exc.response.status_code in (401, 403):
                status = exc.response.status_code
                raise _Terminal(f"Dhan rejected the feed login (HTTP {status})") from None
            raise
        # Dhan pings every 10 s and the library answers automatically; we send no pings of our
        # own (ping_interval=None) and rely on the silence watchdog instead.
        closer = asyncio.create_task(_close_when(stop, ws))
        try:
            self._ws = ws
            await self._send(ws, SUBSCRIBE_QUOTE, self._subscriptions.keys)
            self._status(FeedState.CONNECTED, "connected")
            connected_at = self._now()
            await self._read(ws, stop, connected_at)
        finally:
            self._ws = None
            closer.cancel()
            await ws.close()

    async def _read(
        self, ws: ClientConnection, stop: asyncio.Event, connected_at: datetime
    ) -> None:
        while not stop.is_set():
            try:
                message = await asyncio.wait_for(ws.recv(), timeout=self._silence)
            except TimeoutError:
                if self._market_hours(self._now()):
                    raise _Silent("silent feed") from None
                continue
            if stop.is_set():
                return
            if (self._now() - connected_at).total_seconds() >= self._stable_after:
                self._backoff.reset()
            if isinstance(message, bytes):
                self._handle(message)

    def _handle(self, message: bytes) -> None:
        received_at = self._now()
        for packet in decode_message(message):
            if isinstance(packet, TradeUpdate):
                tick = self._tick_size(packet.key)
                ltp = round_to_tick(packet.ltp, tick) if tick else packet.ltp
                self._bus.publish(
                    Tick(
                        packet.key,
                        ltp,
                        packet.exchange_time,
                        received_at,
                        ltq=packet.ltq,
                        volume=packet.volume,
                    )  # fmt: skip
                )
            elif isinstance(packet, Disconnect):
                if packet.code in TERMINAL_CODES:
                    raise _Terminal(f"Dhan code {packet.code}: {TERMINAL_CODES[packet.code]}")
                if packet.code == TOO_MANY_CONNECTIONS:
                    self._backoff.jump_to_max()
                raise _Disconnected(f"Dhan disconnect code {packet.code}")
            elif isinstance(packet, Undecodable):
                self.undecodable += 1
                if self.undecodable == 1:
                    log.warning("undecodable dhan packet skipped", code=packet.code,
                                reason=packet.reason)  # fmt: skip

    # ---- helpers --------------------------------------------------------------------

    def _connection_url(self) -> str:
        """The only place the token-bearing URL exists. Never log or store the result."""
        query = urlencode(
            {"version": 2, "token": self._token, "clientId": self._client_id, "authType": 2}
        )
        return f"{self._url}?{query}"

    async def _send(self, ws: ClientConnection, code: int, keys: Sequence[InstrumentKey]) -> None:
        for start in range(0, len(keys), MAX_PER_MESSAGE):
            batch = keys[start : start + MAX_PER_MESSAGE]
            request: dict[str, Any] = {
                "RequestCode": code,
                "InstrumentCount": len(batch),
                "InstrumentList": [
                    {"ExchangeSegment": k.segment.value, "SecurityId": k.security_id} for k in batch
                ],
            }
            await ws.send(json.dumps(request))

    def _status(self, state: FeedState, reason: str) -> None:
        self._bus.publish(FeedStatus(state, reason, self._now()))

    def _describe(self, exc: BaseException) -> str:
        """Exception class + scrubbed text, with the token removed even if it slipped in."""
        text = scrub_text(str(exc)).replace(self._token, "***")
        return f"{type(exc).__name__}: {text}" if text else type(exc).__name__


class _Disconnected(WebSocketException):
    """Dhan sent a non-terminal disconnect packet: treat like a dropped connection."""


async def _close_when(stop: asyncio.Event, ws: ClientConnection) -> None:
    await stop.wait()
    await ws.close()


async def _stopped_within(stop: asyncio.Event, seconds: float) -> bool:
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
    except TimeoutError:
        return False
    return True
