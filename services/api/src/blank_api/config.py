from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="BLANK_", env_file=".env", extra="ignore")

    app_name: str = "blank-ai-workbench"
    database_url: str = "sqlite+aiosqlite:///./data/blank_dev.db"
    redis_url: str = "redis://localhost:6379/0"
    cors_origins: list[str] = ["http://localhost:3000"]
    # 空字符串 = 本机单用户模式，不校验 token
    api_token: str = ""
    # 默认不把宿主机绝对路径返回给前端
    expose_workspace_paths: bool = False
    # Fernet 主密钥（urlsafe base64）。留空则自动写入 data/.blank_secret.key
    secret_key: str = ""
    model_timeout_seconds: float = 60.0


@lru_cache
def get_settings() -> Settings:
    return Settings()
