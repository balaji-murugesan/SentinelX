"""The window of recent broker rejections (add-risk-guards task 6.2; design §4): 300 s, pruned on
read, and a rejection exactly 300 s old still counts."""

from datetime import datetime, timedelta

from contracts.orders import Order, OrderSide, OrderState
from engine.risk.rejections import REJECTION_WINDOW, RejectionWindow
from tests.engine.risk_builders import NOW, order


def rejected_at(at: datetime) -> Order:
    return order(OrderSide.BUY, 65, updated_at=at, state=OrderState.REJECTED)


def test_the_window_is_five_minutes() -> None:
    assert REJECTION_WINDOW == timedelta(seconds=300)


def test_a_recorded_rejection_is_dated_by_the_orders_rejection_time() -> None:
    window = RejectionWindow()

    window.record(rejected_at(NOW - timedelta(seconds=30)))

    assert window.recent(NOW) == (NOW - timedelta(seconds=30),)


def test_exactly_300_seconds_old_counts_and_a_moment_older_does_not() -> None:
    window = RejectionWindow()
    window.record(rejected_at(NOW))

    assert window.recent(NOW + timedelta(seconds=300)) == (NOW,)
    assert window.recent(NOW + timedelta(seconds=300, microseconds=1)) == ()


def test_old_rejections_are_pruned_on_read_and_stay_gone() -> None:
    window = RejectionWindow()
    window.record(rejected_at(NOW))
    window.record(rejected_at(NOW + timedelta(seconds=200)))

    assert window.recent(NOW + timedelta(seconds=400)) == (NOW + timedelta(seconds=200),)
    # Pruned, not merely hidden: asking about an earlier moment does not bring it back.
    assert window.recent(NOW + timedelta(seconds=250)) == (NOW + timedelta(seconds=200),)


def test_rejections_recorded_out_of_order_are_each_judged_and_returned_oldest_first() -> None:
    """A clock that stepped back between two rejections: a stale time behind a fresh one is still
    pruned, and the result is in time order."""
    window = RejectionWindow()
    window.record(rejected_at(NOW + timedelta(seconds=100)))
    window.record(rejected_at(NOW - timedelta(seconds=400)))  # recorded later, far older
    window.record(rejected_at(NOW))

    assert window.recent(NOW + timedelta(seconds=120)) == (NOW, NOW + timedelta(seconds=100))


def test_an_empty_window_has_nothing() -> None:
    assert RejectionWindow().recent(NOW) == ()
