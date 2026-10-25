"""The ``migrate`` service: apply database migrations, then exit.

Run as ``python -m engine.persistence.migrate``. Exit status 0 means the schema is up to date;
anything else means it is not, and Compose will then refuse to start the engine (so a failed
migration cannot crash-loop it).

It uses its own settings (never EngineSettings): only this process holds the migrate role's
credentials.
"""

from pathlib import Path
from typing import Literal

import structlog
from alembic import command
from alembic.config import Config
from pydantic import SecretStr, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from observability.logging_setup import configure_logging

REPO_ROOT = Path(__file__).resolve().parents[3]  # src/engine/persistence/migrate.py -> repo root


class MigrateSettings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore", hide_input_in_errors=True)

    migrate_database_url: SecretStr
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    @field_validator("migrate_database_url")
    @classmethod
    def _scheme(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().startswith("postgresql+asyncpg://"):
            raise ValueError("must start with postgresql+asyncpg://")
        return value


def alembic_config(url: str) -> Config:
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(Path(__file__).parent / "migrations"))
    config.attributes["url"] = url  # passed in memory, never written to a file
    return config


def main() -> None:
    try:
        settings = MigrateSettings()  # type: ignore[call-arg]
    except ValidationError as exc:
        names = ", ".join(str(e["loc"][0]).upper() for e in exc.errors() if e["loc"])
        raise SystemExit(f"Invalid configuration: {names}") from None

    configure_logging("migrate", settings.log_level)
    log = structlog.get_logger("migrate")
    log.info("applying database migrations")
    try:
        command.upgrade(alembic_config(settings.migrate_database_url.get_secret_value()), "head")
    except Exception:
        # The log pipeline masks any credentials that appear in the error text.
        log.exception("database migration failed")
        raise SystemExit(1) from None
    log.info("database migrations applied")


if __name__ == "__main__":
    main()
