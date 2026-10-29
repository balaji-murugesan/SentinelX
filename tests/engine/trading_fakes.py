"""In-memory ``TradingRepository`` for tests, with the same behaviour as PostgresRepository.

The shared contract suite (test_trading_repository.py) runs against both, so this fake cannot
quietly drift from the real database's behaviour.
"""

from contracts.commands import CommandResult
from contracts.orders import Fill, Order, Position
from engine.persistence.repository import LoadedState, PersistenceError


class MemoryRepository:
    def __init__(self) -> None:
        self.orders: dict[str, Order] = {}
        self.events: list[tuple[str, str, str | None]] = []  # (order_id, state, reason)
        self.fills: list[Fill] = []
        self.positions: dict[tuple[str, str, str], Position] = {}
        self.results: dict[tuple[str, str], CommandResult] = {}

    async def find_result(self, account_id: str, idempotency_key: str) -> CommandResult | None:
        return self.results.get((account_id, idempotency_key))

    async def save_result(self, result: CommandResult) -> None:
        self._check_new_result(result)
        self.results[(result.account_id, result.idempotency_key)] = result

    async def create_order(self, order: Order, result: CommandResult) -> None:
        self._check_new_result(result)
        if order.order_id in self.orders:
            raise PersistenceError(f"order {order.order_id} already stored")
        self.orders[order.order_id] = order
        self.events.append((order.order_id, order.state.value, order.reason))
        self.results[(result.account_id, result.idempotency_key)] = result

    async def update_order(self, order: Order) -> None:
        if order.order_id not in self.orders:
            raise PersistenceError(f"order {order.order_id} is not stored")
        self.orders[order.order_id] = order
        self.events.append((order.order_id, order.state.value, order.reason))

    async def record_fill(self, order: Order, fill: Fill, position: Position) -> None:
        if any(f.fill_id == fill.fill_id for f in self.fills):
            raise PersistenceError(f"fill {fill.fill_id} already stored")
        await self.update_order(order)
        self.fills.append(fill)
        self.positions[(position.account_id, str(position.key), position.product.value)] = position

    async def load(self, account_id: str) -> LoadedState:
        return LoadedState(
            open_orders=sorted(
                (
                    o
                    for o in self.orders.values()
                    if o.account_id == account_id and not o.state.is_final
                ),
                key=lambda o: o.created_at,
            ),
            fills=[f for f in self.fills if f.account_id == account_id],
            positions=[p for p in self.positions.values() if p.account_id == account_id],
        )

    def _check_new_result(self, result: CommandResult) -> None:
        if (result.account_id, result.idempotency_key) in self.results:
            raise PersistenceError("duplicate idempotency key")
