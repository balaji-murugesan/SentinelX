"""The ``SetRiskLimits`` command and the limits held in memory (add-risk-engine task 4.3).

Scenarios (risk-limits spec): "Fresh install", "Limits survive restart", "Repeated edit",
"Attempt to set the floor" and "Rejected edit is recorded". Also: edits are serialised (one lock
around load, validate and save), a broken stored document fails closed, and a store that is down
refuses the edit instead of guessing.
"""

import asyncio
from datetime import datetime
from decimal import Decimal
from typing import cast

from redis.asyncio import Redis

from contracts.commands import LimitValue, SetRiskLimits
from engine.orders.commands import CommandConsumer
from engine.persistence.repository import PersistenceError
from engine.risk.limits import TIGHTEN_ONLY, parse_limits
from engine.risk.limits_service import LimitsService, LimitsStatus
from tests.engine.risk_fakes import MemoryRiskRepository
from tests.engine.test_order_service import build
from tests.engine.test_risk_limits import AFTER_CLOSE, BASE, MID_DAY, edit
from tests.engine.trading_fakes import MemoryRepository

D = Decimal


class Clock:
    def __init__(self, at: datetime) -> None:
        self.at = at

    def __call__(self) -> datetime:
        return self.at


def command(
    limits: dict[str, LimitValue], *, key: str = "k1", command_id: str = "c1"
) -> SetRiskLimits:
    return SetRiskLimits(
        command_id=command_id, idempotency_key=key, account_id="main", source="cli",
        sent_at=MID_DAY, limits=limits,
    )  # fmt: skip


def service(
    risk: MemoryRiskRepository | None = None,
    results: MemoryRepository | None = None,
    clock: Clock | None = None,
    figures: tuple[Decimal, Decimal | None] = (D(0), None),
) -> tuple[LimitsService, MemoryRiskRepository, MemoryRepository, Clock]:
    risk = risk or MemoryRiskRepository()
    results = results or MemoryRepository()
    clock = clock or Clock(AFTER_CLOSE)
    built = LimitsService(
        account_id="main", repository=risk, results=results, figures=lambda: figures, now=clock
    )
    return built, risk, results, clock


# ---- loading --------------------------------------------------------------------------------


async def test_before_the_first_load_the_limits_are_not_loaded_and_entries_would_be_refused() -> (
    None
):
    limits, *_ = service()

    assert limits.status is LimitsStatus.NOT_LOADED and limits.current is None
    assert (limits.reason_for() or "").startswith("RISK_STATE_UNAVAILABLE")


async def test_fresh_install_has_no_limits_and_says_so() -> None:
    """Spec "Fresh install": no stored limits, so every entry gets RISK_LIMITS_NOT_SET."""
    limits, *_ = service()

    assert await limits.load() is True

    assert limits.status is LimitsStatus.NOT_SET and limits.current is None
    assert (limits.reason_for() or "").startswith("RISK_LIMITS_NOT_SET")


async def test_limits_survive_a_restart() -> None:
    """Spec "Limits survive restart": a new service on the same storage has the same limits."""
    first, risk, results, _ = service()
    assert (await first.set_limits(command(BASE))).accepted

    restarted, *_ = service(risk, results)
    await restarted.load()

    assert restarted.status is LimitsStatus.SET and restarted.reason_for() is None
    assert restarted.current == parse_limits(BASE)


async def test_a_storage_that_is_down_at_load_leaves_entries_refused() -> None:
    class Down(MemoryRiskRepository):
        async def load_limits(self, account_id: str) -> None:  # type: ignore[override]
            raise PersistenceError("db down")

    limits, *_ = service(Down())

    assert await limits.load() is False
    assert limits.status is LimitsStatus.NOT_LOADED


async def test_a_stored_document_that_no_longer_validates_is_unusable_not_empty() -> None:
    limits, risk, *_ = service()
    await risk.save_limits("main", {**BASE, "price_band_pct": "-5"}, AFTER_CLOSE)  # type: ignore[arg-type]

    await limits.load()

    assert limits.status is LimitsStatus.UNUSABLE and limits.current is None
    assert (limits.reason_for() or "").startswith("RISK_LIMITS_INVALID")


