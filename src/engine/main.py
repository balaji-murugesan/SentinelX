"""Engine entry point (``uv run --env-file .env python -m engine.main``)."""

from contracts.banner import log_startup_banner
from contracts.settings import SettingsError, load_settings
from engine.config import EngineSettings


def main() -> None:
    try:
        settings = load_settings(EngineSettings)
    except SettingsError as exc:
        # SystemExit with a string prints it to stderr and exits with status 1: the process
        # refuses to start rather than running with a bad configuration.
        raise SystemExit(str(exc)) from None
    log_startup_banner("engine", settings.mode)


if __name__ == "__main__":
    main()
