"""Environment configuration."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://localhost/airhoops"
    # Comma-separated list of frontend origins allowed by CORS.
    allowed_origins: str = "http://localhost:3000"
    # Also allowed: origins matching this regex. Default: the production site and Vercel's
    # branch / preview deployments of this project (airhoops-git-master-…, airhoops-<hash>-…).
    allowed_origin_regex: str = r"https://airhoops(-[a-z0-9-]+-sammie440s-projects)?\.vercel\.app"

    max_players: int = 4
    # A disconnected player keeps their seat this long (page reloads, Wi-Fi blips).
    reconnect_grace_s: float = 30.0
    # Connected sockets that send nothing (not even a ping) for this long are dropped.
    heartbeat_timeout_s: float = 25.0
    # How often the background sweeper runs; 0 disables it (tests drive it manually).
    sweep_interval_s: float = 5.0
    # Match clock resolution (shootout turn timers, race buzzer); 0 disables it.
    tick_interval_s: float = 0.2

    rate_limit_enabled: bool = True
    sentry_dsn: str = ""
    environment: str = "development"
    log_level: str = "INFO"

    @property
    def db_url(self) -> str:
        """Managed hosts hand out postgres:// URLs; SQLAlchemy wants the psycopg driver named."""
        url = self.database_url
        for prefix in ("postgres://", "postgresql://"):
            if url.startswith(prefix):
                return "postgresql+psycopg://" + url[len(prefix) :]
        return url

    @property
    def origins(self) -> list[str]:
        """Forgiving about how the env var was typed: quotes, spaces, trailing slashes, a doubled scheme."""
        out = []
        for raw in self.allowed_origins.split(","):
            o = raw.strip().strip("'\"").rstrip("/")
            while o.startswith(("https://https://", "https://http://", "http://http://", "http://https://")):
                o = o.split("://", 1)[1]
            if o:
                out.append(o)
        return out


@lru_cache
def get_settings() -> Settings:
    return Settings()