# ---- the command ----------------------------------------------------------------------------


async def test_the_first_set_is_accepted_stored_and_becomes_current() -> None:
    limits, risk, *_ = service()

    result = await limits.set_limits(command(BASE))

    assert result.accepted and result.reason is None
    assert result.command_id == "c1" and result.idempotency_key == "k1"
    assert limits.status is LimitsStatus.SET and limits.current == parse_limits(BASE)
    stored = await risk.load_limits("main")
    assert stored is not None and stored.version == 1
    assert parse_limits(stored.limits) == limits.current


async def test_repeated_edit_returns_the_first_result_and_changes_nothing() -> None:
    """Spec "Repeated edit": the same key twice, even with different content."""
    limits, risk, *_ = service()
    first = await limits.set_limits(command(BASE, key="same"))

    again = await limits.set_limits(command(edit(per_trade_max_risk="500"), key="same"))

    assert again == first
    stored = await risk.load_limits("main")
    assert stored is not None and stored.version == 1  # saved once
    assert limits.current == parse_limits(BASE)
    assert len(risk.events) == 1  # and audited once


async def test_a_refused_edit_is_remembered_so_a_retry_is_refused_the_same_way() -> None:
    limits, risk, _, clock = service()
    await limits.set_limits(command(BASE, key="first"))
    clock.at = MID_DAY
    refused = await limits.set_limits(command(edit(per_trade_max_risk="1500"), key="loosen"))
    assert not refused.accepted

    clock.at = AFTER_CLOSE  # the same key, now outside market hours: still the first answer
    assert (
        await limits.set_limits(command(edit(per_trade_max_risk="1500"), key="loosen")) == refused
    )


async def test_attempt_to_set_the_floor_is_rejected_as_an_unknown_limit() -> None:
    """Spec "Attempt to set the floor": engine-owned state is not a limit."""
    limits, risk, *_ = service()
    await limits.set_limits(command(BASE))

    for owned in ("floor", "peak", "halt_level", "entries_today"):
        result = await limits.set_limits(command(edit(**{owned: "1"}), key=f"set-{owned}"))

        assert not result.accepted
        assert (result.reason or "").startswith("RISK_LIMITS_INVALID")
        assert f"unknown limit '{owned}'" in (result.reason or "")
    stored = await risk.load_limits("main")
    assert stored is not None and stored.version == 1  # nothing was stored


async def test_an_out_of_range_edit_leaves_the_stored_limits_unchanged() -> None:
    limits, risk, *_ = service()
    await limits.set_limits(command(BASE))

    result = await limits.set_limits(command(edit(price_band_pct="-1"), key="bad"))

    assert not result.accepted
    stored = await risk.load_limits("main")
    assert stored is not None and stored.version == 1
    assert limits.current == parse_limits(BASE)


async def test_loosening_mid_day_is_refused_and_tightening_applies_to_the_next_order() -> None:
    limits, _, _, clock = service()
    await limits.set_limits(command(BASE))
    clock.at = MID_DAY

    loosen = await limits.set_limits(command(edit(per_trade_max_risk="1500"), key="up"))
    tighten = await limits.set_limits(command(edit(per_trade_max_risk="500"), key="down"))

    assert not loosen.accepted and (loosen.reason or "").startswith(TIGHTEN_ONLY)
    assert "per_trade_max_risk" in (loosen.reason or "")
    assert tighten.accepted
    assert limits.current is not None and limits.current.per_trade_max_risk == D(500)


async def test_the_floor_replay_uses_the_engine_figures() -> None:
    """Removing the lock after it has raised the floor: the figures come from the engine."""
    limits, _, _, clock = service(figures=(D(1500), D(200)))
    await limits.set_limits(command(BASE))
    clock.at = MID_DAY

    result = await limits.set_limits(
        command(edit(lock_mode="OFF", lock_trigger=None, lock_at=None), key="drop-lock")
    )

    assert not result.accepted and "lower today's floor" in (result.reason or "")


