"""Api entry point. The FastAPI app arrives in task 4.1; for now it validates config and
announces its mode."""

from api.config import ApiSettings
from contracts.banner import log_startup_banner
from contracts.settings import SettingsError, load_settings


def main() -> None:
    try:
        settings = load_settings(ApiSettings)
    except SettingsError as exc:
        raise SystemExit(str(exc)) from None
    log_startup_banner("api", settings.mode)


if __name__ == "__main__":
    main()
