from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict


BASE_DIR = Path(__file__).parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=BASE_DIR / ".env", extra="ignore")

    anthropic_api_key: str = ""
    db_path: Path = BASE_DIR / "data" / "news.db"
    config_dir: Path = BASE_DIR / "config"
    log_level: str = "INFO"
    scan_interval_hours: int = 8
    min_relevance_score: int = 5
    llm_model: str = "claude-sonnet-4-6"
    fetch_timeout_s: int = 15
    fetch_max_connections: int = 20
    # Server binding — override for cloud deployment
    host: str = "127.0.0.1"
    port: int = 8080
    # DB backup endpoint — set a secret token to enable; empty = disabled
    backup_token: str = ""


settings = Settings()
