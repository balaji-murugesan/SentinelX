"""Holds the account's risk limits in memory and applies the ``SetRiskLimits`` command to them
(add-risk-engine task 4.3; risk-limits spec).

``LimitsService`` is the one writer of the stored limits. The rest of the engine only *reads*
``status`` and ``current`` (the gate and the monitor, from task 5.3 on), so a check never touches
the database and always sees one consistent document.

Why one lock around load, validate and save: ``save_limits`` has no compare-and-set. Two edits
that were both validated against the same stored version would each pass the tighten-only check,
and the second save would silently undo the first one's tightening. ``asyncio.Lock`` is a C#
``SemaphoreSlim(1)``: ``async with`` waits for it and always releases it (``try/finally``).

Idempotency reuses the command-result table the order commands already use
(``TradingRepository.find_result`` / ``save_result``), keyed by account and idempotency key, so a
repeated key returns the first answer and changes nothing. The answer is stored for a rejected
edit too, so a retry of a refused edit is refused the same way.

Fail closed: a stored document that no longer parses is never "no limits" and never "defaults".
It leaves the service ``UNUSABLE``, and during market hours it cannot be replaced either, because
treating it as empty would let any value through the tighten-only rule.
"""

import asyncio
import time
from collections.abc import Callable
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Final

import structlog

from contracts.commands import CommandResult, LimitValue, SetRiskLimits
from engine.market_data.clock import is_market_hours, utc_now
from engine.persistence.repository import PersistenceError, TradingRepository
from engine.risk.limits import (
    LIMITS_INVALID,
    LIMITS_NOT_SET,
    STATE_UNAVAILABLE,
    EditRejected,
    InvalidLimits,
    RiskLimits,
    parse_limits,
    validate_edit,
)
from engine.risk.repository import AuditValue, RiskEvent, RiskRepository

log = structlog.get_logger("engine.risk.limits")

RULE: Final = "limits"  # the rule id on audit records of limit edits
PHASE: Final = "config"


class LimitsStatus(StrEnum):
    """What the engine knows about its limits. ``StrEnum`` is a C# ``enum`` whose members are also
    strings. Only ``SET`` lets an entry through; every other status refuses entries, each with its
    own stable reason code (see ``reason_for``)."""

    NOT_LOADED = "NOT_LOADED"  # never read from storage, or the read failed
    NOT_SET = "NOT_SET"  # the owner has not set limits yet (a fresh install)
    UNUSABLE = "UNUSABLE"  # a document is stored but does not validate
    SET = "SET"


def _scalar(value: object) -> AuditValue:
    """A value an audit row can carry: text, a whole number, a flag or None. Anything else (a
    hand-edited document might hold a list or a float) is written as its ``repr``."""
    if value is None or isinstance(value, (str, int, bool)):
        return value
    return repr(value)[:200]


