"""Reads order commands from the Redis command stream and answers each one (order-commands spec).

Follows the Stage 0 ping responder (``engine.diagnostics``): a stream in, one reply stream per
command with a TTL, and a loop that waits and retries instead of crashing when Redis is down.
The difference is delivery. Pings are fire-and-forget; commands move money, so they are read
through a *consumer group* (design §4):

  * ``XREADGROUP`` hands each message to one consumer and remembers it in the group's
    "pending entries list" (PEL) until the consumer calls ``XACK``. Think of a message-queue
    lease that is renewed on delivery and only released on acknowledge.
  * The engine reads its own PEL first (id ``0``): messages delivered before a crash but never
    acknowledged. Then it switches to new messages (id ``>``).
  * ``XACK`` happens only after the result is persisted (``OrderService`` stores the result in
    the same transaction as the order). A crash between persist and ack redelivers the command;
    the stored result is found by idempotency key and replied again, so nothing is duplicated.

Either mechanism alone leaves a crash window; together they close it (design §4).
"""

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, cast

import structlog
from pydantic import ValidationError
from redis.asyncio import Redis
from redis.exceptions import RedisError, ResponseError

from contracts.commands import (
    COMMAND_GROUP,
    COMMAND_STREAM,
    REPLY_TTL_SECONDS,
    CancelOrder,
    CommandResult,
    PlaceOrder,
    ResumeRecovery,
    parse_command,
    reply_stream,
)
from engine.orders.service import OrderService

log = structlog.get_logger("engine.orders.commands")

CONSUMER = "engine-1"  # one engine process in Phase 1; the name only has to stay stable
WRONG_ACCOUNT = "command is for account {theirs!r}, this engine serves {ours!r}"


class CommandConsumer:
    def __init__(
        self,
        redis: Redis,
        service: OrderService,
        *,
        account_id: str,
        resume: Callable[[ResumeRecovery], Awaitable[CommandResult]] | None = None,
    ) -> None:
        self._redis = redis
        self._service = service
        self._account_id = account_id
        # Injected (like a C# Func<,> passed to the constructor) so this module does not depend
        # on engine.orders.recovery; engine.main wires it. None = resume is not available.
        self._resume = resume

    async def run(
        self, stop: asyncio.Event, *, block_ms: int = 500, retry_seconds: float = 1.0
    ) -> None:
        """Process commands until ``stop`` is set.

        If Redis is unreachable the loop waits (1 s doubling to 10 s) and starts over from the
        pending entries, so a command that was delivered but not finished is picked up again.
        """
        delay = retry_seconds
        # "0" = redeliver my unacknowledged messages; ">" = only messages never delivered.
        read_id = "0"
        group_ready = False
        while not stop.is_set():
            try:
                if not group_ready:
                    await self._create_group()
                    group_ready = True
                batches = await cast(
                    "Any",
                    self._redis.xreadgroup(
                        COMMAND_GROUP, CONSUMER, {COMMAND_STREAM: read_id}, count=10, block=block_ms
                    ),
                )
                delay = retry_seconds
                messages = [m for _stream, entries in batches or [] for m in entries]
                if read_id != ">":
                    if not messages:
                        read_id = ">"  # pending list drained: now wait for new commands
                        continue
                    read_id = str(messages[-1][0])  # next page of pending, after this one
                for message_id, fields in messages:
                    await self._handle(str(message_id), fields)
            except (RedisError, OSError) as exc:
                log.warning("redis unavailable; retrying", error=type(exc).__name__, retry_in=delay)
                read_id = "0"
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=delay)
                delay = min(delay * 2, 10.0)

    async def _create_group(self) -> None:
        """Create the group at the start of the stream, so commands sent while the engine was
        down are still processed. ``mkstream`` creates the stream if nothing was sent yet."""
        try:
            await cast(
                "Any",
                self._redis.xgroup_create(COMMAND_STREAM, COMMAND_GROUP, id="0", mkstream=True),
            )
        except ResponseError as exc:
            if "BUSYGROUP" not in str(exc):  # BUSYGROUP = the group already exists: fine
                raise

    async def _handle(self, message_id: str, fields: dict[str, str]) -> None:
        try:
            command = parse_command(fields["data"])
        except (KeyError, ValidationError):
            # Never log the content (rule 10). Acknowledge, or this entry would be redelivered
            # at every restart: a poison message.
            log.warning("malformed command ignored", message_id=message_id)
            await self._ack(message_id)
            return

        if command.account_id != self._account_id:
            result = CommandResult(
                command_id=command.command_id,
                idempotency_key=command.idempotency_key,
                account_id=command.account_id,
                accepted=False,
                reason=WRONG_ACCOUNT.format(theirs=command.account_id, ours=self._account_id),
                decided_at=datetime.now(UTC),
            )
            log.warning("command for another account rejected", command_id=command.command_id)
        else:
            try:
                result = await self._process(command)
            except Exception:
                # Unexpected failure: leave the message unacknowledged. It is redelivered after
                # the next restart, and idempotency stops a second order.
                log.exception("command failed; left pending", command_id=command.command_id)
                return

        await self._reply(command.command_id, result)
        await self._ack(message_id)

    async def _process(self, command: PlaceOrder | CancelOrder | ResumeRecovery) -> CommandResult:
        # match/case on the type is like a C# switch expression with type patterns.
        match command:
            case PlaceOrder():
                return await self._service.place(command)
            case CancelOrder():
                return await self._service.cancel(command)
            case ResumeRecovery():
                if self._resume is None:
                    return CommandResult(
                        command_id=command.command_id,
                        idempotency_key=command.idempotency_key,
                        account_id=command.account_id,
                        accepted=False,
                        reason="recovery resume is not available in this engine",
                        decided_at=datetime.now(UTC),
                    )
                return await self._resume(command)

    async def _reply(self, command_id: str, result: CommandResult) -> None:
        key = reply_stream(command_id)
        # A transactional pipeline sends XADD and EXPIRE together (like one DB transaction),
        # so a reply stream can never be left without its TTL.
        async with self._redis.pipeline(transaction=True) as pipe:
            pipe.xadd(key, {"data": result.model_dump_json()})
            pipe.expire(key, REPLY_TTL_SECONDS)
            await cast("Any", pipe.execute())

    async def _ack(self, message_id: str) -> None:
        await cast("Any", self._redis.xack(COMMAND_STREAM, COMMAND_GROUP, message_id))
