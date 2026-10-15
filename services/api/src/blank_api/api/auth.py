from typing import Annotated

from fastapi import Depends, Header

from blank_api.api.errors import ApiError
from blank_api.config import get_settings


async def require_token(authorization: Annotated[str | None, Header()] = None) -> str:
    settings = get_settings()
    if not settings.api_token:
        return "local-owner"
    if authorization != f"Bearer {settings.api_token}":
        raise ApiError(code="unauthorized", message="缺少或错误的访问令牌", status_code=401)
    return "owner"


Principal = Annotated[str, Depends(require_token)]
