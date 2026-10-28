"""The order state machine of PRD §5 (orders spec "Order state machine").

A pure transition table: ``transition`` returns the order in its new state or raises
``InvalidTransition``, and never touches storage or the broker. The order service decides
what to do with a refused transition (log it, keep the order unchanged).

    Requested -> Submitted | Rejected
    Submitted -> Open | Rejected | Unknown        (Unknown: no broker ack in time)
    Unknown   -> Open | Rejected                  (after reconciling with the broker)
    Open      -> PartiallyFilled | Filled | Cancelled
    PartiallyFilled -> PartiallyFilled | Filled | Cancelled   (another part fills)
    Filled, Rejected, Cancelled: final
"""

from datetime import datetime
from typing import Any

from contracts.orders import Order, OrderState

S = OrderState
TRANSITIONS: dict[OrderState, frozenset[OrderState]] = {
    S.REQUESTED: frozenset({S.SUBMITTED, S.REJECTED}),
    S.SUBMITTED: frozenset({S.OPEN, S.REJECTED, S.UNKNOWN}),
    S.UNKNOWN: frozenset({S.OPEN, S.REJECTED}),
    S.OPEN: frozenset({S.PARTIALLY_FILLED, S.FILLED, S.CANCELLED}),
    S.PARTIALLY_FILLED: frozenset({S.PARTIALLY_FILLED, S.FILLED, S.CANCELLED}),
    S.FILLED: frozenset(),
    S.REJECTED: frozenset(),
    S.CANCELLED: frozenset(),
}


class InvalidTransition(Exception):
    def __init__(self, order: Order, target: OrderState) -> None:
        super().__init__(
            f"order {order.order_id}: {order.state.value} -> {target.value} is not allowed"
        )
        self.order = order
        self.target = target


def can_transition(source: OrderState, target: OrderState) -> bool:
    return target in TRANSITIONS[source]


def transition(
    order: Order, target: OrderState, *, at: datetime, reason: str | None = None, **changes: Any
) -> Order:
    """``order`` moved to ``target`` at ``at``, with any other field ``changes`` (for example
    ``broker_order_id`` or ``filled_quantity``). Raises InvalidTransition if not allowed."""
    if not can_transition(order.state, target):
        raise InvalidTransition(order, target)
    # model_copy(update=...) is a copy with changes, like a C# record `with` expression, but it
    # skips validation; the model_validate round trip re-runs every validator on the result.
    updated = order.model_copy(
        update={**changes, "state": target, "reason": reason, "updated_at": at}
    )
    return Order.model_validate(updated.model_dump())
