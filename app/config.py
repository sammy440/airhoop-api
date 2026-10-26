"""Environment configuration (README §10: DATABASE_URL, ALLOWED_ORIGINS)."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://localhost/airhoops"
    # Comma-separated list of frontend origins allowed by CORS.
    allowed_origins: str = "http://localhost:3000"

    max_players: int = 4
    # A disconnected player keeps their seat this long (page reloads, Wi-Fi blips).
    reconnect_grace_s: float = 30.0
    # Connected sockets that send nothing (not even a ping) for this long are dropped.
    heartbeat_timeout_s: float = 25.0
    # How often the background sweeper runs; 0 disables it (tests drive it manually).
    sweep_interval_s: float = 5.0

    @property
    def origins(self) -> list[str]:
        return [o.strip() for o in self.allowed_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
