"""Api settings. Deliberately no database fields: the api talks only to Redis (D-013)."""

from contracts.settings import ModeSettings


class ApiSettings(ModeSettings):
    """Nothing beyond the shared settings yet; Stage 5 adds auth settings."""
