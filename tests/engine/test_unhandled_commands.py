"""Commands that parse but have no handler yet get an answer, never a crash (task 1.1).

KillSwitch, SetRiskLimits and SizeOrder are part of the command contract from add-risk-engine
task 1.1, but nothing handles them until groups 4, 6 and 10. An engine that receives one must
reply "not supported" and keep reading, because an exception here would stop the consumer.
"""

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import cast

import pytest
from redis.asyncio import Redis

from contracts.commands import AnyCommand, KillSwitch, SetBanList, SetRiskLimits, SizeOrder
from contracts.market import InstrumentKey
from contracts.orders import OrderSide
from engine.orders.commands import CommandConsumer
from tests.engine.test_order_service import build

NOW = datetime(2026, 11, 16, 4, 0, tzinfo=UTC)
BASE = {
    "command_id": "c1",
    "idempotency_key": "k1",
    "account_id": "main",
    "source": "cli",
    "sent_at": NOW,
}


@pytest.mark.parametrize(
    "command",
    [
        KillSwitch(**BASE),  # type: ignore[arg-type]
        SetRiskLimits(**BASE, limits={"per_trade_max_risk": "1000.00"}),  # type: ignore[arg-type]
        SizeOrder(
            **BASE,  # type: ignore[arg-type]
            key=InstrumentKey.parse("NSE_FNO:53720"),
            side=OrderSide.BUY,
            entry_price=Decimal("200"),
            initial_stop=Decimal("190"),
        ),  # fmt: skip
        SetBanList(**BASE, trade_date=date(2026, 11, 16), symbols=["XYZ"]),  # type: ignore[arg-type]
    ],
    ids=["kill_switch", "set_risk_limits", "size_order", "set_ban_list"],
)
async def test_a_command_with_no_handler_is_answered_not_accepted(command: AnyCommand) -> None:
    service, adapter, repo = build()
    consumer = CommandConsumer(cast(Redis, None), service, account_id="main")

    result = await consumer._process(command)

    assert not result.accepted
    assert command.command in (result.reason or "") and "not supported" in (result.reason or "")
    assert result.command_id == "c1" and result.idempotency_key == "k1"
    assert adapter.placed == [] and repo.orders == {}
