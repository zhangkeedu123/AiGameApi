import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    database_url: str
    session_secret: str
    upload_dir: Path
    error_log: Path
    cookie_secure: bool = True
    pool_max: int = 10

    @classmethod
    def from_env(cls):
        secret = os.environ["SESSION_SECRET"]
        if len(secret) < 32:
            raise ValueError("SESSION_SECRET must be at least 32 characters")
        return cls(
            os.environ["DATABASE_URL"],
            secret,
            Path(os.getenv("UPLOAD_DIR", "var/uploads")).resolve(),
            Path(os.getenv("ERROR_LOG", "var/logs/errors.log")).resolve(),
            os.getenv("COOKIE_SECURE", "true").lower() in {"true", "1"},
            int(os.getenv("DB_POOL_MAX", "10")),
        )
