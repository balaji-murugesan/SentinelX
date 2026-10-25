"""Engine settings: the shared mode/Redis/log settings plus the engine's database access."""

from pathlib import Path

from pydantic import SecretStr, field_validator

from contracts.settings import ModeSettings
from engine.healthcheck import DEFAULT_HEARTBEAT_PATH


class EngineSettings(ModeSettings):
    # Engine-role database URL (data read/write only, design decision 14). Required.
    # The DDL-capable migrate role's URL is never part of these settings.
    database_url: SecretStr

    # Where the event loop writes its liveness heartbeat (read by `python -m engine.healthcheck`,
    # which uses the same ENGINE_HEARTBEAT_PATH variable).
    engine_heartbeat_path: Path = DEFAULT_HEARTBEAT_PATH

    @field_validator("database_url")
    @classmethod
    def _database_scheme(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().startswith("postgresql+asyncpg://"):
            raise ValueError("must start with postgresql+asyncpg://")
        return value