async def test_a_market_hours_edit_waits_for_the_engine_figures_instead_of_guessing_them() -> None:
    """Figures of None mean today's state is not loaded: the floor cannot be replayed, so the edit
    is refused as unavailable (never judged against zeros) and is not remembered, so the same key
    may be sent again. The first set and an edit outside market hours need no figures."""
    figures: list[tuple[Decimal, Decimal | None] | None] = [None]
    clock = Clock(AFTER_CLOSE)
    limits = LimitsService(
        account_id="main", repository=MemoryRiskRepository(), results=MemoryRepository(),
        figures=lambda: figures[0], now=clock,
    )  # fmt: skip
    assert (await limits.set_limits(command(BASE, key="first"))).accepted
    assert (await limits.set_limits(command(edit(per_trade_max_risk="1500"), key="loose"))).accepted
    clock.at = MID_DAY

    early = await limits.set_limits(command(edit(per_trade_max_risk="500"), key="tighter"))
    figures[0] = (D(0), None)
    later = await limits.set_limits(command(edit(per_trade_max_risk="500"), key="tighter"))

    assert not early.accepted and (early.reason or "").startswith("RISK_STATE_UNAVAILABLE")
    assert later.accepted
    assert limits.current is not None and limits.current.per_trade_max_risk == D(500)


# ---- audit ----------------------------------------------------------------------------------


async def test_an_accepted_edit_is_recorded_with_old_and_new_values() -> None:
    limits, risk, _, clock = service()
    await limits.set_limits(command(BASE, key="first"))
    clock.at = MID_DAY
    await limits.set_limits(command(edit(per_trade_max_risk="500"), key="tighter"))

    event = risk.events[-1]

    assert (event.rule, event.phase, event.decision) == ("limits", "config", "ALLOW")
    assert event.reason_code is None and event.enforced and event.account_id == "main"
    assert event.inputs["outcome"] == "accepted"
    assert event.inputs["old.per_trade_max_risk"] == "1000"
    assert event.inputs["new.per_trade_max_risk"] == "500"
    assert event.inputs["source"] == "cli" and event.inputs["idempotency_key"] == "tighter"


async def test_rejected_edit_is_recorded_with_the_attempted_values_and_the_reason_code() -> None:
    """Spec "Rejected edit is recorded"."""
    limits, risk, _, clock = service()
    await limits.set_limits(command(BASE))
    clock.at = MID_DAY
    await limits.set_limits(command(edit(per_trade_max_risk="1500"), key="loosen"))

    event = risk.events[-1]

    assert (event.decision, event.reason_code) == ("REJECT", TIGHTEN_ONLY)
    assert event.inputs["outcome"] == "rejected"
    assert event.inputs["new.per_trade_max_risk"] == "1500"  # what was attempted
    assert event.inputs["old.per_trade_max_risk"] == "1000"
    assert "per_trade_max_risk" in str(event.inputs["problems"])


async def test_a_failed_audit_write_does_not_undo_an_accepted_edit() -> None:
    class AuditDown(MemoryRiskRepository):
        async def append_event(self, event: object) -> None:  # type: ignore[override]
            raise PersistenceError("audit down")

    limits, *_ = service(AuditDown())

    result = await limits.set_limits(command(BASE))

    assert result.accepted and limits.status is LimitsStatus.SET


# ---- storage failures and the lock ----------------------------------------------------------


async def test_a_store_that_cannot_save_refuses_the_edit_and_keeps_the_old_limits() -> None:
    class SaveDown(MemoryRiskRepository):
        fail = False

        async def save_limits(
            self, account_id: str, limits: dict[str, LimitValue], at: datetime
        ) -> int:
            if self.fail:
                raise PersistenceError("db down")
            return await super().save_limits(account_id, limits, at)

    risk = SaveDown()
    limits, *_ = service(risk)
    await limits.set_limits(command(BASE))
    risk.fail = True

    result = await limits.set_limits(command(edit(per_trade_max_risk="500"), key="k2"))

    assert not result.accepted and (result.reason or "").startswith("RISK_STATE_UNAVAILABLE")
    assert limits.current == parse_limits(BASE)


async def test_a_results_store_that_is_down_refuses_the_edit() -> None:
    class ResultsDown(MemoryRepository):
        async def find_result(self, account_id: str, idempotency_key: str) -> None:  # type: ignore[override]
            raise PersistenceError("db down")

    limits, risk, *_ = service(results=ResultsDown())

    result = await limits.set_limits(command(BASE))

    assert not result.accepted and (result.reason or "").startswith("RISK_STATE_UNAVAILABLE")
    assert await risk.load_limits("main") is None


