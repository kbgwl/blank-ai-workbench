from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from blank_api.db import get_session_factory, shielded_close


async def get_db() -> AsyncIterator[AsyncSession]:
    # 不用 ``async with``：客户端断流时请求任务会被取消，连接 teardown 正好落在
    # 取消点上，池子里就会留下一条半关闭的连接——后续任何请求检出它都会报
    # “no active connection”（任务于是启动一步就失败，或者永远停在 queued）。
    # 所以收尾单独屏蔽取消，只让业务体保持可取消。
    session = get_session_factory()()
    try:
        yield session
    finally:
        await shielded_close(session)


DbSession = Annotated[AsyncSession, Depends(get_db)]
