"""Order commands sent to the engine over Redis, and their replies (order-commands spec).

Commands go on one Redis Stream; the engine answers each on its own reply stream, the same
pattern as the Stage 0 diagnostic ping. ``Command`` is a *discriminated union*: the
``command`` field names the model, so one stream carries both kinds and pydantic picks the
right class when parsing (like a C# polymorphic JSON converter keyed on a type tag).
"""

from decimal import Decimal
from typing import Annotated, Literal, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, TypeAdapter, model_validator

from contracts.market import InstrumentKey
from contracts.orders import OrderSide, OrderType, ProductType, check_limit_price

COMMAND_STREAM = "sentinelx:commands"
COMMAND_GROUP = "engine"
REPLY_TTL_SECONDS = 60


def reply_stream(command_id: str) -> str:
    """Where the engine writes the result of one command."""
    return f"sentinelx:reply:{command_id}"


class _CommandBase(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1] = 1
    command_id: str = Field(min_length=1)
    # Same key = same intent: a repeat returns the first result and does nothing (rule 5).
    idempotency_key: str = Field(min_length=1)
    account_id: str = Field(min_length=1)
    source: str = Field(min_length=1)
    sent_at: AwareDatetime


class PlaceOrder(_CommandBase):
    command: Literal["place_order"] = "place_order"
    key: InstrumentKey
    side: OrderSide
    quantity: int = Field(gt=0)
    product: ProductType
    order_type: OrderType
    limit_price: Decimal | None = Field(default=None, gt=0)
    # Optional with defaults, so a command sent before add-risk-engine still parses.
    # ``Field(default=None, gt=0)`` is a declarative guard, like a C# ``[Range]`` attribute: it is
    # checked when the command is built, and None (absent) skips the check.
    trigger_price: Decimal | None = Field(default=None, gt=0)  # STOP_MARKET only
    initial_stop: Decimal | None = Field(default=None, gt=0)  # entries: where the trade is wrong

    @model_validator(mode="after")
    def _limit_price_matches_type(self) -> Self:
        check_limit_price(self.order_type, self.limit_price, self.trigger_price)
        return self


class CancelOrder(_CommandBase):
    command: Literal["cancel_order"] = "cancel_order"
    order_id: str = Field(min_length=1)


class ResumeRecovery(_CommandBase):
    """Ask the engine to re-check itself and, only if clean, clear its recovery halt."""

    command: Literal["resume_recovery"] = "resume_recovery"


class KillSwitch(_CommandBase):
    """Stop trading for the day: cancel pending entries and flatten every position. It carries
    nothing else: ``source`` says who asked, and a kill is idempotent per account and day."""

    command: Literal["kill_switch"] = "kill_switch"


# A limit value is text, a whole number, a flag, or None ("not configured"). ``A | B`` is a
# union type: the value must be one of them (closest C# analogue: ``OneOf<string, int, bool>``).
# There is deliberately no float member: money and percentages travel as strings ("1000.00")
# and become ``Decimal`` in the engine, so a rounding error can never enter a risk limit. A
# JSON number with a fraction would be a float, and is refused here.
LimitValue = str | int | bool | None


class SetRiskLimits(_CommandBase):
    """Replace the account's risk limits. The engine validates the keys, the values and the
    tighten-only rule; this model only fixes the shape. Flat keys, e.g. ``per_trade_max_risk``
    or ``mode.per_trade_risk``."""

    command: Literal["set_risk_limits"] = "set_risk_limits"
    limits: dict[str, LimitValue]


class SizeOrder(_CommandBase):
    """Ask how many units an entry may be for the per-trade risk limit. Changes no state."""

    command: Literal["size_order"] = "size_order"
    key: InstrumentKey
    side: OrderSide
    entry_price: Decimal = Field(gt=0)
    initial_stop: Decimal = Field(gt=0)


# Every command the engine can receive: a union type, like a C# base type with these subclasses.
# Code that handles it with ``match`` is checked by mypy to cover every member.
AnyCommand = PlaceOrder | CancelOrder | ResumeRecovery | KillSwitch | SetRiskLimits | SizeOrder
# Annotated[..., Field(discriminator=...)] tells pydantic to choose the class by `command`.
Command = Annotated[AnyCommand, Field(discriminator="command")]
_COMMAND = TypeAdapter(Command)


def parse_command(raw: str | bytes) -> AnyCommand:
    """Parse one stream entry. Raises pydantic.ValidationError for anything malformed."""
    return _COMMAND.validate_json(raw)


class CommandResult(BaseModel):
    """The engine's answer to one command, stored for idempotency and sent as the reply."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1] = 1
    command_id: str
    idempotency_key: str
    account_id: str
    accepted: bool
    order_id: str | None = None
    reason: str | None = None  # why it was rejected; None when accepted
    # Set only on the answer to a SizeOrder command (optional, so older replies still parse).
    sized_quantity: int | None = None  # units, a whole number of lots
    risk_amount: Decimal | None = None  # rupees at risk for that quantity
    decided_at: AwareDatetime