async def test_two_edits_in_flight_cannot_undo_each_other() -> None:
    """Both documents start from the same stored version: one tightens ``per_trade_max_risk``,
    the other tightens ``max_trades_per_day`` but still carries the OLD ``per_trade_max_risk``.
    Validated against the same version, both would pass and the second save would undo the first
    tightening. With the lock the second is judged against the first, and refused."""

    class Yielding(MemoryRiskRepository):
        async def load_limits(self, account_id: str):  # type: ignore[no-untyped-def,override]
            stored = await super().load_limits(account_id)
            await asyncio.sleep(0)  # let the other edit run between this load and the save
            return stored

        async def save_limits(
            self, account_id: str, limits: dict[str, LimitValue], at: datetime
        ) -> int:
            await asyncio.sleep(0)
            return await super().save_limits(account_id, limits, at)

    limits, risk, _, clock = service(Yielding())
    await limits.set_limits(command(BASE))
    clock.at = MID_DAY

    first, second = await asyncio.gather(
        limits.set_limits(command(edit(per_trade_max_risk="500"), key="a")),
        limits.set_limits(command(edit(max_trades_per_day=4), key="b")),
    )

    assert first.accepted
    assert not second.accepted and (second.reason or "").startswith(TIGHTEN_ONLY)
    stored = await risk.load_limits("main")
    assert stored is not None and stored.limits["per_trade_max_risk"] == "500"
    assert limits.current is not None and limits.current.per_trade_max_risk == D(500)


async def test_a_broken_stored_document_cannot_be_replaced_during_market_hours() -> None:
    """Treating it as empty would skip the tighten-only rule; after the close it can be fixed."""
    limits, risk, _, clock = service()
    await risk.save_limits("main", {**BASE, "price_band_pct": "-5"}, AFTER_CLOSE)  # type: ignore[arg-type]
    await limits.load()
    clock.at = MID_DAY

    refused = await limits.set_limits(command(BASE, key="fix-now"))
    clock.at = AFTER_CLOSE
    fixed = await limits.set_limits(command(BASE, key="fix-later"))

    assert not refused.accepted and "stored limits are unusable" in (refused.reason or "")
    assert fixed.accepted and limits.status is LimitsStatus.SET


# ---- through the command consumer -----------------------------------------------------------


async def test_the_consumer_routes_set_risk_limits_to_the_limits_service() -> None:
    limits, risk, *_ = service()
    order_service, adapter, repo = build()
    consumer = CommandConsumer(
        cast(Redis, None), order_service, account_id="main", set_limits=limits.set_limits
    )

    result = await consumer._process(command(BASE))

    assert result.accepted
    assert limits.current == parse_limits(BASE)
    assert adapter.placed == [] and repo.orders == {}  # no order path was touched


# ---- review round 1: storage that fails around the save ---------------------------------------


class CommitThenError(MemoryRiskRepository):
    """The write commits, then the call reports a failure (a timeout after COMMIT)."""

    async def save_limits(
        self, account_id: str, limits: dict[str, LimitValue], at: datetime
    ) -> int:
        await super().save_limits(account_id, limits, at)
        raise PersistenceError("timed out after commit")


async def test_a_save_that_committed_before_it_errored_is_believed_after_a_reread() -> None:
    risk = CommitThenError()
    limits, *_ = service(risk)

    result = await limits.set_limits(command(BASE))

    assert result.accepted  # the store has it, so it is in force
    assert limits.status is LimitsStatus.SET and limits.current == parse_limits(BASE)


