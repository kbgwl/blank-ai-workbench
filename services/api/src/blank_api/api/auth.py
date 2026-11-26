import secrets
from typing import Annotated

from fastapi import Depends, Header

from blank_api.api.errors import ApiError
from blank_api.config import get_settings


async def require_token(authorization: Annotated[str | None, Header()] = None) -> str:
    settings = get_settings()
    if not settings.api_token:
        return "local-owner"
    # 定长比较：!= 会在第一个不同字符处提前返回，给逐字节猜令牌留下时间差。
    # 按 UTF-8 字节比：compare_digest 对含非 ASCII 的 str 会直接抛 TypeError。
    if not secrets.compare_digest(
        str(authorization or "").encode("utf-8"),
        f"Bearer {settings.api_token}".encode("utf-8"),
    ):
        raise ApiError(code="unauthorized", message="缺少或错误的访问令牌", status_code=401)
    return "owner"


Principal = Annotated[str, Depends(require_token)]
