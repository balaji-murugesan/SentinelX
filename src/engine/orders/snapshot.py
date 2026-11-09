"""Writes the engine's state to Redis for clients to read (order-commands spec; design §4).

Orders and positions live in ``OrderService``; this module copies a view of them into one Redis
string at most every ``SNAPSHOT_INTERVAL_S``. MTM (unrealized P&L) is computed here, from the
newest tick per instrument, because clients must never compute P&L themselves. The snapshot is
never read back by the engine: Redis holds a copy, not the truth (the database does).

If Redis is down the write is skipped and tried again on the next interval, so the engine keeps
trading (and a crash here must not stop it: ``engine.main`` supervises this task separately).
"""

import asyncio
import contextlib
from collections.abc import Callable
from datetime import datetime
from decimal import Decimal

import structlog
from redis.asyncio import Redis
from redis.exceptions import RedisError

from contracts.market import InstrumentKey
from contracts.snapshot import PositionView, Snapshot, snapshot_key
from engine.market_data.bus import Subscription
from engine.market_data.clock import utc_now
from engine.market_data.provider import FeedStatus, Tick
from engine.orders.service import OrderService
from engine.positions.book import mtm, net_pnl

log = structlog.get_logger("engine.orders.snapshot")


class SnapshotWriter:
    def __init__(
        self,
        redis: Redis,
        service: OrderService,
        ticks: Subscription[Tick | FeedStatus],
        *,
        account_id: str,
        interval_s: float,
        now: Callable[[], datetime] = utc_now,
    ) -> None:
        self._redis = redis
        self._service = service
        self._ticks = ticks
        self._account_id = account_id
        self._interval_s = interval_s
        self._now = now
        self._prices: dict[InstrumentKey, Decimal] = {}

    def build(self) -> Snapshot:
        """The current state as a contract model. Pure apart from draining waiting ticks."""
        while (item := self._ticks.get_nowait()) is not None:
            if isinstance(item, Tick):
                self._prices[item.key] = item.ltp
        views = []
        for position in self._service.positions.values():
            price = self._prices.get(position.key)
            if position.quantity == 0:  # flat: nothing to mark, only realized P&L remains
                views.append(
                    PositionView(
                        position=position, mtm=Decimal(0), net_pnl=net_pnl(position, Decimal(0))
                    )
                )
            elif price is None:  # no price yet: say so, never guess
                views.append(PositionView(position=position))
            else:
                views.append(
                    PositionView(
                        position=position,
                        last_price=price,
                        mtm=mtm(position, price),
                        net_pnl=net_pnl(position, price),
                    )
                )
        return Snapshot(
            account_id=self._account_id,
            taken_at=self._now(),
            open_orders=[o for o in self._service.orders.values() if not o.state.is_final],
            positions=views,
            halted=bool(self._service.halt_differences),
            halt_differences=list(self._service.halt_differences),
        )

    async def write_once(self) -> None:
        await self._redis.set(snapshot_key(self._account_id), self.build().model_dump_json())

    async def run(self, stop: asyncio.Event) -> None:
        """Write a snapshot every interval until ``stop`` is set; Redis errors are retried."""
        while not stop.is_set():
            try:
                await self.write_once()
            except (RedisError, OSError) as exc:
                log.warning("snapshot not written; retrying", error=type(exc).__name__)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=self._interval_s)