class LimitsService:
    def __init__(
        self,
        *,
        account_id: str,
        repository: RiskRepository,
        results: TradingRepository,
        figures: Callable[[], tuple[Decimal, Decimal | None]],
        now: Callable[[], datetime] = utc_now,
    ) -> None:
        """``figures`` returns today's ``(peak, floor)`` from the engine's own state (floor None
        until there is one). It has no default on purpose: the floor re-simulation of an edit must
        use the real figures, and a forgotten argument would silently use zero."""
        self._account_id = account_id
        self._repo = repository
        self._results = results
        self._figures = figures
        self._now = now
        self._lock = asyncio.Lock()
        self._status = LimitsStatus.NOT_LOADED
        self._current: RiskLimits | None = None
        self._unusable_reason: str | None = None

    @property
    def status(self) -> LimitsStatus:
        return self._status

    @property
    def current(self) -> RiskLimits | None:
        """The limits in force; None unless ``status`` is ``SET``."""
        return self._current

    def reason_for(self) -> str | None:
        """The reason entries are refused because of the limits, or None when they are set."""
        match self._status:
            case LimitsStatus.SET:
                return None
            case LimitsStatus.NOT_SET:
                return f"{LIMITS_NOT_SET}: the owner has not set risk limits yet"
            case LimitsStatus.UNUSABLE:
                return f"{LIMITS_INVALID}: stored limits are unusable: {self._unusable_reason}"
            case LimitsStatus.NOT_LOADED:
                return f"{STATE_UNAVAILABLE}: risk limits could not be loaded"

    async def load(self) -> bool:
        """Read the stored limits (at start, and again while they are ``NOT_LOADED``). True once
        the storage answered, whatever it held; False if it did not (entries stay refused)."""
        async with self._lock:
            return await self._load()

    async def _load(self) -> bool:
        try:
            stored = await self._repo.load_limits(self._account_id)
        except PersistenceError as error:
            log.error("risk_limits_not_loaded", error=str(error))
            return False
        self._adopt(None if stored is None else stored.limits)
        return True

    def _adopt(self, document: dict[str, LimitValue] | None) -> None:
        """Make ``document`` (None: nothing stored) the limits in force, or the reason it is not
        usable."""
        self._current, self._unusable_reason = None, None
        if document is None:
            self._status = LimitsStatus.NOT_SET
            return
        try:
            self._current = parse_limits(document)
        except InvalidLimits as error:
            self._unusable_reason = str(error)
            self._status = LimitsStatus.UNUSABLE
            log.error("risk_limits_unusable", problems=error.problems)
            return
        self._status = LimitsStatus.SET

    # ---- the SetRiskLimits command ---------------------------------------------------------

    async def set_limits(self, command: SetRiskLimits) -> CommandResult:
        """Apply one edit completely or not at all, and answer it."""
        if command.account_id != self._account_id:
            return self._answer(
                command, f"command is for account {command.account_id!r}, not {self._account_id!r}"
            )
        async with self._lock:
            try:
                first = await self._results.find_result(command.account_id, command.idempotency_key)
            except PersistenceError as error:
                return self._answer(command, f"{STATE_UNAVAILABLE}: {error}")
            if first is not None:
                return first  # the same key twice: the first answer, nothing else happens
            result = await self._apply(command)
            if (result.reason or "").startswith(STATE_UNAVAILABLE):
                # A storage failure is not an answer to remember: the same key must be allowed to
                # succeed once the storage is back.
                return result
            try:
                await self._results.save_result(result)
            except PersistenceError as error:
                # The edit stands. A repeat of this key is judged again, and an identical edit
                # changes nothing, so it gets the same answer.
                log.warning("risk_limits_result_not_stored", error=str(error))
            return result

    async def _apply(self, command: SetRiskLimits) -> CommandResult:
        started = time.perf_counter()
        at = self._now()
        try:
            stored = await self._repo.load_limits(self._account_id)
        except PersistenceError as error:
            return await self._refused(command, STATE_UNAVAILABLE, [str(error)], {}, started)

        old: RiskLimits | None = None
        old_values: dict[str, LimitValue] = {}
        if stored is not None:
            old_values = dict(stored.limits)
            try:
                old = parse_limits(stored.limits)
            except InvalidLimits as error:
                if is_market_hours(at):
                    # Replacing a broken document now would skip the tighten-only rule.
                    return await self._refused(
                        command, LIMITS_INVALID,
                        [f"stored limits are unusable ({error}); fix them after the close"],
                        old_values, started,
                    )  # fmt: skip

        peak, floor = self._figures()
        try:
            new = validate_edit(old, command.limits, now=at, peak=peak, floor=floor)
        except EditRejected as rejected:
            return await self._refused(
                command, rejected.code, rejected.problems, old_values, started
            )

        try:
            await self._repo.save_limits(self._account_id, new.to_values(), at)
        except PersistenceError as error:
            # The error can arrive after the write committed (a timeout after COMMIT). So do not
            # assume either way: read the store back and believe it. If it cannot be read, the
            # limits in memory may differ from the stored ones, so entries stop until it can.
            if not await self._load():
                self._current, self._status = None, LimitsStatus.NOT_LOADED
            if self._current != new:
                return await self._refused(
                    command, STATE_UNAVAILABLE, [str(error)], old_values, started
                )
        else:
            self._current, self._unusable_reason = new, None
            self._status = LimitsStatus.SET

        result = self._answer(command, None)
        await self._audit(command, "ALLOW", None, old_values, new.to_values(), [], started, at)
        return result

    async def _refused(
        self,
        command: SetRiskLimits,
        code: str,
        problems: tuple[str, ...] | list[str],
        old_values: dict[str, LimitValue],
        started: float,
    ) -> CommandResult:
        await self._audit(
            command, "REJECT", code, old_values, dict(command.limits), list(problems), started,
            self._now(),
        )  # fmt: skip
        return self._answer(command, f"{code}: " + "; ".join(problems))

    async def _audit(
        self,
        command: SetRiskLimits,
        decision: str,
        code: str | None,
        old: dict[str, LimitValue],
        new: dict[str, LimitValue],
        problems: list[str],
        started: float,
        at: datetime,
    ) -> None:
        """Append the edit's record: old values, attempted or new values, and the outcome. A record
        that cannot be built or written is logged with what it said, never raised: the edit's
        outcome does not depend on its audit row. (A hand-edited stored document may hold a value
        no audit row can carry, a list say, so old values are made safe first.)"""
        inputs: dict[str, AuditValue] = {
            "outcome": "accepted" if decision == "ALLOW" else "rejected",
            "source": command.source,
            "idempotency_key": command.idempotency_key,
            "problems": "; ".join(problems) or None,
        }
        inputs.update({f"old.{key}": _scalar(value) for key, value in old.items()})
        inputs.update({f"new.{key}": _scalar(value) for key, value in new.items()})
        try:
            await self._repo.append_event(
                RiskEvent(
                    at=at,
                    account_id=self._account_id,
                    rule=RULE,
                    phase=PHASE,
                    decision=decision,
                    enforced=True,
                    latency_us=int((time.perf_counter() - started) * 1_000_000),
                    reason_code=code,
                    inputs=inputs,
                )  # fmt: skip
            )
        except Exception as error:
            # The record is lost, so put what it said in the log: an unaudited loosening must
            # still be reconstructable.
            changed = {
                key: [old.get(key), new.get(key)]
                for key in old.keys() | new.keys()
                if old.get(key) != new.get(key)
            }
            log.error(
                "risk_limits_audit_not_stored", decision=decision, reason_code=code,
                source=command.source, idempotency_key=command.idempotency_key,
                changed=changed, problems=problems, error=str(error),
            )  # fmt: skip

    def _answer(self, command: SetRiskLimits, reason: str | None) -> CommandResult:
        return CommandResult(
            command_id=command.command_id,
            idempotency_key=command.idempotency_key,
            account_id=self._account_id,
            accepted=reason is None,
            reason=reason,
            decided_at=self._now(),
        )
