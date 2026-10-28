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

    @model_validator(mode="after")
    def _limit_price_matches_type(self) -> Self:
        check_limit_price(self.order_type, self.limit_price)
        return self


class CancelOrder(_CommandBase):
    command: Literal["cancel_order"] = "cancel_order"
    order_id: str = Field(min_length=1)


# Annotated[..., Field(discriminator=...)] tells pydantic to choose the class by `command`.
Command = Annotated[PlaceOrder | CancelOrder, Field(discriminator="command")]
_COMMAND = TypeAdapter(Command)


def parse_command(raw: str | bytes) -> PlaceOrder | CancelOrder:
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
    decided_at: AwareDatetime
