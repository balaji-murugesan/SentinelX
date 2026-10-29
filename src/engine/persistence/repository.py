"""Where trading state is stored (add-order-position-core design §3).

``TradingRepository`` is a ``Protocol``: ``PostgresRepository`` is the real one, and tests use
an in-memory implementation with the same behaviour. Every method that writes several things
does so in ONE transaction (all or nothing), and every failure surfaces as
``PersistenceError``, so the order service has one thing to catch to fail closed.
"""

from dataclasses import dataclass
from typing import Protocol

from contracts.commands import CommandResult
from contracts.orders import Fill, Order, Position


class PersistenceError(Exception):
    """A read or write did not complete (database down, timeout, constraint violation)."""


@dataclass(frozen=True)
class LoadedState:
    """What restart recovery reads back (design §9)."""

    open_orders: list[Order]  # every order not in a final state, including Unknown
    fills: list[Fill]  # all fills, in the order they were applied
    positions: list[Position]


class TradingRepository(Protocol):
    async def find_result(self, account_id: str, idempotency_key: str) -> CommandResult | None:
        """The stored result for this idempotency key, if the command was processed before."""
        ...

    async def save_result(self, result: CommandResult) -> None:
        """Store the result of a command that created no order (a rejection)."""
        ...

    async def create_order(self, order: Order, result: CommandResult) -> None:
        """Store a new order, its first audit record and the command result: one transaction."""
        ...

    async def update_order(self, order: Order) -> None:
        """Store the order's new state and an audit record of the transition."""
        ...

    async def record_fill(self, order: Order, fill: Fill, position: Position) -> None:
        """Store a fill with its order's new state, an audit record and the updated position:
        one transaction, so a fill is never stored without its position change."""
        ...

    async def load(self, account_id: str) -> LoadedState: ...
