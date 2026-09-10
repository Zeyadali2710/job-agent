from functools import lru_cache
from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict


def _read_secret(name: str) -> str:
    """Read a Docker secret file. Fails loudly rather than falling back to env."""
    path = Path("/run/secrets") / name
    if not path.exists():
        raise RuntimeError(f"Required secret '{name}' not mounted at {path}")
    return path.read_text().strip()


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, extra="ignore")

    dry_run: bool = True
    log_level: str = "INFO"
    tz: str = "Australia/Sydney"

    postgres_user: str = "jobagent"
    postgres_db: str = "jobagent"
    postgres_host: str = "postgres"
    postgres_port: int = 5432

    @property
    def database_url(self) -> str:
        pw = _read_secret("postgres_password")
        return (
            f"postgresql+asyncpg://{self.postgres_user}:{pw}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()