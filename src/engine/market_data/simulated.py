"""SimulatedFeed: ticks without Dhan, for tests and after-hours development.

Two modes, both published through the same TickBus and subscription rules as the Dhan feed,
so every consumer runs its real code path (spec "Same consumer code path"):

  * scripted    - replays a fixed list of price steps (tests: "price goes 120 -> 108 -> ...").
  * random walk - every interval, each subscribed instrument moves a random whole number of
                  ticks; seeded, so a run can be repeated exactly.

Never allowed in live mode: EngineSettings refuses FEED_SOURCE=simulated with TRADING_MODE=live.
"""

import asyncio
import random
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal

from contracts.market import InstrumentKey
from engine.market_data.bus import TickBus
from engine.market_data.clock import utc_now
from engine.market_data.provider import FeedState, FeedStatus, Subscriptions, Tick


@dataclass(frozen=True)
class ScriptStep:
    key: InstrumentKey
    price: Decimal
    after_s: float = 0.0  # pause before this step
    volume: int | None = None


@dataclass(frozen=True)
class WalkConfig:
    """SIM_* settings: start price, max ticks per step, seconds per step, fallback tick, seed."""

    start_price: Decimal
    step_ticks: int
    interval_s: float
    default_tick: Decimal
    seed: int | None = None


class RandomWalk:
    """Pure price generator: prices stay on each instrument's tick grid and >= one tick."""

    def __init__(self, config: WalkConfig) -> None:
        self._config = config
        self._rng = random.Random(config.seed)  # own generator: reproducible, not global
        self._prices: dict[InstrumentKey, Decimal] = {}

    def next_price(self, key: InstrumentKey, tick: Decimal) -> Decimal:
        price = self._prices.get(key)
        if price is None:
            price = snap_to_tick(self._config.start_price, tick)
        else:
            moved = self._rng.randint(-self._config.step_ticks, self._config.step_ticks)
            price = max(tick, price + moved * tick)
        self._prices[key] = price
        return price


def snap_to_tick(price: Decimal, tick: Decimal) -> Decimal:
    """Nearest multiple of ``tick`` (half rounds up), never below one tick."""
    steps = (price / tick).to_integral_value(rounding=ROUND_HALF_UP)
    return max(tick, steps * tick)


class SimulatedFeed:
    def __init__(
        self,
        bus: TickBus,
        subscriptions: Subscriptions,
        *,
        script: Sequence[ScriptStep] | None = None,
        walk: WalkConfig | None = None,
        tick_size: Callable[[InstrumentKey], Decimal | None] = lambda _key: None,
        now: Callable[[], datetime] = utc_now,
    ) -> None:
        if (script is None) == (walk is None):
            raise ValueError("SimulatedFeed needs exactly one of script or walk")
        self._bus = bus
        self._subscriptions = subscriptions
        self._script = script
        self._walk = RandomWalk(walk) if walk else None
        self._interval = walk.interval_s if walk else 0.0
        self._default_tick = walk.default_tick if walk else Decimal("0.05")
        self._tick_size = tick_size
        self._now = now
        self._volume: dict[InstrumentKey, int] = {}

    async def subscribe(self, keys: Iterable[InstrumentKey], owner: str) -> None:
        self._subscriptions.add(keys, owner)  # nothing to send: there is no server

    async def unsubscribe(self, keys: Iterable[InstrumentKey], owner: str) -> None:
        self._subscriptions.remove(keys, owner)

    async def run(self, stop: asyncio.Event) -> None:
        self._bus.publish(FeedStatus(FeedState.CONNECTED, "simulated feed", self._now()))
        if self._script is not None:
            await self._replay(self._script, stop)
        else:
            await self._random_walk(stop)

    async def _replay(self, script: Sequence[ScriptStep], stop: asyncio.Event) -> None:
        """Publish each step for subscribed instruments, then return."""
        for step in script:
            if step.after_s and await _stopped_within(stop, step.after_s):
                return
            if step.key in self._subscriptions:
                self._publish(step.key, step.price, step.volume)

    async def _random_walk(self, stop: asyncio.Event) -> None:
        assert self._walk is not None
        while not stop.is_set():
            # Sorted so the same seed gives the same sequence regardless of subscribe order.
            for key in sorted(self._subscriptions.keys, key=str):
                tick = self._tick_size(key) or self._default_tick
                self._publish(key, self._walk.next_price(key, tick), None)
            if await _stopped_within(stop, self._interval):
                return

    def _publish(self, key: InstrumentKey, price: Decimal, volume: int | None) -> None:
        if volume is None:
            volume = self._volume.get(key, 0) + 1
        self._volume[key] = volume
        now = self._now()
        self._bus.publish(Tick(key, price, now, now, ltq=1, volume=volume))


async def _stopped_within(stop: asyncio.Event, seconds: float) -> bool:
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
    except TimeoutError:
        return False
    return True
