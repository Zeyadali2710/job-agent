from functools import lru_cache
from pathlib import Path
from urllib.parse import quote_plus

from pydantic_settings import BaseSettings, SettingsConfigDict


def _read_secret(name: str) -> str:
    """Read a Docker secret file. Fails loudly rather than falling back to env."""
    path = Path("/run/secrets") / name
    if not path.exists():
        raise RuntimeError(f"Required secret '{name}' not mounted at {path}")
    value = path.read_text().strip()
    if not value:
        raise RuntimeError(f"Secret '{name}' at {path} is empty")
    return value


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, extra="ignore")

    dry_run: bool = True
    log_level: str = "INFO"
    tz: str = "Australia/Sydney"

    # Not a secret: the MagicDNS name the Discord card deep-links back to.
    tailnet_hostname: str = ""

    postgres_user: str = "ja_api"
    postgres_db: str = "jobagent"
    postgres_host: str = "postgres"
    postgres_port: int = 5432

    @property
    def _password(self) -> str:
        # Percent-encoded: '/' and '+' in a password otherwise corrupt the DSN.
        return quote_plus(_read_secret(f"{self.postgres_user}_password"))

    def _dsn(self, driver: str) -> str:
        return (
            f"postgresql+{driver}://{self.postgres_user}:{self._password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def anthropic_api_key(self) -> str:
        """Lazy, like discord_webhook: only the worker mounts this secret."""
        return _read_secret("anthropic_api_key")

    @property
    def discord_webhook(self) -> str:
        """Read lazily, like _password: only the notifier mounts this secret,
        and every other service must still start without it."""
        return _read_secret("discord_webhook")

    @property
    def database_url(self) -> str:
        """Async DSN — asyncpg. Used by every long-running service."""
        return self._dsn("asyncpg")

    @property
    def sync_database_url(self) -> str:
        """Sync DSN — psycopg. Used by Alembic only."""
        return self._dsn("psycopg")


@lru_cache
def get_settings() -> Settings:
    return Settings()