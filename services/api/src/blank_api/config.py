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
    # 主密钥落盘位置。桌面壳会指到用户数据目录（与数据库同一处），
    # 避免升级换包后主密钥跟着换、已存的供应商 Key 全部解不开。
    secret_key_file: str = ""
    # 运行时设置（含安全与权限）落盘位置。同理：留在包内会在升级后重置成默认值，
    # 客户配好的权限边界会悄悄漂回默认——对"权限可控"来说是致命的。
    settings_file: str = ""
    model_timeout_seconds: float = 60.0


@lru_cache
def get_settings() -> Settings:
    return Settings()
