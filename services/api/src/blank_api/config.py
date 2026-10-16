from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="BLANK_", env_file=".env", extra="ignore")

    app_name: str = "blank-ai-workbench"
    database_url: str = "sqlite+aiosqlite:///./data/blank_dev.db"
    redis_url: str = "redis://localhost:6379/0"
    # 网页开发态（localhost:3000）+ 桌面壳（自定义协议 app://bundle）。
    # 桌面壳是独立来源，不放行的话渲染进程直连本机后端会被 CORS 拦掉。
    cors_origins: list[str] = ["http://localhost:3000", "app://bundle"]
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