async def test_a_save_that_failed_and_cannot_be_read_back_stops_entries_until_a_reload() -> None:
    class Gone(MemoryRiskRepository):
        down = False

        async def save_limits(
            self, account_id: str, limits: dict[str, LimitValue], at: datetime
        ) -> int:
            self.down = True
            raise PersistenceError("db down")

        async def load_limits(self, account_id: str):  # type: ignore[no-untyped-def,override]
            if self.down:
                raise PersistenceError("db down")
            return await super().load_limits(account_id)

    risk = Gone()
    await MemoryRiskRepository.save_limits(risk, "main", dict(BASE), AFTER_CLOSE)
    limits, *_ = service(risk)
    await limits.load()
    assert limits.status is LimitsStatus.SET

    result = await limits.set_limits(command(edit(per_trade_max_risk="1500"), key="k2"))

    assert not result.accepted and (result.reason or "").startswith("RISK_STATE_UNAVAILABLE")
    # Memory may no longer match the store, so entries stop (fail closed) until load() succeeds.
    assert limits.status is LimitsStatus.NOT_LOADED and limits.current is None
    risk.down = False
    assert await limits.load() is True and limits.status is LimitsStatus.SET


async def test_a_storage_failure_is_not_remembered_as_the_answer_for_that_key() -> None:
    class Flaky(MemoryRiskRepository):
        down = True

        async def load_limits(self, account_id: str):  # type: ignore[no-untyped-def,override]
            if self.down:
                raise PersistenceError("db down")
            return await super().load_limits(account_id)

    risk = Flaky()
    limits, _, results, _ = service(risk)

    first = await limits.set_limits(command(BASE, key="retry-me"))
    risk.down = False
    second = await limits.set_limits(command(BASE, key="retry-me"))

    assert not first.accepted and (first.reason or "").startswith("RISK_STATE_UNAVAILABLE")
    assert ("main", "retry-me") in results.results  # only the second answer was stored
    assert second.accepted


async def test_a_refusal_for_unavailable_storage_is_audited_when_the_audit_still_works() -> None:
    class LoadDown(MemoryRiskRepository):
        async def load_limits(self, account_id: str):  # type: ignore[no-untyped-def,override]
            raise PersistenceError("db down")

    risk = LoadDown()
    limits, *_ = service(risk)

    await limits.set_limits(command(BASE))

    [event] = risk.events
    assert (event.decision, event.reason_code) == ("REJECT", "RISK_STATE_UNAVAILABLE")


async def test_a_command_for_another_account_is_refused_and_stores_nothing() -> None:
    limits, risk, results, _ = service()
    foreign = SetRiskLimits(
        command_id="c", idempotency_key="k", account_id="spouse", source="cli",
        sent_at=MID_DAY, limits=BASE,
    )  # fmt: skip

    result = await limits.set_limits(foreign)

    assert not result.accepted and "spouse" in (result.reason or "")
    assert await risk.load_limits("main") is None and results.results == {} and risk.events == []


async def test_a_lost_audit_row_is_logged_with_the_changed_values() -> None:
    from structlog.testing import capture_logs

    class AuditDown(MemoryRiskRepository):
        async def append_event(self, event: object) -> None:  # type: ignore[override]
            raise PersistenceError("audit grant revoked")

    risk = AuditDown()
    await MemoryRiskRepository.save_limits(risk, "main", dict(BASE), AFTER_CLOSE)
    limits, *_ = service(risk)
    await limits.load()

    with capture_logs() as logs:
        await limits.set_limits(command(edit(per_trade_max_risk="1500"), key="loosen"))

    [lost] = [entry for entry in logs if entry["event"] == "risk_limits_audit_not_stored"]
    assert lost["changed"] == {"per_trade_max_risk": ["1000", "1500"]}
    assert lost["decision"] == "ALLOW" and lost["idempotency_key"] == "loosen"


async def test_a_stored_value_no_audit_row_can_carry_does_not_break_a_refusal() -> None:
    """A hand-edited document may hold a list or a float. The refusal is still answered and
    audited (the odd value as text), and the command is not left pending."""
    limits, risk, _, clock = service()
    await MemoryRiskRepository.save_limits(
        risk,
        "main",
        {**BASE, "per_trade_max_risk": [1, 2]},
        AFTER_CLOSE,  # type: ignore[dict-item]
    )
    await limits.load()
    clock.at = MID_DAY  # market hours: an unusable stored document cannot be replaced

    result = await limits.set_limits(command(BASE, key="fix-now"))

    assert not result.accepted and "stored limits are unusable" in (result.reason or "")
    assert risk.events[-1].inputs["old.per_trade_max_risk"] == "[1, 2]"
