import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from blank_api.api.errors import ApiError, error_body

logger = logging.getLogger(__name__)


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api_error(request: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content=error_body(exc.code, exc.message))

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=error_body(f"http_{exc.status_code}", str(exc.detail)),
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        # 不回显 exc.errors()：Pydantic v2 会把提交的 input 原样带进去，
        # 一旦是 API Key / 密码类字段，错误响应就成了二次泄漏。
        parts: list[str] = []
        for item in exc.errors()[:8]:
            loc = ".".join(
                str(piece) for piece in (item.get("loc") or ()) if str(piece) != "body"
            )
            message = str(item.get("msg") or "不合法")
            parts.append(f"{loc}: {message}" if loc else message)
        return JSONResponse(
            status_code=422,
            content=error_body("validation_error", "；".join(parts) or "请求参数不合法"),
        )

    @app.exception_handler(Exception)
    async def _internal_error(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("unhandled error: %s %s", request.method, request.url.path)
        return JSONResponse(
            status_code=500,
            content=error_body("internal_error", "unexpected server error"),
        )
